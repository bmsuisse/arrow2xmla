//! Arrow in, XMLA out. A generic writer for the XML for Analysis (XMLA) answers an OLAP client such as Excel's
//! PivotTable reads, and a parser for its requests. It knows nothing of any engine or product: callers fill the model
//! (`model`) from their own data and write it, from Rust or through a binding.

#![warn(missing_docs)]

mod array;
mod cellset;
mod display;
mod esc;
mod float;
mod model;
mod rowset;
mod soap;
#[cfg(feature = "request")]
mod request;

pub use array::{Array, Validity, Values};
pub use cellset::WriteOptions;
pub use model::*;
pub use rowset::{Rowset, RowsetColumn, ROWSET_NS};
pub use soap::{Envelope, Fault, Method};
#[cfg(feature = "request")]
pub use request::{parse_request, parse_request_limited, Call, ParseError, Request, Session, DEFAULT_MAX_REQUEST};

/// The XMLA namespace.
pub const XA: &str = "urn:schemas-microsoft-com:xml-analysis";

/// Why a cellset was not written.
#[derive(Debug)]
#[non_exhaustive]
pub enum Error {
    /// The sink failed.
    Io(std::io::Error),
    /// A column or array type that is not written.
    Unsupported(String),
    /// A cellset that is not valid (shapes that do not match, bad arrays).
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

    #[test]
    fn envelope_wraps_and_streams_the_same_bytes() {
        let e = Envelope::new(Method::Execute).with_session("a\"b&c");
        let whole = String::from_utf8(e.wrap(b"<x/>")).unwrap();
        assert!(whole.contains("<Session xmlns=\"urn:schemas-microsoft-com:xml-analysis\" SessionId='a\"b&amp;c'/>"), "{whole}");
        assert!(whole.contains("<ExecuteResponse xmlns=\"urn:schemas-microsoft-com:xml-analysis\"><return><x/></return></ExecuteResponse>"));
        assert_eq!(e.wrap(b"<x/>"), [e.head(), b"<x/>".to_vec(), e.tail()].concat());
        assert!(!String::from_utf8(Envelope::new(Method::Discover).wrap(b"")).unwrap().contains("<soap:Header>"));
    }

    #[test]
    fn a_fault_escapes_its_message() {
        let mut f = Fault::new("bad <MDX> & \"quotes\"");
        f.code = 0xC10E_0000;
        let xml = String::from_utf8(f.to_vec()).unwrap();
        assert!(xml.contains("<faultcode>XMLAnalysisError.0xc10e0000</faultcode>"));
        assert!(xml.contains("<faultstring>bad &lt;MDX&gt; &amp; \"quotes\"</faultstring>"));
        assert!(xml.contains("ErrorCode=\"3238920192\""));
    }

    #[test]
    fn a_rowset_has_its_schema_and_rows() {
        let (offsets, data) = text(&["Sales", "a&b"]);
        let (fo, fd) = text(&["true", "false"]);
        let rs = Rowset::new()
            .with_column(RowsetColumn::new("CUBE_NAME", Array::new(Values::Utf8 { offsets: &offsets, data: &data })))
            .with_column(RowsetColumn::new("ID", Array::new(Values::I64(&[7, 8])).with_validity(&[0b01], 0)))
            .with_column({
                let mut c = RowsetColumn::new("IS_VIRTUAL", Array::new(Values::Utf8 { offsets: &fo, data: &fd }));
                c.xsd_type = Some("xsd:boolean".into());
                c
            });
        let xml = String::from_utf8(rs.to_vec().unwrap()).unwrap();
        assert!(xml.contains("<xsd:element sql:field=\"CUBE_NAME\" name=\"CUBE_NAME\" type=\"xsd:string\" minOccurs=\"0\"/>"));
        assert!(xml.contains("name=\"ID\" type=\"xsd:long\""));
        assert!(xml.contains("name=\"IS_VIRTUAL\" type=\"xsd:boolean\""));
        assert!(xml.contains("<row><CUBE_NAME>Sales</CUBE_NAME><ID>7</ID><IS_VIRTUAL>true</IS_VIRTUAL></row>"));
        assert!(xml.contains("<row><CUBE_NAME>a&amp;b</CUBE_NAME><IS_VIRTUAL>false</IS_VIRTUAL></row>"), "a null is left out: {xml}");
        assert!(xml.ends_with("</row></root>"));
    }

    #[test]
    fn a_rowset_refuses_bad_names_and_lengths() {
        let one = || Array::new(Values::I64(&[1]));
        for bad in ["", "1A", "A B", "A<", "é"] {
            assert!(Rowset::new().with_column(RowsetColumn::new(bad, one())).validate().is_err(), "{bad:?}");
        }
        assert!(Rowset::new().with_column(RowsetColumn::new("A", one())).with_column(RowsetColumn::new("A", one())).validate().is_err());
        assert!(Rowset::new().with_column(RowsetColumn::new("A", one())).with_column(RowsetColumn::new("B", Array::new(Values::I64(&[1, 2])))).validate().is_err());
        assert!(Rowset::new().to_vec().unwrap().ends_with(b"</root>"), "no columns: no rows");
        for bad in ["", "boolean", "xsd:", "xsd:a\"/><x/>", "xsd:int32", "x sd:int"] {
            let mut c = RowsetColumn::new("A", one());
            c.xsd_type = Some(bad.into());
            assert!(Rowset::new().with_column(c).validate().is_err(), "{bad:?}");
        }
    }

    #[test]
    fn axis_rules() {
        let mut cs = Cellset::new("C");
        cs.columns.push(Column::new(Array::new(Values::F64(&[1.0])), "0"));
        assert!(String::from_utf8(cs.to_vec().unwrap()).unwrap().contains("<Cell CellOrdinal=\"0\">"), "no axes: one cell");
        let mut two = Cellset::new("C");
        two.columns.push(Column::new(Array::new(Values::F64(&[1.0, 2.0])), "0"));
        assert!(two.validate().is_err(), "no axes: two cells");
        let axis = || Axis::tuples(Tuples::new(vec![Member::new("[H]", "[H].[L]", 1)], vec![vec![0]]));
        let mut three = Cellset::new("C");
        three.axes = vec![axis(), axis(), axis()];
        three.columns.push(Column::new(Array::new(Values::F64(&[1.0])), "0"));
        assert!(three.validate().unwrap_err().to_string().contains("at most two axes"));
    }

    #[test]
    fn the_core_refuses_a_negative_level_and_a_member_property_twice() {
        let mut cs = Cellset::new("C");
        cs.slicer.push(Member::new("[H]", "[H].[L]", -1));
        assert!(cs.validate().unwrap_err().to_string().contains("negative"));
        let mut cs = Cellset::new("C");
        let mut axis = Axis::tuples(Tuples::default());
        axis.properties = vec![MemberProp::MemberCaption, MemberProp::MemberCaption];
        cs.axes.push(axis);
        assert!(cs.validate().unwrap_err().to_string().contains("twice"));
    }

    #[test]
    fn a_cell_property_twice_is_refused() {
        let mut cs = Cellset::new("C");
        cs.cell_properties = vec![CellProp::Value, CellProp::Value];
        assert!(cs.validate().is_err());
    }

    #[cfg(feature = "request")]
    #[test]
    fn request_size_limit() {
        assert_eq!(parse_request_limited(b"<a/>", 3), Err(ParseError::TooLarge { limit: 3 }));
        assert!(parse_request_limited(b"<a/>", 4).is_ok());
    }

    fn member(h: &str) -> Member {
        Member { hierarchy: h.into(), level_name: format!("{h}.[L]"), level_number: 1, parent_unique_name: format!("{h}.[All]"), member_type: MemberType::Regular, ..Member::default() }
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

    #[test]
    fn the_axes_must_list_the_cells() {
        let (offsets, data) = text(&["a", "b"]);
        let mut cs = cellset(&offsets, &data, &[1.0, 2.0, 3.0]);
        assert!(cs.to_vec().is_err(), "three cells but two members");
        cs.columns[0].values = Array::new(Values::F64(&[1.0, 2.0]));
        assert!(cs.to_vec().is_ok());
        cs.columns.push(Column { values: Array::new(Values::F64(&[3.0, 4.0])), format: "0".into() });
        assert!(cs.to_vec().is_err(), "two columns need a columns axis");
    }

    #[test]
    fn integers_are_typed_by_their_size() {
        let (offsets, data) = text(&["a", "b", "c"]);
        let mut cs = cellset(&offsets, &data, &[0.0; 3]);
        let big = [5i64, i64::from(i32::MAX) + 1, i64::MIN];
        cs.columns[0].values = Array::new(Values::I64(&big));
        let xml = String::from_utf8(cs.to_vec().unwrap()).unwrap();
        assert!(xml.contains("xsd:int\">5<"));
        assert!(xml.contains("xsd:long\">2147483648<"));
        assert!(xml.contains("xsd:long\">-9223372036854775808<"));
        let huge = [u64::MAX, 1, 2];
        cs.columns[0].values = Array::new(Values::U64(&huge));
        assert!(String::from_utf8(cs.to_vec().unwrap()).unwrap().contains("xsd:unsignedLong\">18446744073709551615<"));
    }

    #[test]
    fn text_that_is_not_utf8_is_refused() {
        let offsets = [0, 2];
        let a = Array::new(Values::Utf8 { offsets: &offsets, data: &[0xFF, 0xFE] });
        assert!(a.check().is_err());
        let surrogate = [0xED, 0xA0, 0x80];
        let a = Array::new(Values::Utf8 { offsets: &[0, 3], data: &surrogate });
        assert!(a.check().is_err());
        assert!(Array::new(Values::Utf8 { offsets: &[0, 2], data: "é".as_bytes() }).check().is_ok());
    }

    #[test]
    fn a_string_view_outside_its_buffer_is_refused() {
        let mut view = [0u8; 16];
        view[..4].copy_from_slice(&50u32.to_le_bytes());
        view[12..16].copy_from_slice(&1000u32.to_le_bytes());
        let bufs: [&[u8]; 1] = [b"abc"];
        let views = [view];
        assert!(Array::new(Values::Utf8View { views: &views, buffers: &bufs }).check().is_err());
        let a = Array::new(Values::Utf8View { views: &views, buffers: &bufs }).with_validity(&[0], 0); // a null: never read
        assert!(a.check().is_ok());
    }

    #[test]
    fn many_writers_at_once_agree() {
        let names: Vec<String> = (0..30_000).map(|i| format!("k{i}")).collect();
        let refs: Vec<&str> = names.iter().map(String::as_str).collect();
        let (offsets, data) = text(&refs);
        let floats: Vec<f64> = (0..30_000).map(|i| f64::from(i) * 0.1).collect();
        let cs = cellset(&offsets, &data, &floats);
        let want = cs.to_vec().unwrap();
        let cs = &cs;
        std::thread::scope(|s| {
            let handles: Vec<_> = (0..12).map(|i| s.spawn(move || cs.write_chunks(WriteOptions::with_threads(i % 5), &mut |_| Ok(())))).collect();
            for h in handles {
                h.join().unwrap().unwrap();
            }
        });
        assert_eq!(cs.to_vec().unwrap(), want);
    }
}
