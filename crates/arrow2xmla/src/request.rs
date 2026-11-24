//! Parsing of an XMLA request (a SOAP envelope holding a `Discover` or an `Execute`). No DOCTYPE is ever accepted (no
//! entity expansion), the nesting and the number of elements are bounded, and what a strict parser refuses is refused
//! (a second root, text outside the root, an undeclared prefix, characters XML forbids). UTF-8 and UTF-16 with a BOM.
//! A body longer than a limit (`DEFAULT_MAX_REQUEST`, or the caller's) is refused before it is read.

use quick_xml::events::Event;
use quick_xml::XmlVersion;
use quick_xml::name::ResolveResult;
use quick_xml::NsReader;

use crate::esc::has_forbidden;
use crate::XA;

const SOAP: &str = "http://schemas.xmlsoap.org/soap/envelope/";
const MAX_DEPTH: usize = 64;
/// Elements one request may have: a request is a few dozen, a statement with its filters a few thousand. The body's
/// size is limited too; this keeps a body of nothing but tiny elements from costing many times its size.
const MAX_ELEMENTS: usize = 100_000;

/// Why a request was not accepted.
#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum ParseError {
    /// The request has a DOCTYPE (entities: a billion laughs).
    Doctype,
    /// The request is not XML, or not an XMLA request this parser accepts.
    NotXml(String),
    /// The request is longer than the limit given.
    TooLarge {
        /// The limit, in bytes.
        limit: usize,
    },
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::Doctype => write!(f, "DOCTYPE is not accepted"),
            ParseError::NotXml(e) => write!(f, "the request is not XML: {e}"),
            ParseError::TooLarge { limit } => write!(f, "the request is longer than {limit} bytes"),
        }
    }
}

impl std::error::Error for ParseError {}

/// What the SOAP header says about the session.
#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Session {
    /// No session header.
    None,
    /// `BeginSession`: the server makes an id.
    Begin,
    /// `Session SessionId=..`: the client's own id, to echo.
    Use(String),
    /// `EndSession`: the client ends it (with its id when it names one).
    End(Option<String>),
}

/// What the request asks for.
#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Call {
    /// A `Discover`: a request for a rowset of metadata.
    Discover {
        /// `RequestType`, e.g. `MDSCHEMA_CUBES`.
        request_type: String,
        /// Name -> values (`<NAME>v</NAME>` or `<NAME><Value>a</Value><Value>b</Value></NAME>`), in order.
        restrictions: Vec<(String, Vec<String>)>,
        /// `PropertyList` entries.
        properties: Vec<(String, String)>,
    },
    /// An `Execute`: an MDX statement.
    Execute {
        /// The `Statement` text.
        statement: String,
        /// `PropertyList` entries.
        properties: Vec<(String, String)>,
    },
    /// A body with neither.
    Other,
}

/// A parsed XMLA request.
#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct Request {
    /// What the SOAP header says about the session.
    pub session: Session,
    /// The first `Discover` or `Execute`.
    pub call: Call,
}

impl Call {
    /// A property's value (the last one when it is repeated).
    pub fn property(&self, name: &str) -> Option<&str> {
        let props = match self {
            Call::Discover { properties, .. } | Call::Execute { properties, .. } => properties,
            Call::Other => return None,
        };
        props.iter().rev().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
}

#[derive(Default)]
struct El {
    ns: String,
    local: String,
    attrs: Vec<(String, String)>,
    text: String,
    seen_child: bool,
    children: Vec<El>,
}

impl El {
    fn child(&self, ns: &str, local: &str) -> Option<&El> {
        self.children.iter().find(|c| c.ns == ns && c.local == local)
    }
    fn path(&self, p: &[(&str, &str)]) -> Option<&El> {
        p.iter().try_fold(self, |el, (ns, l)| el.child(ns, l))
    }
    fn attr(&self, name: &str) -> Option<&str> {
        self.attrs.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
}

fn not_xml<E: std::fmt::Display>(e: E) -> ParseError {
    ParseError::NotXml(e.to_string())
}

fn open(ns: &ResolveResult, e: &quick_xml::events::BytesStart) -> Result<El, ParseError> {
    let ns = match ns {
        ResolveResult::Bound(n) => n.as_ref().to_owned(),
        ResolveResult::Unbound => String::new(),
        ResolveResult::Unknown(_) => return Err(ParseError::NotXml("a namespace prefix that is not declared".into())),
    };
    let mut el = El { ns, local: e.local_name().as_ref().to_owned(), ..El::default() };
    for a in e.attributes() {
        let a = a.map_err(not_xml)?;
        let key = a.key.as_ref();
        if key == "xmlns" || key.starts_with("xmlns:") {
            continue; // a namespace declaration is not an attribute
        }
        if a.value.contains('<') {
            return Err(ParseError::NotXml("`<` in an attribute value".into()));
        }
        let v = a.normalized_value(XmlVersion::Implicit1_0).map_err(not_xml)?.into_owned();
        if has_forbidden(v.as_bytes()) {
            return Err(ParseError::NotXml("a character XML does not allow".into()));
        }
        if !key.contains(':') {
            el.attrs.push((key.to_owned(), v)); // a prefixed attribute is another one than the plain name
        }
    }
    Ok(el)
}

fn text_into(stack: &mut [El], s: &str) -> Result<(), ParseError> {
    if has_forbidden(s.as_bytes()) {
        return Err(ParseError::NotXml("a character XML does not allow".into()));
    }
    match stack.last_mut() {
        Some(top) => {
            if !top.seen_child {
                top.text.push_str(s); // like ElementTree's `.text`: only what comes before the first child
            }
            Ok(())
        }
        None if s.bytes().all(|b| matches!(b, b' ' | b'\t' | b'\r' | b'\n')) => Ok(()),
        None => Err(ParseError::NotXml("text outside the root element".into())),
    }
}

fn close(stack: &mut Vec<El>, root: &mut Option<El>) -> Result<(), ParseError> {
    if let Some(el) = stack.pop() {
        match stack.last_mut() {
            Some(parent) => {
                parent.seen_child = true;
                parent.children.push(el);
            }
            None if root.is_some() => return Err(ParseError::NotXml("more than one root element".into())),
            None => *root = Some(el),
        }
    }
    Ok(())
}

/// UTF-8 (with or without a BOM) as is; UTF-16 with a BOM is turned into UTF-8 first. Anything else is refused.
fn utf8(body: &[u8]) -> Result<std::borrow::Cow<'_, [u8]>, ParseError> {
    let utf16 = |rest: &[u8], le: bool| -> Result<Vec<u8>, ParseError> {
        if !rest.len().is_multiple_of(2) {
            return Err(ParseError::NotXml("a UTF-16 body of odd length".into()));
        }
        let units: Vec<u16> =
            rest.as_chunks::<2>().0.iter().map(|c| if le { u16::from_le_bytes(*c) } else { u16::from_be_bytes(*c) }).collect();
        String::from_utf16(&units).map(String::into_bytes).map_err(|_| ParseError::NotXml("invalid UTF-16".into()))
    };
    Ok(if let Some(r) = body.strip_prefix(b"\xFF\xFE") {
        utf16(r, true)?.into()
    } else if let Some(r) = body.strip_prefix(b"\xFE\xFF") {
        utf16(r, false)?.into()
    } else {
        body.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(body).into()
    })
}

fn dom(body: &[u8]) -> Result<El, ParseError> {
    let body = &*utf8(body)?;
    let mut reader = NsReader::from_reader(body);
    let mut buf = Vec::new();
    let mut stack: Vec<El> = vec![];
    let mut root: Option<El> = None;
    let mut elements = 0usize;
    // a new element: counted, bounded, and never a second root
    let begin = |stack: &Vec<El>, root: &Option<El>, elements: &mut usize| -> Result<(), ParseError> {
        *elements += 1;
        if *elements > MAX_ELEMENTS {
            return Err(ParseError::NotXml("too many elements".into()));
        }
        if stack.len() >= MAX_DEPTH {
            return Err(ParseError::NotXml("too deeply nested".into()));
        }
        if stack.is_empty() && root.is_some() {
            return Err(ParseError::NotXml("more than one root element".into()));
        }
        Ok(())
    };
    loop {
        let (ns, ev) = reader.read_resolved_event_into(&mut buf).map_err(not_xml)?;
        match ev {
            Event::DocType(_) => return Err(ParseError::Doctype),
            Event::Start(e) => {
                begin(&stack, &root, &mut elements)?;
                stack.push(open(&ns, &e)?);
            }
            Event::Empty(e) => {
                begin(&stack, &root, &mut elements)?;
                stack.push(open(&ns, &e)?);
                close(&mut stack, &mut root)?;
            }
            Event::End(_) => close(&mut stack, &mut root)?,
            Event::Text(t) => text_into(&mut stack, &t.xml10_content())?,
            Event::CData(c) => text_into(&mut stack, &c.xml10_content())?,
            Event::GeneralRef(r) => {
                let name = r.xml10_content();
                let ch = if let Some(c) = r.resolve_char_ref().map_err(not_xml)? {
                    c.to_string()
                } else {
                    match name.as_ref() {
                        "amp" => "&".into(),
                        "lt" => "<".into(),
                        "gt" => ">".into(),
                        "quot" => "\"".into(),
                        "apos" => "'".into(),
                        other => return Err(ParseError::NotXml(format!("unknown entity &{other};"))),
                    }
                };
                text_into(&mut stack, &ch)?;
            }
            Event::Eof => break,
            _ => {}
        }
        buf.clear();
    }
    if !stack.is_empty() {
        return Err(ParseError::NotXml("unclosed element".into()));
    }
    root.ok_or_else(|| ParseError::NotXml("no root element".into()))
}

fn list(parent: Option<&El>, outer: &str, inner: &str) -> Option<Vec<(String, Vec<String>)>> {
    let l = parent?.path(&[(XA, outer), (XA, inner)])?;
    Some(
        l.children
            .iter()
            .map(|c| {
                let vals: Vec<String> = c.children.iter().filter(|v| v.ns == XA && v.local == "Value").map(|v| v.text.clone()).collect();
                (c.local.clone(), if vals.is_empty() { vec![c.text.clone()] } else { vals })
            })
            .collect(),
    )
}

fn properties(call: &El) -> Vec<(String, String)> {
    call.path(&[(XA, "Properties"), (XA, "PropertyList")])
        .map(|l| l.children.iter().map(|c| (c.local.clone(), c.text.clone())).collect())
        .unwrap_or_default()
}

/// Parses one request. The envelope's elements are matched by namespace, as the protocol says, whatever prefix the
/// client uses.
pub fn parse_request(body: &[u8]) -> Result<Request, ParseError> {
    parse_request_limited(body, DEFAULT_MAX_REQUEST)
}

/// The longest request `parse_request` reads (4 MiB: a real one is a few KB).
pub const DEFAULT_MAX_REQUEST: usize = 4 << 20;

/// `parse_request` for a body of at most `max_size` bytes; a longer one is `ParseError::TooLarge` without being read.
pub fn parse_request_limited(body: &[u8], max_size: usize) -> Result<Request, ParseError> {
    if body.len() > max_size {
        return Err(ParseError::TooLarge { limit: max_size });
    }
    let root = dom(body)?;
    let session = match root.child(SOAP, "Header") {
        None => Session::None,
        Some(h) if h.child(XA, "BeginSession").is_some() => Session::Begin,
        Some(h) => match (h.child(XA, "EndSession"), h.child(XA, "Session")) {
            (Some(e), _) => Session::End(e.attr("SessionId").filter(|id| !id.is_empty()).map(str::to_owned)),
            (None, Some(s)) => s.attr("SessionId").filter(|id| !id.is_empty()).map_or(Session::None, |id| Session::Use(id.to_owned())),
            (None, None) => Session::None,
        },
    };
    // the first Discover (or Execute) of any Body, as a path lookup in ElementTree finds it
    let bodies = || root.children.iter().filter(|c| c.ns == SOAP && c.local == "Body");
    let call = if let Some(d) = bodies().find_map(|b| b.child(XA, "Discover")) {
        Call::Discover {
            request_type: d.child(XA, "RequestType").map(|r| r.text.trim().to_owned()).unwrap_or_default(),
            restrictions: list(Some(d), "Restrictions", "RestrictionList").unwrap_or_default(),
            properties: properties(d),
        }
    } else if let Some(x) = bodies().find_map(|b| b.child(XA, "Execute")) {
        Call::Execute {
            statement: x.path(&[(XA, "Command"), (XA, "Statement")]).map(|s| s.text.clone()).unwrap_or_default(),
            properties: properties(x),
        }
    } else {
        Call::Other
    };
    Ok(Request { session, call })
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEAD: &str = "<soap:Envelope xmlns:soap=\"http://schemas.xmlsoap.org/soap/envelope/\">";

    #[test]
    fn discover_with_values_and_session() {
        let x = format!(
            "{HEAD}<soap:Header><Session xmlns=\"{XA}\" SessionId=\"abc\"/></soap:Header><soap:Body><Discover xmlns=\"{XA}\">\
<RequestType> MDSCHEMA_CUBES </RequestType><Restrictions><RestrictionList><CATALOG_NAME>Demo</CATALOG_NAME>\
<X><Value>a</Value><Value>b&amp;c</Value></X></RestrictionList></Restrictions>\
<Properties><PropertyList><Catalog>Demo</Catalog><Empty/></PropertyList></Properties></Discover></soap:Body></soap:Envelope>"
        );
        let r = parse_request(x.as_bytes()).unwrap();
        assert_eq!(r.session, Session::Use("abc".into()));
        let Call::Discover { request_type, restrictions, properties } = r.call else { panic!() };
        assert_eq!(request_type, "MDSCHEMA_CUBES");
        assert_eq!(restrictions[0], ("CATALOG_NAME".into(), vec!["Demo".into()]));
        assert_eq!(restrictions[1], ("X".into(), vec!["a".into(), "b&c".into()]));
        assert_eq!(properties, vec![("Catalog".into(), "Demo".into()), ("Empty".into(), String::new())]);
    }

    #[test]
    fn execute_statement_cdata_and_prefixes() {
        let x = format!(
            "<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\"><s:Body><x:Execute xmlns:x=\"{XA}\"><x:Command>\
<x:Statement><![CDATA[SELECT <1> FROM [C]]]></x:Statement></x:Command></x:Execute></s:Body></s:Envelope>"
        );
        let Call::Execute { statement, .. } = parse_request(x.as_bytes()).unwrap().call else { panic!() };
        assert_eq!(statement, "SELECT <1> FROM [C]");
    }

    #[test]
    fn doctype_is_refused() {
        let x = "<?xml version=\"1.0\"?><!DOCTYPE a [<!ENTITY e \"x\">]><a>&e;</a>";
        assert_eq!(parse_request(x.as_bytes()), Err(ParseError::Doctype));
    }

    #[test]
    fn utf16_with_a_bom_is_read_and_its_doctype_refused() {
        let utf16 = |s: &str| -> Vec<u8> { std::iter::once(0xFEFFu16).chain(s.encode_utf16()).flat_map(u16::to_le_bytes).collect() };
        let ok = utf16(&format!("{HEAD}<soap:Body><Discover xmlns=\"{XA}\"><RequestType>X</RequestType></Discover></soap:Body></soap:Envelope>"));
        assert!(matches!(parse_request(&ok).unwrap().call, Call::Discover { .. }));
        let bad = utf16("<?xml version=\"1.0\" encoding=\"utf-16\"?><!DOCTYPE a [<!ENTITY b \"c\">]><a>&b;</a>");
        assert_eq!(parse_request(&bad), Err(ParseError::Doctype));
    }

    #[test]
    fn broken_is_not_xml() {
        assert!(matches!(parse_request(b"<a><b></a>"), Err(ParseError::NotXml(_))));
        assert!(matches!(parse_request(b""), Err(ParseError::NotXml(_))));
    }

    #[test]
    fn sessions() {
        let b = |h: &str| format!("{HEAD}<soap:Header>{h}</soap:Header><soap:Body/></soap:Envelope>");
        let s = |h: &str| parse_request(b(h).as_bytes()).unwrap().session;
        assert_eq!(s(&format!("<BeginSession xmlns=\"{XA}\"/>")), Session::Begin);
        assert_eq!(s(&format!("<EndSession xmlns=\"{XA}\" SessionId=\"q\"/>")), Session::End(Some("q".into())));
    }
}
