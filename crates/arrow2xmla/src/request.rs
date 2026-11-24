//! Parsing of an XMLA request (a SOAP envelope holding a `Discover` or an `Execute`). No DOCTYPE is ever accepted (no
//! entity expansion), and the nesting is bounded. UTF-8 only.

use quick_xml::events::Event;
use quick_xml::XmlVersion;
use quick_xml::name::ResolveResult;
use quick_xml::NsReader;

use crate::XA;

const SOAP: &str = "http://schemas.xmlsoap.org/soap/envelope/";
const MAX_DEPTH: usize = 64;

#[derive(Debug, PartialEq, Eq)]
pub enum ParseError {
    Doctype,
    NotXml(String),
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::Doctype => write!(f, "DOCTYPE is not accepted"),
            ParseError::NotXml(e) => write!(f, "the request is not XML: {e}"),
        }
    }
}

impl std::error::Error for ParseError {}

/// What the SOAP header says about the session.
#[derive(Debug, PartialEq, Eq)]
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

#[derive(Debug, PartialEq, Eq)]
pub enum Call {
    Discover {
        request_type: String,
        /// Name -> values (`<NAME>v</NAME>` or `<NAME><Value>a</Value><Value>b</Value></NAME>`), in order.
        restrictions: Vec<(String, Vec<String>)>,
        properties: Vec<(String, String)>,
    },
    Execute { statement: String, properties: Vec<(String, String)> },
    /// A body with neither.
    Other,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Request {
    pub session: Session,
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
        _ => String::new(),
    };
    let mut el = El { ns, local: e.local_name().as_ref().to_owned(), ..El::default() };
    for a in e.attributes() {
        let a = a.map_err(not_xml)?;
        let key = a.key.as_ref();
        if key == "xmlns" || key.starts_with("xmlns:") {
            continue; // a namespace declaration is not an attribute
        }
        let v = a.normalized_value(XmlVersion::Implicit1_0).map_err(not_xml)?.into_owned();
        el.attrs.push((a.key.local_name().as_ref().to_owned(), v));
    }
    Ok(el)
}

fn text_into(stack: &mut [El], s: &str) {
    if let Some(top) = stack.last_mut() {
        if !top.seen_child {
            top.text.push_str(s); // like ElementTree's `.text`: only what comes before the first child
        }
    }
}

fn close(stack: &mut Vec<El>, root: &mut Option<El>) {
    if let Some(el) = stack.pop() {
        match stack.last_mut() {
            Some(parent) => {
                parent.seen_child = true;
                parent.children.push(el);
            }
            None => *root = Some(el),
        }
    }
}

/// UTF-8 (with or without a BOM) as is; UTF-16 with a BOM is turned into UTF-8 first. Anything else is refused.
fn utf8(body: &[u8]) -> Result<std::borrow::Cow<'_, [u8]>, ParseError> {
    let utf16 = |rest: &[u8], le: bool| -> Result<Vec<u8>, ParseError> {
        if rest.len() % 2 != 0 {
            return Err(ParseError::NotXml("a UTF-16 body of odd length".into()));
        }
        let units: Vec<u16> =
            rest.chunks_exact(2).map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) }).collect();
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
    loop {
        let (ns, ev) = reader.read_resolved_event_into(&mut buf).map_err(not_xml)?;
        match ev {
            Event::DocType(_) => return Err(ParseError::Doctype),
            Event::Start(e) => {
                if stack.len() >= MAX_DEPTH {
                    return Err(ParseError::NotXml("too deeply nested".into()));
                }
                stack.push(open(&ns, &e)?);
            }
            Event::Empty(e) => {
                stack.push(open(&ns, &e)?);
                close(&mut stack, &mut root);
            }
            Event::End(_) => close(&mut stack, &mut root),
            Event::Text(t) => text_into(&mut stack, &t.xml10_content()),
            Event::CData(c) => text_into(&mut stack, &c.xml10_content()),
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
                text_into(&mut stack, &ch);
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
    let root = dom(body)?;
    let session = match root.child(SOAP, "Header") {
        None => Session::None,
        Some(h) if h.child(XA, "BeginSession").is_some() => Session::Begin,
        Some(h) => match (h.child(XA, "EndSession"), h.child(XA, "Session")) {
            (Some(e), _) => Session::End(e.attr("SessionId").map(str::to_owned)),
            (None, Some(s)) => s.attr("SessionId").map_or(Session::None, |id| Session::Use(id.to_owned())),
            (None, None) => Session::None,
        },
    };
    let body_el = root.child(SOAP, "Body");
    let call = if let Some(d) = body_el.and_then(|b| b.child(XA, "Discover")) {
        Call::Discover {
            request_type: d.child(XA, "RequestType").map(|r| r.text.trim().to_owned()).unwrap_or_default(),
            restrictions: list(Some(d), "Restrictions", "RestrictionList").unwrap_or_default(),
            properties: properties(d),
        }
    } else if let Some(x) = body_el.and_then(|b| b.child(XA, "Execute")) {
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
<RequestType> MDSCHEMA_CUBES </RequestType><Restrictions><RestrictionList><CATALOG_NAME>OneView</CATALOG_NAME>\
<X><Value>a</Value><Value>b&amp;c</Value></X></RestrictionList></Restrictions>\
<Properties><PropertyList><Catalog>OneView</Catalog><Empty/></PropertyList></Properties></Discover></soap:Body></soap:Envelope>"
        );
        let r = parse_request(x.as_bytes()).unwrap();
        assert_eq!(r.session, Session::Use("abc".into()));
        let Call::Discover { request_type, restrictions, properties } = r.call else { panic!() };
        assert_eq!(request_type, "MDSCHEMA_CUBES");
        assert_eq!(restrictions[0], ("CATALOG_NAME".into(), vec!["OneView".into()]));
        assert_eq!(restrictions[1], ("X".into(), vec!["a".into(), "b&c".into()]));
        assert_eq!(properties, vec![("Catalog".into(), "OneView".into()), ("Empty".into(), String::new())]);
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
