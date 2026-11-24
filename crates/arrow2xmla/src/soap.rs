//! The SOAP envelope around an XMLA answer, and the fault that answers a request that failed.

use crate::esc::{escape_into, quote_attr_into};

const SOAP: &str = "http://schemas.xmlsoap.org/soap/envelope/";

/// Which method an answer is for: the response element is `DiscoverResponse` or `ExecuteResponse`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Method {
    /// The answer to a `Discover`: a rowset.
    Discover,
    /// The answer to an `Execute`: a cellset.
    Execute,
}

impl Method {
    fn response(self) -> &'static str {
        match self {
            Self::Discover => "DiscoverResponse",
            Self::Execute => "ExecuteResponse",
        }
    }
}

/// The envelope of an answer. A big answer is sent between `head()` and `tail()` as it is written; a small one is
/// `wrap`ped.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct Envelope {
    /// What the answer is to.
    pub method: Method,
    /// The session id to answer with (the client's own, or a new one for `BeginSession`); none: no session header.
    pub session: Option<String>,
}

impl Envelope {
    /// An envelope for `method` without a session header.
    pub fn new(method: Method) -> Self {
        Self { method, session: None }
    }

    /// The same with a session header.
    pub fn with_session(mut self, id: impl Into<String>) -> Self {
        self.session = Some(id.into());
        self
    }

    /// Everything before the answer: the envelope, the session header, the response element and `<return>`.
    pub fn head(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(format!("<?xml version=\"1.0\" encoding=\"utf-8\"?><soap:Envelope xmlns:soap=\"{SOAP}\" soap:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\">").as_bytes());
        header(&mut out, self.session.as_deref());
        out.extend_from_slice(format!("<soap:Body><{r} xmlns=\"{}\"><return>", crate::XA, r = self.method.response()).as_bytes());
        out
    }

    /// Everything after the answer.
    pub fn tail(&self) -> Vec<u8> {
        format!("</return></{}></soap:Body></soap:Envelope>", self.method.response()).into_bytes()
    }

    /// `head()`, `body` and `tail()` as one.
    pub fn wrap(&self, body: &[u8]) -> Vec<u8> {
        let mut out = self.head();
        out.extend_from_slice(body);
        out.extend_from_slice(&self.tail());
        out
    }
}

fn header(out: &mut Vec<u8>, session: Option<&str>) {
    if let Some(id) = session {
        out.extend_from_slice(format!("<soap:Header><Session xmlns=\"{}\" SessionId=", crate::XA).as_bytes());
        quote_attr_into(out, id);
        out.extend_from_slice(b"/></soap:Header>");
    }
}

/// A SOAP fault in the form Analysis Services gives: `faultcode` `XMLAnalysisError.0x<code>`, the message, and the
/// error in `detail`.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct Fault {
    /// What went wrong, for a person.
    pub message: String,
    /// The error code (an HRESULT-like number; `0xC1000000` and up are server errors).
    pub code: u32,
    /// Who raised it.
    pub source: String,
    /// The session id to answer with, if any.
    pub session: Option<String>,
}

impl Fault {
    /// A fault with the generic server error code and no session.
    pub fn new(message: impl Into<String>) -> Self {
        Self { message: message.into(), code: 0xC110_0000, source: "arrow2xmla".into(), session: None }
    }

    /// The fault as a whole SOAP envelope (to send with HTTP status 500).
    pub fn to_vec(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(format!("<?xml version=\"1.0\" encoding=\"utf-8\"?><soap:Envelope xmlns:soap=\"{SOAP}\" soap:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\">").as_bytes());
        header(&mut out, self.session.as_deref());
        out.extend_from_slice(format!("<soap:Body><soap:Fault><faultcode>XMLAnalysisError.0x{:08x}</faultcode><faultstring>", self.code).as_bytes());
        escape_into(&mut out, &self.message);
        out.extend_from_slice(b"</faultstring><faultactor>Server</faultactor><detail><Error xmlns=\"urn:schemas-microsoft-com:xml-analysis:exception\" ErrorCode=\"");
        out.extend_from_slice(self.code.to_string().as_bytes());
        out.extend_from_slice(b"\" Description=");
        quote_attr_into(&mut out, &self.message);
        out.extend_from_slice(b" Source=");
        quote_attr_into(&mut out, &self.source);
        out.extend_from_slice(b" HelpFile=\"\"/></detail></soap:Fault></soap:Body></soap:Envelope>");
        out
    }
}
