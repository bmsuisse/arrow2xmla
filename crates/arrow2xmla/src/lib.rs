//! Arrow in, XMLA out. A generic writer for the XML for Analysis (XMLA) answers an OLAP client such as Excel's
//! PivotTable reads, and a parser for its requests. It knows nothing of any engine or product: callers fill the model
//! (`model`) from their own data and write it, from Rust or through a binding.

mod array;
mod cellset;
mod display;
mod esc;
mod float;
mod model;
#[cfg(feature = "request")]
mod request;

pub use array::{Array, Validity, Values};
pub use cellset::WriteOptions;
pub use display::{display_infos, DRILLED_DOWN, SAME_PARENT};
pub use esc::{escape_into, quote_attr_into};
pub use float::write_f64;
pub use model::*;
#[cfg(feature = "request")]
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

#[cfg(test)]
mod tests {
    use super::*;

    fn member(h: &str) -> Member {
        Member { hierarchy: h.into(), level_name: format!("{h}.[L]"), level_number: 1, parent_unique_name: format!("{h}.[All]"), member_type: 1, ..Member::default() }
    }

    /// A text array as Arrow lays it out: one data buffer and `n + 1` offsets.
    fn text(values: &[&str]) -> (Vec<i32>, Vec<u8>) {
        let mut offsets = vec![0];
        let mut data = vec![];
        for v in values {
            data.extend_from_slice(v.as_bytes());
            offsets.push(data.len() as i32);
        }
        (offsets, data)
    }

    fn cellset<'a>(offsets: &'a [i32], data: &'a [u8], floats: &'a [f64]) -> Cellset<'a> {
        let mut cs = Cellset::new("C");
        cs.axes.push(Axis {
            source: AxisSource::Level(Box::new(LevelAxis {
                all: None,
                template: member("[T].[F]"),
                keys: Array::new(Values::Utf8 { offsets, data }),
                blank_caption: "(blank)".into(),
            })),
            properties: vec![MemberProp::MemberCaption],
        });
        cs.columns.push(Column { values: Array::new(Values::F64(floats)), format: "0".into() });
        cs
    }

    #[test]
    fn a_small_cellset_has_its_members_and_cells() {
        let (offsets, data) = text(&["a&b", "x]y"]);
        let xml = String::from_utf8(cellset(&offsets, &data, &[1.5, 2.0]).to_vec().unwrap()).unwrap();
        assert!(xml.contains("<Caption>a&amp;b</Caption>"));
        assert!(xml.contains("<UName>[T].[F].&amp;[x]]y]</UName>"));
        assert!(xml.contains("<Cell CellOrdinal=\"1\"><Value xsi:type=\"xsd:double\">2.0</Value><FormatString>0</FormatString></Cell>"));
        assert!(xml.ends_with("</CellData></root>"));
    }

    #[test]
    fn threads_do_not_change_a_byte_and_chunks_join_to_the_whole() {
        let names: Vec<String> = (0..40_000).map(|i| format!("k{i} & <{i}> ]")).collect();
        let refs: Vec<&str> = names.iter().map(String::as_str).collect();
        let (offsets, data) = text(&refs);
        let floats: Vec<f64> = (0..40_000).map(|i| f64::from(i) * 0.37).collect();
        let cs = cellset(&offsets, &data, &floats);
        let run = |threads| {
            let mut chunks = vec![];
            cs.write_chunks(WriteOptions { threads }, &mut |c| {
                chunks.push(c);
                Ok(())
            })
            .unwrap();
            chunks
        };
        let (one, four) = (run(1), run(4));
        assert!(one.len() > 3, "a big answer comes in several chunks");
        assert_eq!(one.concat(), four.concat());
        assert_eq!(one.concat(), cs.to_vec().unwrap());
    }

    #[test]
    fn nulls_are_blank_members_and_missing_cells() {
        let (offsets, data) = text(&["a", ""]);
        let bits = [0b01u8]; // row 1 is null
        let mut cs = cellset(&offsets, &data, &[1.0, 2.0]);
        if let AxisSource::Level(l) = &mut cs.axes[0].source {
            l.keys = Array::new(Values::Utf8 { offsets: &offsets, data: &data }).with_validity(&bits, 0);
        }
        cs.columns[0].values = Array::new(Values::F64(&[1.0, 2.0])).with_validity(&bits, 0);
        let xml = String::from_utf8(cs.to_vec().unwrap()).unwrap();
        assert!(xml.contains("<Caption>(blank)</Caption>"));
        assert!(xml.contains("CellOrdinal=\"0\"") && !xml.contains("CellOrdinal=\"1\""));
    }

    #[test]
    fn malformed_arrays_are_refused_not_read() {
        let data = b"abc";
        let bad = [0, 2, 1]; // offsets go down
        assert!(Array::new(Values::Utf8 { offsets: &bad, data }).check().is_err());
        let far = [0, 9]; // past the data
        assert!(Array::new(Values::Utf8 { offsets: &far, data }).check().is_err());
        let short = [0u8];
        assert!(Array::new(Values::F64(&[1.0; 20])).with_validity(&short, 0).check().is_err());
        let (offsets, d) = text(&["a", "b"]);
        let mut cs = cellset(&offsets, &d, &[1.0, 2.0]);
        cs.columns.push(Column { values: Array::new(Values::F64(&[1.0])), format: "0".into() });
        assert!(cs.to_vec().is_err(), "columns of different lengths are refused");
    }

    #[test]
    fn string_views_read_inline_and_buffered_text() {
        let long = b"a value longer than twelve bytes";
        let mut inline = [0u8; 16];
        inline[..4].copy_from_slice(&3u32.to_le_bytes());
        inline[4..7].copy_from_slice(b"abc");
        let mut buffered = [0u8; 16];
        buffered[..4].copy_from_slice(&(long.len() as u32).to_le_bytes());
        buffered[4..8].copy_from_slice(&long[..4]);
        buffered[12..16].copy_from_slice(&0u32.to_le_bytes());
        let views = [inline, buffered];
        let bufs: [&[u8]; 1] = [long];
        let a = Array::new(Values::Utf8View { views: &views, buffers: &bufs });
        assert_eq!(a.text(0), Some(&b"abc"[..]));
        assert_eq!(a.text(1), Some(&long[..]));
    }
}
