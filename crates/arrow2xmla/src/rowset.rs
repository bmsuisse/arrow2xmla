//! A rowset: the answer to a `Discover` (the cubes, the dimensions, the properties, ...). Columns come from Arrow
//! arrays, like the cells of a cellset, and are written with the schema block clients read the column types from.

use std::io::Write;

use crate::array::{Array, Values};
use crate::esc::{escape_bytes_into, needs_escape};
use crate::float::{f16_to_f64, write_f64};
use crate::Error;

/// The XMLA rowset namespace.
pub const ROWSET_NS: &str = "urn:schemas-microsoft-com:xml-analysis:rowset";

/// One column of a rowset: its name (an XML name: the element each value is written in) and its values.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct RowsetColumn<'a> {
    /// The column's name, e.g. `CUBE_NAME`.
    pub name: String,
    /// One value per row; a null is left out of its row.
    pub values: Array<'a>,
    /// The type declared in the schema block, when the default for the array's type is not wanted (`xsd:boolean`
    /// for text columns holding `true` / `false`).
    pub xsd_type: Option<String>,
}

impl<'a> RowsetColumn<'a> {
    /// A column typed after its array: text `xsd:string`, `Int8` `xsd:byte`, `Int16` `xsd:short`, `Int32` `xsd:int`,
    /// `Int64` `xsd:long`, the unsigned ones `xsd:unsignedByte` / `unsignedShort` / `unsignedInt` / `unsignedLong`,
    /// floats `xsd:double`.
    pub fn new(name: impl Into<String>, values: Array<'a>) -> Self {
        Self { name: name.into(), values, xsd_type: None }
    }

    fn declared_type(&self) -> &str {
        if let Some(t) = &self.xsd_type {
            return t;
        }
        match self.values.values {
            Values::Utf8 { .. } | Values::LargeUtf8 { .. } | Values::Utf8View { .. } => "xsd:string",
            Values::I8(_) => "xsd:byte",
            Values::I16(_) => "xsd:short",
            Values::I32(_) => "xsd:int",
            Values::U8(_) => "xsd:unsignedByte",
            Values::U16(_) => "xsd:unsignedShort",
            Values::I64(_) => "xsd:long",
            Values::U32(_) => "xsd:unsignedInt",
            Values::U64(_) => "xsd:unsignedLong",
            Values::F16(_) | Values::F32(_) | Values::F64(_) => "xsd:double",
        }
    }
}

/// The rows of a `Discover` answer, column by column.
#[derive(Clone, Debug, Default)]
#[non_exhaustive]
pub struct Rowset<'a> {
    /// The columns, in order; all of one length.
    pub columns: Vec<RowsetColumn<'a>>,
}

impl<'a> Rowset<'a> {
    /// A rowset without columns.
    pub fn new() -> Self {
        Self::default()
    }

    /// The rowset with a column added.
    pub fn with_column(mut self, column: RowsetColumn<'a>) -> Self {
        self.columns.push(column);
        self
    }

    /// Whether the rowset can be written: valid names (an XML name that is not repeated), arrays of one length that
    /// are sound.
    pub fn validate(&self) -> Result<(), Error> {
        let rows = self.columns.first().map_or(0, |c| c.values.len());
        for (i, c) in self.columns.iter().enumerate() {
            if !is_name(&c.name) {
                return Err(Error::Invalid(format!("{:?} is not a column name (letters, digits, `_`, `-`, `.`; not starting with a digit)", c.name)));
            }
            if let Some(t) = &c.xsd_type {
                if !t.strip_prefix("xsd:").is_some_and(|n| !n.is_empty() && n.chars().all(|ch| ch.is_ascii_alphabetic())) {
                    return Err(Error::Invalid(format!("{t:?} is not a type (an `xsd:` name such as xsd:boolean)")));
                }
            }
            if self.columns[..i].iter().any(|o| o.name == c.name) {
                return Err(Error::Invalid(format!("column {} twice", c.name)));
            }
            if c.values.len() != rows {
                return Err(Error::Invalid("the columns are not of one length".into()));
            }
            c.values.check()?;
        }
        Ok(())
    }

    /// Writes the rowset (`<root>` with its schema and rows) to `out`.
    pub fn write_to<W: Write>(&self, out: &mut W) -> Result<(), Error> {
        self.validate()?;
        let mut b: Vec<u8> = Vec::with_capacity(8192);
        b.extend_from_slice(format!("<root xmlns=\"{ROWSET_NS}\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">").as_bytes());
        b.extend_from_slice(format!("<xsd:schema targetNamespace=\"{ROWSET_NS}\" xmlns:sql=\"urn:schemas-microsoft-com:xml-sql\" elementFormDefault=\"qualified\">").as_bytes());
        b.extend_from_slice(b"<xsd:element name=\"root\"><xsd:complexType><xsd:sequence minOccurs=\"0\" maxOccurs=\"unbounded\"><xsd:element name=\"row\" type=\"row\"/></xsd:sequence></xsd:complexType></xsd:element>");
        b.extend_from_slice(b"<xsd:simpleType name=\"uuid\"><xsd:restriction base=\"xsd:string\"><xsd:pattern value=\"[0-9a-zA-Z]{8}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{12}\"/></xsd:restriction></xsd:simpleType>");
        b.extend_from_slice(b"<xsd:complexType name=\"xmlDocument\"><xsd:sequence><xsd:any/></xsd:sequence></xsd:complexType><xsd:complexType name=\"row\"><xsd:sequence>");
        for c in &self.columns {
            b.extend_from_slice(format!("<xsd:element sql:field=\"{n}\" name=\"{n}\" type=\"{}\" minOccurs=\"0\"/>", c.declared_type(), n = c.name).as_bytes());
        }
        b.extend_from_slice(b"</xsd:sequence></xsd:complexType></xsd:schema>");
        let rows = self.columns.first().map_or(0, |c| c.values.len());
        for r in 0..rows {
            b.extend_from_slice(b"<row>");
            for c in &self.columns {
                if !c.values.is_valid(r) {
                    continue;
                }
                b.push(b'<');
                b.extend_from_slice(c.name.as_bytes());
                b.push(b'>');
                value_into(&mut b, &c.values, r);
                b.extend_from_slice(b"</");
                b.extend_from_slice(c.name.as_bytes());
                b.push(b'>');
            }
            b.extend_from_slice(b"</row>");
            if b.len() >= 1 << 20 {
                out.write_all(&b)?;
                b.clear();
            }
        }
        b.extend_from_slice(b"</root>");
        out.write_all(&b)?;
        Ok(())
    }

    /// The rowset as bytes.
    pub fn to_vec(&self) -> Result<Vec<u8>, Error> {
        let mut v = Vec::new();
        self.write_to(&mut v)?;
        Ok(v)
    }
}

/// An XML name restricted to what a column needs: ASCII letters, digits, `_`, `-`, `.`, not starting with a digit,
/// `-` or `.`.
fn is_name(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphabetic() || c == '_') && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

fn value_into(b: &mut Vec<u8>, a: &Array<'_>, r: usize) {
    match a.values {
        Values::F64(v) => write_f64(b, v[r]),
        Values::F32(v) => write_f64(b, f64::from(v[r])),
        Values::F16(v) => write_f64(b, f16_to_f64(v[r])),
        Values::I64(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::I32(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::I16(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::I8(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::U64(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::U32(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::U16(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::U8(v) => b.extend_from_slice(itoa::Buffer::new().format(v[r]).as_bytes()),
        Values::Utf8 { .. } | Values::LargeUtf8 { .. } | Values::Utf8View { .. } => {
            if let Some(t) = a.text(r) {
                if needs_escape(t) {
                    escape_bytes_into(b, t);
                } else {
                    b.extend_from_slice(t);
                }
            }
        }
    }
}
