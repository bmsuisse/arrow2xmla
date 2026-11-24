//! The cellset (`urn:schemas-microsoft-com:xml-analysis:mddataset`) written from the model, streamed in chunks.

use std::io::Write;

use arrow_array::cast::AsArray;
use arrow_array::types::*;
use arrow_array::{Array, ArrayRef};
use arrow_schema::DataType;

use crate::display::{children_bits, display_infos, DRILLED_DOWN};
use crate::esc::{escape_into, quote_attr_into};
use crate::float::write_f64;
use crate::model::{Axis, AxisSource, Cellset, Member, MemberProp};
use crate::{Error, XA};

const FLUSH_AT: usize = 1 << 20; // bytes held before they go to the writer

/// The writer: bytes collect and go out in chunks of about 1 MB.
struct Sink<'a, W: Write> {
    buf: Vec<u8>,
    out: &'a mut W,
}

impl<W: Write> Sink<'_, W> {
    fn maybe_flush(&mut self) -> Result<(), Error> {
        if self.buf.len() >= FLUSH_AT {
            self.out.write_all(&self.buf)?;
            self.buf.clear();
        }
        Ok(())
    }
    fn finish(self) -> Result<(), Error> {
        self.out.write_all(&self.buf)?;
        Ok(())
    }
}

fn lit(b: &mut Vec<u8>, s: &str) {
    b.extend_from_slice(s.as_bytes());
}

fn prop_value(m: &Member, p: MemberProp, out: &mut Vec<u8>) {
    match p {
        MemberProp::ParentUniqueName => escape_into(out, &m.parent_unique_name),
        MemberProp::HierarchyUniqueName => escape_into(out, &m.hierarchy),
        MemberProp::MemberUniqueName => escape_into(out, &m.unique_name),
        MemberProp::MemberCaption => escape_into(out, &m.caption),
        MemberProp::LevelUniqueName => escape_into(out, &m.level_name),
        MemberProp::LevelNumber => lit(out, itoa::Buffer::new().format(m.level_number)),
        MemberProp::ChildrenCardinality => lit(out, itoa::Buffer::new().format(m.children_cardinality)),
        MemberProp::ParentLevel => out.push(b'0'),
        MemberProp::ParentCount => out.push(if m.parent_unique_name.is_empty() { b'0' } else { b'1' }),
        MemberProp::MemberType => lit(out, itoa::Buffer::new().format(m.member_type)),
    }
}

/// A member's XML around its DisplayInfo (the one part that depends on where it stands in the axis).
fn member_parts(m: &Member, props: &[MemberProp]) -> (Vec<u8>, Vec<u8>) {
    let mut head = Vec::with_capacity(256);
    lit(&mut head, "<Member Hierarchy=");
    quote_attr_into(&mut head, &m.hierarchy);
    lit(&mut head, "><UName>");
    escape_into(&mut head, &m.unique_name);
    lit(&mut head, "</UName><Caption>");
    escape_into(&mut head, &m.caption);
    lit(&mut head, "</Caption><LName>");
    escape_into(&mut head, &m.level_name);
    lit(&mut head, "</LName><LNum>");
    lit(&mut head, itoa::Buffer::new().format(m.level_number));
    lit(&mut head, "</LNum><DisplayInfo>");
    let mut tail = Vec::with_capacity(128);
    lit(&mut tail, "</DisplayInfo>");
    for p in props {
        lit(&mut tail, "<");
        lit(&mut tail, p.name());
        lit(&mut tail, ">");
        prop_value(m, *p, &mut tail);
        lit(&mut tail, "</");
        lit(&mut tail, p.name());
        lit(&mut tail, ">");
    }
    lit(&mut tail, "</Member>");
    (head, tail)
}

fn hierarchy_info(out: &mut Vec<u8>, h: &str, props: &[MemberProp]) {
    let mut e = Vec::new();
    escape_into(&mut e, h);
    let e = String::from_utf8(e).expect("utf-8");
    out.extend_from_slice(format!(
        "<HierarchyInfo name=\"{e}\"><UName name=\"{e}.[MEMBER_UNIQUE_NAME]\" type=\"xsd:string\"/>\
<Caption name=\"{e}.[MEMBER_CAPTION]\" type=\"xsd:string\"/><LName name=\"{e}.[LEVEL_UNIQUE_NAME]\" type=\"xsd:string\"/>\
<LNum name=\"{e}.[LEVEL_NUMBER]\" type=\"xsd:int\"/><DisplayInfo name=\"{e}.[DISPLAY_INFO]\" type=\"xsd:unsignedInt\"/>"
    ).as_bytes());
    for p in props {
        out.extend_from_slice(format!("<{n} name=\"{e}.[{n}]\" type=\"xsd:string\"/>", n = p.name()).as_bytes());
    }
    lit(out, "</HierarchyInfo>");
}

/// The hierarchies of an axis in first-seen order (what its AxisInfo lists).
fn axis_hierarchies(axis: &Axis) -> Vec<&str> {
    let mut seen: Vec<&str> = vec![];
    match &axis.source {
        AxisSource::Tuples(t) => {
            if let Some(first) = t.first() {
                for m in first {
                    if !seen.contains(&m.hierarchy.as_str()) {
                        seen.push(&m.hierarchy);
                    }
                }
            }
        }
        AxisSource::Level(l) => {
            if l.all.is_some() || !l.keys.is_empty() {
                seen.push(&l.template.hierarchy);
            }
        }
    }
    seen
}

enum Piece {
    Lit(Vec<u8>),
    Key,
    Caption,
}

const SAME_PARENT_TEXT: &str = "131072"; // SAME_PARENT
const KEY: &[u8] = b"\x01K";
const CAP: &[u8] = b"\x01C";

fn pieces(bytes: &[u8]) -> Vec<Piece> {
    let mut out = vec![];
    let mut cur = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i..].starts_with(KEY) || bytes[i..].starts_with(CAP) {
            if !cur.is_empty() {
                out.push(Piece::Lit(std::mem::take(&mut cur)));
            }
            out.push(if bytes[i + 1] == b'K' { Piece::Key } else { Piece::Caption });
            i += 2;
        } else {
            cur.push(bytes[i]);
            i += 1;
        }
    }
    if !cur.is_empty() {
        out.push(Piece::Lit(cur));
    }
    out
}

fn str_at<'a>(a: &'a ArrayRef) -> Result<Box<dyn Fn(usize) -> Option<&'a str> + 'a>, Error> {
    match a.data_type() {
        DataType::Utf8 => {
            let s = a.as_string::<i32>();
            Ok(Box::new(move |i| if s.is_null(i) { None } else { Some(s.value(i)) }))
        }
        DataType::LargeUtf8 => {
            let s = a.as_string::<i64>();
            Ok(Box::new(move |i| if s.is_null(i) { None } else { Some(s.value(i)) }))
        }
        DataType::Utf8View => {
            let s = a.as_string_view();
            Ok(Box::new(move |i| if s.is_null(i) { None } else { Some(s.value(i)) }))
        }
        t => Err(Error::Unsupported(format!("a level's keys must be a string array, not {t}"))),
    }
}

fn write_axis<W: Write>(s: &mut Sink<W>, n: usize, axis: &Axis) -> Result<(), Error> {
    lit(&mut s.buf, &format!("<Axis name=\"Axis{n}\"><Tuples>"));
    match &axis.source {
        AxisSource::Tuples(tuples) => {
            let infos = display_infos(tuples);
            // each member's XML once, however many tuples hold it
            let mut parts: Vec<Option<(Vec<u8>, Vec<u8>)>> = vec![];
            let mut idx: std::collections::HashMap<&Member, usize> = std::collections::HashMap::new();
            for (t, di) in tuples.iter().zip(&infos) {
                lit(&mut s.buf, "<Tuple>");
                for (m, d) in t.iter().zip(di) {
                    let k = *idx.entry(m).or_insert_with(|| {
                        parts.push(None);
                        parts.len() - 1
                    });
                    let (h, tl) = parts[k].get_or_insert_with(|| member_parts(m, &axis.properties));
                    s.buf.extend_from_slice(h);
                    lit(&mut s.buf, itoa::Buffer::new().format(*d));
                    s.buf.extend_from_slice(tl);
                }
                lit(&mut s.buf, "</Tuple>");
                s.maybe_flush()?;
            }
        }
        AxisSource::Level(l) => {
            if let Some(all) = &l.all {
                let info = children_bits(all) | if l.keys.is_empty() { 0 } else { DRILLED_DOWN };
                let (h, t) = member_parts(all, &axis.properties);
                lit(&mut s.buf, "<Tuple>");
                s.buf.extend_from_slice(&h);
                lit(&mut s.buf, itoa::Buffer::new().format(info));
                s.buf.extend_from_slice(&t);
                lit(&mut s.buf, "</Tuple>");
            }
            let mut proto = l.template.clone();
            proto.unique_name = format!("{}.&[{}]", proto.hierarchy, "\x01K");
            proto.caption = "\x01C".into();
            let (h, t) = member_parts(&proto, &axis.properties);
            let (head, tail) = (pieces(&h), pieces(&t));
            let get = str_at(&l.keys)?;
            let (mut key, mut cap) = (Vec::new(), Vec::new());
            for i in 0..l.keys.len() {
                let v = get(i);
                key.clear();
                cap.clear();
                match v {
                    None => escape_into(&mut cap, &l.blank_caption),
                    Some(text) => {
                        escape_into(&mut cap, text);
                        if text.contains(']') {
                            escape_into(&mut key, &text.replace(']', "]]"));
                        } else {
                            escape_into(&mut key, text);
                        }
                    }
                }
                lit(&mut s.buf, "<Tuple>");
                for p in &head {
                    match p {
                        Piece::Lit(b) => s.buf.extend_from_slice(b),
                        Piece::Key => s.buf.extend_from_slice(&key),
                        Piece::Caption => s.buf.extend_from_slice(&cap),
                    }
                }
                lit(&mut s.buf, if i == 0 { "0" } else { SAME_PARENT_TEXT });
                for p in &tail {
                    match p {
                        Piece::Lit(b) => s.buf.extend_from_slice(b),
                        Piece::Key => s.buf.extend_from_slice(&key),
                        Piece::Caption => s.buf.extend_from_slice(&cap),
                    }
                }
                lit(&mut s.buf, "</Tuple>");
                s.maybe_flush()?;
            }
        }
    }
    lit(&mut s.buf, "</Tuples></Axis>");
    Ok(())
}

/// One column's cells, typed once.
enum Cells<'a> {
    F64(&'a arrow_array::Float64Array),
    F32(&'a arrow_array::Float32Array),
    F16(&'a arrow_array::Float16Array),
    I64(&'a arrow_array::Int64Array),
    I32(&'a arrow_array::Int32Array),
    I16(&'a arrow_array::Int16Array),
    I8(&'a arrow_array::Int8Array),
    U64(&'a arrow_array::UInt64Array),
    U32(&'a arrow_array::UInt32Array),
    U16(&'a arrow_array::UInt16Array),
    U8(&'a arrow_array::UInt8Array),
    Str(Box<dyn Fn(usize) -> Option<&'a str> + 'a>),
}

fn cells_of(a: &ArrayRef) -> Result<Cells<'_>, Error> {
    Ok(match a.data_type() {
        DataType::Float64 => Cells::F64(a.as_primitive::<Float64Type>()),
        DataType::Float32 => Cells::F32(a.as_primitive::<Float32Type>()),
        DataType::Float16 => Cells::F16(a.as_primitive::<Float16Type>()),
        DataType::Int64 => Cells::I64(a.as_primitive::<Int64Type>()),
        DataType::Int32 => Cells::I32(a.as_primitive::<Int32Type>()),
        DataType::Int16 => Cells::I16(a.as_primitive::<Int16Type>()),
        DataType::Int8 => Cells::I8(a.as_primitive::<Int8Type>()),
        DataType::UInt64 => Cells::U64(a.as_primitive::<UInt64Type>()),
        DataType::UInt32 => Cells::U32(a.as_primitive::<UInt32Type>()),
        DataType::UInt16 => Cells::U16(a.as_primitive::<UInt16Type>()),
        DataType::UInt8 => Cells::U8(a.as_primitive::<UInt8Type>()),
        DataType::Utf8 | DataType::LargeUtf8 | DataType::Utf8View => Cells::Str(str_at(a)?),
        t => return Err(Error::Unsupported(format!("a measure column of type {t}"))),
    })
}

macro_rules! int_cell {
    ($a:expr, $i:expr, $buf:expr, $ord:expr, $tail:expr) => {
        if $a.is_null($i) {
            false
        } else {
            open_cell($buf, $ord, "xsd:int");
            lit($buf, itoa::Buffer::new().format($a.value($i)));
            close_cell($buf, $tail);
            true
        }
    };
}

fn open_cell(b: &mut Vec<u8>, ordinal: u64, ty: &str) {
    lit(b, "<Cell CellOrdinal=\"");
    lit(b, itoa::Buffer::new().format(ordinal));
    lit(b, "\"><Value xsi:type=\"");
    lit(b, ty);
    lit(b, "\">");
}

fn close_cell(b: &mut Vec<u8>, tail: &[u8]) {
    lit(b, "</Value>");
    b.extend_from_slice(tail);
    lit(b, "</Cell>");
}

fn write_cells<W: Write>(s: &mut Sink<W>, cs: &Cellset) -> Result<(), Error> {
    let with_value = cs.cell_properties.iter().any(|p| p.name == "VALUE");
    let ncols = cs.columns.len() as u64;
    if ncols == 0 {
        return Ok(());
    }
    let rows = cs.columns[0].values.len();
    if cs.columns.iter().any(|c| c.values.len() != rows) {
        return Err(Error::Invalid("the columns are not of one length".into()));
    }
    // per column: its cell's other properties (only those asked for, in order)
    let tails: Vec<Vec<u8>> = cs
        .columns
        .iter()
        .map(|c| {
            let mut t = Vec::new();
            for p in &cs.cell_properties {
                let value = match p.name.as_str() {
                    "FORMAT_STRING" => {
                        let mut e = Vec::new();
                        escape_into(&mut e, &c.format);
                        e
                    }
                    "LANGUAGE" => cs.language.to_string().into_bytes(),
                    _ => continue,
                };
                lit(&mut t, &format!("<{}>", p.element));
                t.extend_from_slice(&value);
                lit(&mut t, &format!("</{}>", p.element));
            }
            t
        })
        .collect();
    let typed: Vec<Cells> = cs.columns.iter().map(|c| cells_of(&c.values)).collect::<Result<_, _>>()?;
    let mut tmp = Vec::new();
    for r in 0..rows {
        for (c, col) in typed.iter().enumerate() {
            let ord = r as u64 * ncols + c as u64;
            let tail = &tails[c][..];
            let b = &mut s.buf;
            if !with_value {
                // a cell with no value property: written for every row that has a value
                let present = match col {
                    Cells::F64(a) => !a.is_null(r),
                    Cells::F32(a) => !a.is_null(r),
                    Cells::F16(a) => !a.is_null(r),
                    Cells::I64(a) => !a.is_null(r),
                    Cells::I32(a) => !a.is_null(r),
                    Cells::I16(a) => !a.is_null(r),
                    Cells::I8(a) => !a.is_null(r),
                    Cells::U64(a) => !a.is_null(r),
                    Cells::U32(a) => !a.is_null(r),
                    Cells::U16(a) => !a.is_null(r),
                    Cells::U8(a) => !a.is_null(r),
                    Cells::Str(g) => g(r).is_some(),
                };
                if present {
                    lit(b, "<Cell CellOrdinal=\"");
                    lit(b, itoa::Buffer::new().format(ord));
                    lit(b, "\">");
                    b.extend_from_slice(tail);
                    lit(b, "</Cell>");
                }
                continue;
            }
            match col {
                Cells::F64(a) => {
                    if !a.is_null(r) {
                        open_cell(b, ord, "xsd:double");
                        write_f64(b, a.value(r));
                        close_cell(b, tail);
                    }
                }
                Cells::F32(a) => {
                    if !a.is_null(r) {
                        open_cell(b, ord, "xsd:double");
                        write_f64(b, f64::from(a.value(r)));
                        close_cell(b, tail);
                    }
                }
                Cells::F16(a) => {
                    if !a.is_null(r) {
                        open_cell(b, ord, "xsd:double");
                        write_f64(b, a.value(r).to_f64());
                        close_cell(b, tail);
                    }
                }
                Cells::I64(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::I32(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::I16(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::I8(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::U64(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::U32(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::U16(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::U8(a) => {
                    int_cell!(a, r, b, ord, tail);
                }
                Cells::Str(g) => {
                    if let Some(text) = g(r) {
                        open_cell(b, ord, "xsd:string");
                        tmp.clear();
                        escape_into(&mut tmp, text);
                        b.extend_from_slice(&tmp);
                        close_cell(b, tail);
                    }
                }
            }
        }
        s.maybe_flush()?;
    }
    Ok(())
}

impl Cellset {
    /// Writes the cellset to `out` in chunks (an answer is sent while it is built, never held whole).
    pub fn write_to<W: Write>(&self, out: &mut W) -> Result<(), Error> {
        let mut s = Sink { buf: Vec::with_capacity(FLUSH_AT + (FLUSH_AT >> 2)), out };
        let b = &mut s.buf;
        lit(b, &format!(
            "<root xmlns=\"{XA}:mddataset\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">\
<OlapInfo><CubeInfo><Cube><CubeName>"
        ));
        escape_into(b, &self.cube_name);
        lit(b, "</CubeName></Cube></CubeInfo><AxesInfo>");
        for (n, axis) in self.axes.iter().enumerate() {
            lit(b, &format!("<AxisInfo name=\"Axis{n}\">"));
            for h in axis_hierarchies(axis) {
                hierarchy_info(b, h, &axis.properties);
            }
            lit(b, "</AxisInfo>");
        }
        lit(b, "<AxisInfo name=\"SlicerAxis\">");
        let mut seen: Vec<&str> = vec![];
        for m in &self.slicer {
            if !seen.contains(&m.hierarchy.as_str()) {
                seen.push(&m.hierarchy);
                hierarchy_info(b, &m.hierarchy, &[]);
            }
        }
        lit(b, "</AxisInfo></AxesInfo><CellInfo>");
        for p in &self.cell_properties {
            lit(b, &format!("<{} name=\"{}\"", p.element, p.name));
            if let Some(t) = &p.xsd_type {
                lit(b, &format!(" type=\"{t}\""));
            }
            lit(b, "/>");
        }
        lit(b, "</CellInfo></OlapInfo><Axes>");
        for (n, axis) in self.axes.iter().enumerate() {
            write_axis(&mut s, n, axis)?;
        }
        let b = &mut s.buf;
        if self.slicer.is_empty() {
            lit(b, "<Axis name=\"SlicerAxis\"><Tuples/></Axis>");
        } else {
            lit(b, "<Axis name=\"SlicerAxis\"><Tuples><Tuple>");
            for m in &self.slicer {
                let (h, t) = member_parts(m, &[]);
                b.extend_from_slice(&h);
                b.push(b'0');
                b.extend_from_slice(&t);
            }
            lit(b, "</Tuple></Tuples></Axis>");
        }
        lit(b, "</Axes><CellData>");
        write_cells(&mut s, self)?;
        lit(&mut s.buf, "</CellData></root>");
        s.finish()
    }

    pub fn to_vec(&self) -> Result<Vec<u8>, Error> {
        let mut v = Vec::new();
        self.write_to(&mut v)?;
        Ok(v)
    }
}
