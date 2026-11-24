//! Arrow in, XMLA out. A generic writer for the XML for Analysis (XMLA) answers an OLAP client such as Excel's
//! PivotTable reads, and a parser for its requests. It knows nothing of any engine or product: callers fill the model
//! (`model`) from their own data and write it, from Rust or through a binding.

mod cellset;
mod display;
mod esc;
mod float;
mod model;
mod request;

pub use display::{display_infos, DRILLED_DOWN, SAME_PARENT};
pub use esc::{escape_into, quote_attr_into};
pub use float::write_f64;
pub use model::*;
pub use request::{parse_request, Call, ParseError, Request, Session};

/// The XMLA namespace.
pub const XA: &str = "urn:schemas-microsoft-com:xml-analysis";

#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    /// A column or array type that is not written.
    Unsupported(String),
    Invalid(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "{e}"),
            Error::Unsupported(s) | Error::Invalid(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}
