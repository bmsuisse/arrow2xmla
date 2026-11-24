//! The cellset (`urn:schemas-microsoft-com:xml-analysis:mddataset`) written from the model, in chunks. The two big
//! parts, the members of a level axis and the cells, are cut into ranges of rows that threads render at once and the
//! writer hands on in order, so a large answer scales with the cores and is never held whole by this crate.

use std::io::Write;
use std::ops::Range;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use crate::array::{Array, Values};
use crate::display::{children_bits, display_infos, DRILLED_DOWN};
use crate::esc::{escape_attr_into, escape_bytes_into, escape_into, needs_escape, quote_attr_into};
use crate::float::write_f64;
use crate::model::{Axis, AxisSource, Cellset, Member, MemberProp};
use crate::{Error, XA};

const FLUSH_AT: usize = 1 << 20; // bytes of small parts held before they go out as a chunk
const PAR_MIN_ROWS: usize = 16 * 1024; // below this a single thread is faster than the hand-over
const LEVEL_CHUNK_ROWS: usize = 2048; // rows of a level axis per chunk: about 1 MB
const CELL_CHUNK_CELLS: usize = 12 * 1024; // cells per chunk: about 1 MB
const SAME_PARENT_TEXT: &[u8] = b"131072"; // SAME_PARENT

/// How a cellset is written.
#[derive(Clone, Copy, Debug, Default)]
pub struct WriteOptions {
    /// Threads that render at once; 0 picks the machine's (at most 8). 1 keeps everything on the calling thread.
    pub threads: usize,
}

impl WriteOptions {
    fn threads(self) -> usize {
        if self.threads > 0 {
            self.threads
        } else {
            std::thread::available_parallelism().map_or(1, |n| n.get()).min(8)
        }
    }
}

type Sink<'s> = &'s mut dyn FnMut(Vec<u8>) -> Result<(), Error>;

/// Small parts collect in `pending`; the big ones go out as chunks of their own, in order.
struct Writer<'s> {
    pending: Vec<u8>,
    sink: Sink<'s>,
    threads: usize,
}

impl Writer<'_> {
    fn flush(&mut self) -> Result<(), Error> {
        if !self.pending.is_empty() {
            // a fresh buffer of the same room, so the next small parts do not grow it from nothing
            let room = self.pending.capacity().min(FLUSH_AT + (FLUSH_AT >> 2));
            let chunk = std::mem::replace(&mut self.pending, Vec::with_capacity(room));
            (self.sink)(chunk)?;
        }
        Ok(())
    }

    fn maybe_flush(&mut self) -> Result<(), Error> {
        if self.pending.len() >= FLUSH_AT {
            self.flush()?;
        }
        Ok(())
    }

    /// Renders rows `0..n` in chunks of `rows_per_chunk` (a chunk is `f(range, buf)`), on several threads when there
    /// are enough rows, and passes the chunks on in row order.
    fn parallel<F>(&mut self, n: usize, rows_per_chunk: usize, bytes_per_row: usize, f: F) -> Result<(), Error>
    where
        F: Fn(Range<usize>, &mut Vec<u8>) + Sync,
    {
        self.flush()?;
        let chunks = n.div_ceil(rows_per_chunk);
        let range = |c: usize| c * rows_per_chunk..((c + 1) * rows_per_chunk).min(n);
        let threads = self.threads.min(chunks);
        if threads <= 1 || n < PAR_MIN_ROWS {
            for c in 0..chunks {
                let mut buf = Vec::with_capacity(rows_per_chunk * bytes_per_row);
                f(range(c), &mut buf);
                (self.sink)(buf)?;
            }
            return Ok(());
        }
        let wave = threads * 4; // chunks in flight at once: bounds what is held
        let mut first = 0;
        while first < chunks {
            let last = (first + wave).min(chunks);
            let next = AtomicUsize::new(first);
            let done: Vec<Mutex<Option<Vec<u8>>>> = (first..last).map(|_| Mutex::new(None)).collect();
            std::thread::scope(|s| {
                for _ in 0..threads {
                    s.spawn(|| loop {
                        let c = next.fetch_add(1, Ordering::Relaxed);
                        if c >= last {
                            break;
                        }
                        let mut buf = Vec::with_capacity(rows_per_chunk * bytes_per_row);
                        f(range(c), &mut buf);
                        *done[c - first].lock().expect("a worker panicked") = Some(buf);
                    });
                }
            });
            for slot in done {
                (self.sink)(slot.into_inner().expect("a worker panicked").expect("every chunk is rendered"))?;
            }
            first = last;
        }
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
    escape_attr_into(&mut e, h);
    let e = String::from_utf8(e).expect("utf-8");
    out.extend_from_slice(
        format!(
            "<HierarchyInfo name=\"{e}\"><UName name=\"{e}.[MEMBER_UNIQUE_NAME]\" type=\"xsd:string\"/>\
<Caption name=\"{e}.[MEMBER_CAPTION]\" type=\"xsd:string\"/><LName name=\"{e}.[LEVEL_UNIQUE_NAME]\" type=\"xsd:string\"/>\
<LNum name=\"{e}.[LEVEL_NUMBER]\" type=\"xsd:int\"/><DisplayInfo name=\"{e}.[DISPLAY_INFO]\" type=\"xsd:unsignedInt\"/>"
        )
        .as_bytes(),
    );
    for p in props {
        out.extend_from_slice(format!("<{n} name=\"{e}.[{n}]\" type=\"xsd:string\"/>", n = p.name()).as_bytes());
    }
    lit(out, "</HierarchyInfo>");
}

/// The hierarchies of an axis in first-seen order (what its AxisInfo lists).
fn axis_hierarchies<'a>(axis: &'a Axis<'_>) -> Vec<&'a str> {
    let mut seen: Vec<&str> = vec![];
    match &axis.source {
        AxisSource::Tuples(t) => {
            if let Some(first) = t.tuples.first() {
                for &k in first {
                    if let Some(m) = t.members.get(k as usize) {
                        if !seen.contains(&m.hierarchy.as_str()) {
                            seen.push(&m.hierarchy);
                        }
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

/// A member's text cut where its key and caption go, so each value only fills two holes.
enum Piece {
    Lit(Vec<u8>),
    Key,
    Caption,
}

/// The pieces of a level's members: what `member_parts` writes for a member whose unique name is
/// `<hierarchy>.&[<key>]` and whose caption is the value's, built part by part (no text is searched for holes, so
/// no text of a caller's can be mistaken for one). Before the DisplayInfo, and after it.
fn level_pieces(m: &Member, props: &[MemberProp]) -> (Vec<Piece>, Vec<Piece>) {
    fn push_lit(ps: &mut Vec<Piece>, bytes: &[u8]) {
        match ps.last_mut() {
            Some(Piece::Lit(l)) => l.extend_from_slice(bytes),
            _ => ps.push(Piece::Lit(bytes.to_vec())),
        }
    }
    fn unique(ps: &mut Vec<Piece>, h: &str) {
        let mut e = Vec::new();
        escape_into(&mut e, h);
        e.extend_from_slice(b".&amp;[");
        push_lit(ps, &e);
        ps.push(Piece::Key);
        push_lit(ps, b"]");
    }
    let mut head = vec![];
    let esc = |s: &str| {
        let mut e = Vec::new();
        escape_into(&mut e, s);
        e
    };
    let mut lit_head = Vec::from(&b"<Tuple><Member Hierarchy="[..]);
    quote_attr_into(&mut lit_head, &m.hierarchy);
    lit_head.extend_from_slice(b"><UName>");
    push_lit(&mut head, &lit_head);
    unique(&mut head, &m.hierarchy);
    push_lit(&mut head, b"</UName><Caption>");
    head.push(Piece::Caption);
    let mut rest = Vec::from(&b"</Caption><LName>"[..]);
    rest.extend_from_slice(&esc(&m.level_name));
    rest.extend_from_slice(b"</LName><LNum>");
    rest.extend_from_slice(itoa::Buffer::new().format(m.level_number).as_bytes());
    rest.extend_from_slice(b"</LNum><DisplayInfo>");
    push_lit(&mut head, &rest);

    let mut tail = vec![];
    push_lit(&mut tail, b"</DisplayInfo>");
    for p in props {
        push_lit(&mut tail, format!("<{}>", p.name()).as_bytes());
        match p {
            MemberProp::MemberUniqueName => unique(&mut tail, &m.hierarchy),
            MemberProp::MemberCaption => tail.push(Piece::Caption),
            other => {
                let mut v = Vec::new();
                prop_value(m, *other, &mut v);
                push_lit(&mut tail, &v);
            }
        }
        push_lit(&mut tail, format!("</{}>", p.name()).as_bytes());
    }
    push_lit(&mut tail, b"</Member></Tuple>");
    (head, tail)
}

/// What a text needs to become XML text: nothing, escaping, and whether it holds `]` (doubled in a key).
#[inline]
fn classify(t: &[u8]) -> (bool, bool) {
    let (mut special, mut bracket) = (false, false);
    for &b in t {
        special |= matches!(b, b'&' | b'<' | b'>' | 0x00..=0x08 | 0x0B | 0x0C | 0x0E..=0x1F | 0xEF);
        bracket |= b == b']';
    }
    (special, bracket)
}

fn write_axis(w: &mut Writer<'_>, n: usize, axis: &Axis<'_>) -> Result<(), Error> {
    lit(&mut w.pending, &format!("<Axis name=\"Axis{n}\"><Tuples>"));
    match &axis.source {
        AxisSource::Tuples(t) => {
            let width = t.tuples.first().map_or(0, Vec::len);
            if t.tuples.iter().any(|x| x.len() != width || x.iter().any(|&k| k as usize >= t.members.len())) {
                return Err(Error::Invalid("tuples of different widths, or a member index out of range".into()));
            }
            let infos = display_infos(&t.members, &t.tuples);
            // each member's XML once, however many tuples hold it
            let mut parts: Vec<Option<(Vec<u8>, Vec<u8>)>> = vec![None; t.members.len()];
            for (i, tuple) in t.tuples.iter().enumerate() {
                lit(&mut w.pending, "<Tuple>");
                for (j, &k) in tuple.iter().enumerate() {
                    let k = k as usize;
                    let (h, tl) = parts[k].get_or_insert_with(|| member_parts(&t.members[k], &axis.properties));
                    w.pending.extend_from_slice(h);
                    lit(&mut w.pending, itoa::Buffer::new().format(infos[i * width + j]));
                    w.pending.extend_from_slice(tl);
                }
                lit(&mut w.pending, "</Tuple>");
                w.maybe_flush()?;
            }
        }
        AxisSource::Level(l) => {
            if let Some(all) = &l.all {
                let info = children_bits(all) | if l.keys.is_empty() { 0 } else { DRILLED_DOWN };
                let (h, t) = member_parts(all, &axis.properties);
                lit(&mut w.pending, "<Tuple>");
                w.pending.extend_from_slice(&h);
                lit(&mut w.pending, itoa::Buffer::new().format(info));
                w.pending.extend_from_slice(&t);
                lit(&mut w.pending, "</Tuple>");
            }
            let (head, tail) = level_pieces(&l.template, &axis.properties);
            let mut blank = Vec::new();
            escape_into(&mut blank, &l.blank_caption);
            let keys = l.keys;
            w.parallel(keys.len(), LEVEL_CHUNK_ROWS, 450, |range, out| {
                let (mut key, mut cap) = (Vec::new(), Vec::new());
                for i in range {
                    let (key_b, cap_b): (&[u8], &[u8]) = match keys.text(i) {
                        None => (b"", &blank),
                        Some(text) => match classify(text) {
                            (false, false) => (text, text),
                            (special, bracket) => {
                                cap.clear();
                                if special {
                                    escape_bytes_into(&mut cap, text);
                                } else {
                                    cap.extend_from_slice(text);
                                }
                                if bracket {
                                    // the key: `]` doubled, then escaped (a `]` is not special to XML, so each run
                                    // between two `]` is escaped as it is)
                                    key.clear();
                                    for (n, part) in text.split(|&b| b == b']').enumerate() {
                                        if n > 0 {
                                            key.extend_from_slice(b"]]");
                                        }
                                        escape_bytes_into(&mut key, part);
                                    }
                                    (&key, &cap)
                                } else {
                                    (&cap, &cap)
                                }
                            }
                        },
                    };
                    let fill = |out: &mut Vec<u8>, ps: &[Piece]| {
                        for p in ps {
                            match p {
                                Piece::Lit(b) => out.extend_from_slice(b),
                                Piece::Key => out.extend_from_slice(key_b),
                                Piece::Caption => out.extend_from_slice(cap_b),
                            }
                        }
                    };
                    fill(out, &head);
                    out.extend_from_slice(if i == 0 { b"0" } else { SAME_PARENT_TEXT });
                    fill(out, &tail);
                }
            })?;
        }
    }
    lit(&mut w.pending, "</Tuples></Axis>");
    Ok(())
}

/// One column of cells, ready to write.
struct Col<'a> {
    array: Array<'a>,
    /// The cell's other properties (only those asked for, in order), after its value.
    tail: Vec<u8>,
}

#[inline]
fn open_cell(b: &mut Vec<u8>, ordinal: u64, ty: &[u8]) {
    b.extend_from_slice(b"<Cell CellOrdinal=\"");
    b.extend_from_slice(itoa::Buffer::new().format(ordinal).as_bytes());
    b.extend_from_slice(b"\"><Value xsi:type=\"");
    b.extend_from_slice(ty);
    b.extend_from_slice(b"\">");
}

#[inline]
fn close_cell(b: &mut Vec<u8>, tail: &[u8]) {
    b.extend_from_slice(b"</Value>");
    b.extend_from_slice(tail);
    b.extend_from_slice(b"</Cell>");
}

macro_rules! int_cell {
    ($vals:expr, $r:expr, $b:expr, $ord:expr, $tail:expr) => {{
        open_cell($b, $ord, b"xsd:int");
        $b.extend_from_slice(itoa::Buffer::new().format($vals[$r]).as_bytes());
        close_cell($b, $tail);
    }};
}

/// The cell at `(row, column)` when it has a value. With `with_value` false the cell carries only its other
/// properties.
fn write_cell(b: &mut Vec<u8>, ord: u64, col: &Col<'_>, r: usize, with_value: bool) {
    if !col.array.is_valid(r) {
        return;
    }
    if !with_value {
        if matches!(col.array.values, Values::Utf8 { .. } | Values::LargeUtf8 { .. } | Values::Utf8View { .. }) && col.array.text(r).is_none() {
            return;
        }
        b.extend_from_slice(b"<Cell CellOrdinal=\"");
        b.extend_from_slice(itoa::Buffer::new().format(ord).as_bytes());
        b.extend_from_slice(b"\">");
        b.extend_from_slice(&col.tail);
        b.extend_from_slice(b"</Cell>");
        return;
    }
    let tail = &col.tail[..];
    match col.array.values {
        Values::F64(v) => {
            open_cell(b, ord, b"xsd:double");
            write_f64(b, v[r]);
            close_cell(b, tail);
        }
        Values::F32(v) => {
            open_cell(b, ord, b"xsd:double");
            write_f64(b, f64::from(v[r]));
            close_cell(b, tail);
        }
        Values::I64(v) => int_cell!(v, r, b, ord, tail),
        Values::I32(v) => int_cell!(v, r, b, ord, tail),
        Values::I16(v) => int_cell!(v, r, b, ord, tail),
        Values::I8(v) => int_cell!(v, r, b, ord, tail),
        Values::U64(v) => int_cell!(v, r, b, ord, tail),
        Values::U32(v) => int_cell!(v, r, b, ord, tail),
        Values::U16(v) => int_cell!(v, r, b, ord, tail),
        Values::U8(v) => int_cell!(v, r, b, ord, tail),
        Values::Utf8 { .. } | Values::LargeUtf8 { .. } | Values::Utf8View { .. } => {
            if let Some(text) = col.array.text(r) {
                open_cell(b, ord, b"xsd:string");
                if needs_escape(text) {
                    escape_bytes_into(b, text);
                } else {
                    b.extend_from_slice(text);
                }
                close_cell(b, tail);
            }
        }
    }
}

fn write_cells(w: &mut Writer<'_>, cs: &Cellset<'_>) -> Result<(), Error> {
    let with_value = cs.cell_properties.iter().any(|p| p.name == "VALUE");
    let ncols = cs.columns.len();
    if ncols == 0 {
        return Ok(());
    }
    let rows = cs.columns[0].values.len();
    if cs.columns.iter().any(|c| c.values.len() != rows) {
        return Err(Error::Invalid("the columns are not of one length".into()));
    }
    let cols: Vec<Col<'_>> = cs
        .columns
        .iter()
        .map(|c| {
            c.values.check()?;
            let mut tail = Vec::new();
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
                lit(&mut tail, &format!("<{}>", p.element));
                tail.extend_from_slice(&value);
                lit(&mut tail, &format!("</{}>", p.element));
            }
            Ok(Col { array: c.values, tail })
        })
        .collect::<Result<_, Error>>()?;
    let rows_per_chunk = (CELL_CHUNK_CELLS / ncols).max(256);
    w.parallel(rows, rows_per_chunk, ncols * 90, |range, out| {
        for r in range {
            for (c, col) in cols.iter().enumerate() {
                write_cell(out, (r * ncols + c) as u64, col, r, with_value);
            }
        }
    })
}

impl Cellset<'_> {
    /// Writes the cellset as a series of chunks of about 1 MB, each handed to `sink` in order.
    pub fn write_chunks(&self, opts: WriteOptions, sink: &mut dyn FnMut(Vec<u8>) -> Result<(), Error>) -> Result<(), Error> {
        for axis in &self.axes {
            if let AxisSource::Level(l) = &axis.source {
                l.keys.check()?;
                if !l.keys.is_text() {
                    return Err(Error::Unsupported("a level's keys must be a text array".into()));
                }
            }
        }
        let mut w = Writer { pending: Vec::with_capacity(FLUSH_AT + (FLUSH_AT >> 2)), sink, threads: opts.threads() };
        let b = &mut w.pending;
        lit(
            b,
            &format!(
                "<root xmlns=\"{XA}:mddataset\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">\
<OlapInfo><CubeInfo><Cube><CubeName>"
            ),
        );
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
            write_axis(&mut w, n, axis)?;
        }
        let b = &mut w.pending;
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
        write_cells(&mut w, self)?;
        lit(&mut w.pending, "</CellData></root>");
        w.flush()
    }

    /// Writes the cellset to `out`.
    pub fn write_to<W: Write>(&self, out: &mut W) -> Result<(), Error> {
        self.write_chunks(WriteOptions::default(), &mut |chunk| Ok(out.write_all(&chunk)?))
    }

    pub fn to_vec(&self) -> Result<Vec<u8>, Error> {
        let mut v = Vec::new();
        self.write_to(&mut v)?;
        Ok(v)
    }
}
