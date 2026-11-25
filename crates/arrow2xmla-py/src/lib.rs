//! Python binding: `arrow2xmla._native.cellset_xml(cellset, threads)`. The cellset is an `arrow2xmla._model.Cellset`
//! (read by attribute); its arrays come through the Arrow C data interface, no copy.

use std::ffi::{c_char, c_void, CStr};

use arrow2xmla::{Array, Axis, CellProp, Cellset, Column, Envelope, Fault, LevelAxis, Member, MemberProp, MemberType, Method, Rowset, RowsetColumn, Tuples, Values, WriteOptions};
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyCapsule, PyCapsuleMethods};

// The exceptions; they are a hierarchy under ValueError, which the Python package re-exports.
create_exception!(_native, XmlaError, PyValueError, "Anything this package refuses.");
create_exception!(_native, CellsetError, XmlaError, "A cellset or rowset that is not valid: wrong shapes, bad arrays, unknown names.");
create_exception!(_native, UnsupportedTypeError, CellsetError, "An Arrow type that is not written.");
create_exception!(_native, RequestError, XmlaError, "A request that is not accepted.");
create_exception!(_native, DoctypeError, RequestError, "A request with a DOCTYPE.");
create_exception!(_native, RequestTooLarge, RequestError, "A request longer than max_size.");

// The Arrow C data interface (https://arrow.apache.org/docs/format/CDataInterface.html), read through the PyCapsule
// protocol (`__arrow_c_array__`): any Arrow-speaking library's arrays work, none is a dependency.
#[repr(C)]
struct ArrowSchema {
    format: *const c_char,
    name: *const c_char,
    metadata: *const c_char,
    flags: i64,
    n_children: i64,
    children: *mut *mut ArrowSchema,
    dictionary: *mut ArrowSchema,
    release: Option<unsafe extern "C" fn(*mut ArrowSchema)>,
    private_data: *mut c_void,
}

#[repr(C)]
struct ArrowArray {
    length: i64,
    null_count: i64,
    offset: i64,
    n_buffers: i64,
    n_children: i64,
    buffers: *mut *const c_void,
    children: *mut *mut ArrowArray,
    dictionary: *mut ArrowArray,
    release: Option<unsafe extern "C" fn(*mut ArrowArray)>,
    private_data: *mut c_void,
}

/// What must outlive the views built from the capsules: the capsules themselves (they free the arrays), and the
/// lists of data buffers a string-view array needs.
#[derive(Default)]
struct Keep {
    capsules: Vec<Py<PyAny>>,
    buffer_lists: Vec<Box<[&'static [u8]]>>,
}

fn bad(msg: impl Into<String>) -> PyErr {
    CellsetError::new_err(msg.into())
}

/// `n` elements of `T` at `ptr` (an empty slice for 0; a null or misaligned pointer is an error otherwise).
unsafe fn slice<'a, T>(ptr: *const c_void, n: usize, what: &str) -> PyResult<&'a [T]> {
    if n == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(bad(format!("an Arrow array without its {what} buffer")));
    }
    if !(ptr as usize).is_multiple_of(std::mem::align_of::<T>()) {
        return Err(bad(format!("an Arrow {what} buffer that is not aligned")));
    }
    // a slice cannot hold more than isize::MAX bytes: a length like that is a producer's mistake
    if n.checked_mul(std::mem::size_of::<T>()).is_none_or(|bytes| bytes > isize::MAX as usize) {
        return Err(bad(format!("an Arrow array too long for its {what} buffer")));
    }
    Ok(std::slice::from_raw_parts(ptr.cast::<T>(), n))
}

/// A count from the C structs (an `i64`) as a size: never negative.
fn count(v: i64, what: &str) -> PyResult<usize> {
    usize::try_from(v).map_err(|_| bad(format!("an Arrow array with a negative {what}")))
}

fn import<'a>(obj: &Bound<'_, PyAny>, keep: &mut Keep) -> PyResult<Array<'a>> {
    // a column this module read from a stream (`read_stream`) owns its buffers
    if let Ok(col) = obj.extract::<Py<ArrowColumn>>() {
        let r: &ArrowColumn = col.get();
        // SAFETY: the column is frozen and lives as long as `keep` holds it, which outlives every view
        let r: &'a ArrowColumn = unsafe { &*(r as *const ArrowColumn) };
        keep.capsules.push(col.into_any());
        return Ok(r.array());
    }
    let method = obj.getattr("__arrow_c_array__").map_err(|_| {
        bad(format!(
            "expected an Arrow array (an object with __arrow_c_array__, such as a pyarrow Array), not {}",
            obj.get_type().name().map_or_else(|_| "this".to_string(), |n| format!("a {n}"))
        ))
    })?;
    let (schema_c, array_c): (Bound<'_, PyCapsule>, Bound<'_, PyCapsule>) = method.call0()?.extract()?;
    let schema = schema_c.pointer_checked(Some(c"arrow_schema"))?.as_ptr().cast::<ArrowSchema>();
    let array = array_c.pointer_checked(Some(c"arrow_array"))?.as_ptr().cast::<ArrowArray>();
    keep.capsules.push(schema_c.into_any().unbind());
    keep.capsules.push(array_c.into_any().unbind());
    // SAFETY: the capsules hold C data interface structs (the protocol's contract) and stay alive in `keep`. What a
    // producer got wrong that can be seen in the structs is refused below; the buffers are trusted to be as long as
    // their array says (the interface has no way to say more).
    let (schema, array) = unsafe { (&*schema, &*array) };
    if schema.release.is_none() || array.release.is_none() {
        return Err(bad("an Arrow array that was already released"));
    }
    if !schema.format.is_null() {
        // SAFETY: a format string of a schema that has not been released
        if let Ok(format) = unsafe { CStr::from_ptr(schema.format) }.to_str() {
            if parse_decimal(format).is_some() {
                // a decimal array is read as `columns()` reads one: an int64 when whole and it fits, else a double
                let mut acc = Acc::new(format, "")?;
                acc.push_decimal(array)?;
                let col = Py::new(obj.py(), acc.finish())?;
                let r: &ArrowColumn = col.get();
                // SAFETY: as above: the column is frozen and `keep` holds it
                let r: &'a ArrowColumn = unsafe { &*(r as *const ArrowColumn) };
                keep.capsules.push(col.into_any());
                return Ok(r.array());
            }
        }
    }
    import_raw(schema, array, keep)
}

/// A view of an array given as C data interface structs (checked as far as the structs show). The buffers are trusted
/// to be as long as the array says; they must outlive the view.
fn import_raw<'a>(schema: &ArrowSchema, array: &ArrowArray, keep: &mut Keep) -> PyResult<Array<'a>> {
    if schema.format.is_null() {
        return Err(bad("an Arrow schema without a format"));
    }
    if !array.dictionary.is_null() || array.n_children != 0 {
        return Err(bad("a dictionary or nested array: cast it to its plain type first"));
    }
    if array.null_count < -1 {
        return Err(bad("an Arrow array with an invalid null count"));
    }
    let format = unsafe { CStr::from_ptr(schema.format) }.to_str().map_err(|_| bad("an Arrow format that is not text"))?;
    let len = count(array.length, "length")?;
    let off = count(array.offset, "offset")?;
    let end = off.checked_add(len).ok_or_else(|| bad("an Arrow array too long"))?;
    let nb = count(array.n_buffers, "buffer count")?;
    let need = match format {
        "g" | "f" | "e" | "l" | "i" | "s" | "c" | "L" | "I" | "S" | "C" => 2,
        "u" | "U" => 3,
        "vu" => 3,
        other => return Err(UnsupportedTypeError::new_err(format!("an Arrow array of format {other:?} is not supported (floats, integers, text): cast it first"))),
    };
    if nb < need {
        return Err(bad("an Arrow array with too few buffers"));
    }
    if array.buffers.is_null() {
        return Err(bad("an Arrow array without its buffers"));
    }
    let buffers = unsafe { std::slice::from_raw_parts(array.buffers, nb) };
    // SAFETY (all the slices below): lengths come from the checked counts and the buffers are those the array declares
    unsafe {
        macro_rules! prim {
            ($t:ty, $v:ident) => {
                Values::$v(&slice::<$t>(buffers[1], end, "data")?[off.min(end)..])
            };
        }
        let values = match format {
            "g" => prim!(f64, F64),
            "f" => prim!(f32, F32),
            "e" => prim!(u16, F16),
            "l" => prim!(i64, I64),
            "i" => prim!(i32, I32),
            "s" => prim!(i16, I16),
            "c" => prim!(i8, I8),
            "L" => prim!(u64, U64),
            "I" => prim!(u32, U32),
            "S" => prim!(u16, U16),
            "C" => prim!(u8, U8),
            "u" | "U" if len == 0 => {
                if format == "u" {
                    Values::Utf8 { offsets: &[0], data: &[] }
                } else {
                    Values::LargeUtf8 { offsets: &[0], data: &[] }
                }
            }
            "u" => {
                let offsets = &slice::<i32>(buffers[1], end + 1, "offsets")?[off..];
                let last = offsets.last().copied().unwrap_or(0).max(0) as usize;
                Values::Utf8 { offsets, data: slice::<u8>(buffers[2], last, "text")? }
            }
            "U" => {
                let offsets = &slice::<i64>(buffers[1], end + 1, "offsets")?[off..];
                let last = usize::try_from(offsets.last().copied().unwrap_or(0).max(0)).map_err(|_| bad("an Arrow array too long"))?;
                Values::LargeUtf8 { offsets, data: slice::<u8>(buffers[2], last, "text")? }
            }
            _ => {
                // "vu": views, then the data buffers, then their sizes
                let views = slice::<[u8; 16]>(buffers[1], end, "views")?;
                let sizes = slice::<i64>(buffers[nb - 1], nb - 3, "sizes")?;
                let data = (0..nb - 3)
                    .map(|i| slice::<u8>(buffers[2 + i], usize::try_from(sizes[i]).unwrap_or(0), "text"))
                    .collect::<PyResult<Vec<&'static [u8]>>>()?;
                keep.buffer_lists.push(data.into_boxed_slice());
                let list: &[&[u8]] = keep.buffer_lists.last().expect("just pushed");
                // SAFETY: the box is never dropped before `keep` is, and `keep` outlives every view.
                Values::Utf8View { views: &views[off.min(end)..], buffers: std::mem::transmute::<&[&[u8]], &'static [&'static [u8]]>(list) }
            }
        };
        let mut out = Array::new(values);
        if array.null_count != 0 && !buffers[0].is_null() {
            out = out.with_validity(slice::<u8>(buffers[0], end.div_ceil(8), "validity")?, off);
        }
        Ok(out)
    }
}

// A stream of arrays (`__arrow_c_stream__`: a DuckDB relation, a polars Series, a pyarrow Table, ...) read into columns
// this module owns: the batches are copied into one array per column, which is how a column is cut out of a table and
// chunks are combined.
#[repr(C)]
struct ArrowArrayStream {
    get_schema: Option<unsafe extern "C" fn(*mut ArrowArrayStream, *mut ArrowSchema) -> i32>,
    get_next: Option<unsafe extern "C" fn(*mut ArrowArrayStream, *mut ArrowArray) -> i32>,
    get_last_error: Option<unsafe extern "C" fn(*mut ArrowArrayStream) -> *const c_char>,
    release: Option<unsafe extern "C" fn(*mut ArrowArrayStream)>,
    private_data: *mut c_void,
}

struct SchemaGuard(ArrowSchema);
impl Drop for SchemaGuard {
    fn drop(&mut self) {
        if let Some(release) = self.0.release {
            // SAFETY: a schema the producer filled in and has not released
            unsafe { release(&mut self.0) };
        }
    }
}

struct ArrayGuard(ArrowArray);
impl Drop for ArrayGuard {
    fn drop(&mut self) {
        if let Some(release) = self.0.release {
            // SAFETY: an array the producer filled in and has not released
            unsafe { release(&mut self.0) };
        }
    }
}

enum Owned {
    F64(Vec<f64>),
    F32(Vec<f32>),
    F16(Vec<u16>),
    I64(Vec<i64>),
    I32(Vec<i32>),
    I16(Vec<i16>),
    I8(Vec<i8>),
    U64(Vec<u64>),
    U32(Vec<u32>),
    U16(Vec<u16>),
    U8(Vec<u8>),
    Text { offsets: Vec<i64>, data: Vec<u8> },
}

/// One column read from a stream: Arrow values this module owns (text as `large_string`), usable wherever an Arrow
/// array is.
#[pyclass(frozen, module = "arrow2xmla._native")]
struct ArrowColumn {
    owned: Owned,
    validity: Option<Vec<u8>>,
    len: usize,
}

impl ArrowColumn {
    fn array(&self) -> Array<'_> {
        let values = match &self.owned {
            Owned::F64(v) => Values::F64(v),
            Owned::F32(v) => Values::F32(v),
            Owned::F16(v) => Values::F16(v),
            Owned::I64(v) => Values::I64(v),
            Owned::I32(v) => Values::I32(v),
            Owned::I16(v) => Values::I16(v),
            Owned::I8(v) => Values::I8(v),
            Owned::U64(v) => Values::U64(v),
            Owned::U32(v) => Values::U32(v),
            Owned::U16(v) => Values::U16(v),
            Owned::U8(v) => Values::U8(v),
            Owned::Text { offsets, data } => Values::LargeUtf8 { offsets, data },
        };
        let array = Array::new(values);
        match &self.validity {
            Some(bits) => array.with_validity(bits, 0),
            None => array,
        }
    }

    fn type_name(&self) -> &'static str {
        match self.owned {
            Owned::F64(_) => "double",
            Owned::F32(_) => "float",
            Owned::F16(_) => "halffloat",
            Owned::I64(_) => "int64",
            Owned::I32(_) => "int32",
            Owned::I16(_) => "int16",
            Owned::I8(_) => "int8",
            Owned::U64(_) => "uint64",
            Owned::U32(_) => "uint32",
            Owned::U16(_) => "uint16",
            Owned::U8(_) => "uint8",
            Owned::Text { .. } => "large_string",
        }
    }
}

#[pymethods]
impl ArrowColumn {
    fn __len__(&self) -> usize {
        self.len
    }

    /// The Arrow type, e.g. `double`, `int64`, `large_string`.
    #[getter]
    fn r#type(&self) -> &'static str {
        self.type_name()
    }

    fn __repr__(&self) -> String {
        let nulls = self.validity.as_ref().map_or(0, |_| (0..self.len).filter(|&i| !self.array().is_valid(i)).count());
        format!("<ArrowColumn {} x {}, {} null>", self.type_name(), self.len, nulls)
    }

    /// A copy of the rows `offset .. offset + length` (to the end when `length` is None): one result can then feed the
    /// members of an axis and the cells, e.g. the names without the All row that comes first in the cells.
    #[pyo3(signature = (offset, length=None))]
    fn slice(&self, offset: i64, length: Option<i64>) -> PyResult<ArrowColumn> {
        let range_error = || CellsetError::new_err(format!("rows {offset}.. of a column of {} rows: out of range", self.len));
        let start = usize::try_from(offset).map_err(|_| range_error())?;
        let end = match length {
            Some(l) => start.checked_add(usize::try_from(l).map_err(|_| range_error())?).ok_or_else(range_error)?,
            None => self.len,
        };
        if start > self.len || end > self.len {
            return Err(range_error());
        }
        macro_rules! part {
            ($v:ident, $variant:ident) => {
                Owned::$variant($v[start..end].to_vec())
            };
        }
        let owned = match &self.owned {
            Owned::F64(v) => part!(v, F64),
            Owned::F32(v) => part!(v, F32),
            Owned::F16(v) => part!(v, F16),
            Owned::I64(v) => part!(v, I64),
            Owned::I32(v) => part!(v, I32),
            Owned::I16(v) => part!(v, I16),
            Owned::I8(v) => part!(v, I8),
            Owned::U64(v) => part!(v, U64),
            Owned::U32(v) => part!(v, U32),
            Owned::U16(v) => part!(v, U16),
            Owned::U8(v) => part!(v, U8),
            Owned::Text { offsets, data } => {
                let from = offsets[start];
                let to = offsets[end];
                Owned::Text {
                    offsets: offsets[start..=end].iter().map(|o| o - from).collect(),
                    data: data[usize::try_from(from).expect("offsets are not negative")..usize::try_from(to).expect("offsets are not negative")].to_vec(),
                }
            }
        };
        // the validity of the range, re-based to bit 0; none when the range holds no null
        let validity = self.validity.as_ref().and_then(|bits| {
            let valid = |i: usize| bits[i >> 3] >> (i & 7) & 1 == 1;
            if (start..end).all(valid) {
                return None;
            }
            let mut out = vec![0u8; (end - start).div_ceil(8)];
            for (k, i) in (start..end).enumerate() {
                if valid(i) {
                    out[k >> 3] |= 1 << (k & 7);
                }
            }
            Some(out)
        });
        Ok(ArrowColumn { owned, validity, len: end - start })
    }
}

/// A decimal column (`d:precision,scale[,bits]`): its unscaled integers, converted when the column is finished.
struct Dec {
    scale: i32,
    width: usize,
    vals: Vec<i128>,
}

/// `(scale, bytes per value)` of a decimal format: `d:precision,scale` (128 bits) or `d:precision,scale,32|64|128`.
fn parse_decimal(format: &str) -> Option<(i32, usize)> {
    let mut parts = format.strip_prefix("d:")?.split(',');
    let _precision: u32 = parts.next()?.parse().ok()?;
    let scale: i32 = parts.next()?.parse().ok()?;
    let bits: usize = match parts.next() {
        None => 128,
        Some(b) => b.parse().ok()?,
    };
    if parts.next().is_some() {
        return None;
    }
    matches!(bits, 32 | 64 | 128).then_some((scale, bits / 8))
}

/// Copies of one column's batches, appended.
struct Acc {
    owned: Owned,
    dec: Option<Dec>,
    valid: Vec<bool>,
    nulls: bool,
}

impl Acc {
    fn new(format: &str, name: &str) -> PyResult<Acc> {
        if let Some((scale, width)) = parse_decimal(format) {
            return Ok(Acc { owned: Owned::F64(vec![]), dec: Some(Dec { scale, width, vals: vec![] }), valid: vec![], nulls: false });
        }
        let owned = match format {
            "g" => Owned::F64(vec![]),
            "f" => Owned::F32(vec![]),
            "e" => Owned::F16(vec![]),
            "l" => Owned::I64(vec![]),
            "i" => Owned::I32(vec![]),
            "s" => Owned::I16(vec![]),
            "c" => Owned::I8(vec![]),
            "L" => Owned::U64(vec![]),
            "I" => Owned::U32(vec![]),
            "S" => Owned::U16(vec![]),
            "C" => Owned::U8(vec![]),
            "u" | "U" | "vu" => Owned::Text { offsets: vec![0], data: vec![] },
            other => {
                let what = if name.is_empty() { "an Arrow array".to_string() } else { format!("column {name:?}") };
                return Err(UnsupportedTypeError::new_err(format!("{what} has Arrow format {other:?}, which is not supported (floats, integers, decimals, text): cast it first (in a query: `x::DOUBLE`, `x::BIGINT`, `x::VARCHAR`)")));
            }
        };
        Ok(Acc { owned, dec: None, valid: vec![], nulls: false })
    }

    /// Appends the values of a decimal array (read as the little-endian integers the Arrow layout says they are).
    fn push_decimal(&mut self, array: &ArrowArray) -> PyResult<()> {
        let dec = self.dec.as_mut().expect("a decimal column");
        if !array.dictionary.is_null() || array.n_children != 0 {
            return Err(bad("a dictionary or nested array: cast it to its plain type first"));
        }
        if array.null_count < -1 {
            return Err(bad("an Arrow array with an invalid null count"));
        }
        let len = count(array.length, "length")?;
        let off = count(array.offset, "offset")?;
        let end = off.checked_add(len).ok_or_else(|| bad("an Arrow array too long"))?;
        if count(array.n_buffers, "buffer count")? < 2 || array.buffers.is_null() {
            return Err(bad("an Arrow array with too few buffers"));
        }
        // SAFETY: the buffers are those the array declares, as long as it says (the interface has no way to say more)
        let (data, bits) = unsafe {
            let buffers = std::slice::from_raw_parts(array.buffers, 2);
            let data = slice::<u8>(buffers[1], end.checked_mul(dec.width).ok_or_else(|| bad("an Arrow array too long"))?, "data")?;
            let bits = if array.null_count != 0 && !buffers[0].is_null() { Some(slice::<u8>(buffers[0], end.div_ceil(8), "validity")?) } else { None };
            (data, bits)
        };
        for i in off..end {
            let ok = bits.is_none_or(|b| b[i >> 3] >> (i & 7) & 1 == 1);
            let raw = &data[i * dec.width..(i + 1) * dec.width];
            let v = match dec.width {
                4 => i128::from(i32::from_le_bytes(raw.try_into().expect("4 bytes"))),
                8 => i128::from(i64::from_le_bytes(raw.try_into().expect("8 bytes"))),
                _ => i128::from_le_bytes(raw.try_into().expect("16 bytes")),
            };
            dec.vals.push(if ok { v } else { 0 }); // what a null row holds is not to be read
            self.nulls |= !ok;
            self.valid.push(ok);
        }
        Ok(())
    }

    fn push(&mut self, a: &Array<'_>) {
        let n = a.len();
        match (&mut self.owned, a.values) {
            (Owned::F64(v), Values::F64(s)) => v.extend_from_slice(s),
            (Owned::F32(v), Values::F32(s)) => v.extend_from_slice(s),
            (Owned::F16(v), Values::F16(s)) => v.extend_from_slice(s),
            (Owned::I64(v), Values::I64(s)) => v.extend_from_slice(s),
            (Owned::I32(v), Values::I32(s)) => v.extend_from_slice(s),
            (Owned::I16(v), Values::I16(s)) => v.extend_from_slice(s),
            (Owned::I8(v), Values::I8(s)) => v.extend_from_slice(s),
            (Owned::U64(v), Values::U64(s)) => v.extend_from_slice(s),
            (Owned::U32(v), Values::U32(s)) => v.extend_from_slice(s),
            (Owned::U16(v), Values::U16(s)) => v.extend_from_slice(s),
            (Owned::U8(v), Values::U8(s)) => v.extend_from_slice(s),
            (Owned::Text { offsets, data }, _) => {
                for i in 0..n {
                    if let Some(t) = a.text(i) {
                        data.extend_from_slice(t);
                    }
                    offsets.push(data.len() as i64);
                }
            }
            _ => unreachable!("the batches of a stream have one type"),
        }
        for i in 0..n {
            let ok = a.is_valid(i);
            self.nulls |= !ok;
            self.valid.push(ok);
        }
    }

    fn finish(mut self) -> ArrowColumn {
        let len = self.valid.len();
        if let Some(d) = self.dec.take() {
            // a decimal is read as the number Excel holds it as: a whole number as int64 when they all fit, else a double
            // (the unscaled value divided by 10^scale: exact for what a double holds, correctly rounded beyond)
            self.owned = if d.scale == 0 && d.vals.iter().all(|v| i64::try_from(*v).is_ok()) {
                Owned::I64(d.vals.iter().map(|v| *v as i64).collect())
            } else if d.scale < 0 {
                Owned::F64(d.vals.iter().map(|v| *v as f64 * 10f64.powi(-d.scale)).collect())
            } else {
                Owned::F64(d.vals.iter().map(|v| *v as f64 / 10f64.powi(d.scale)).collect())
            };
        }
        let validity = self.nulls.then(|| {
            let mut bits = vec![0u8; len.div_ceil(8)];
            for (i, ok) in self.valid.iter().enumerate() {
                if *ok {
                    bits[i >> 3] |= 1 << (i & 7);
                }
            }
            bits
        });
        ArrowColumn { owned: self.owned, validity, len }
    }
}

/// `read_stream(obj) -> [(name | None, ArrowColumn)]`: the columns of an Arrow stream. A table (a stream of structs) gives
/// one entry per column, named; a stream of plain arrays gives one unnamed column.
#[pyfunction]
fn read_stream(obj: &Bound<'_, PyAny>) -> PyResult<Vec<(Option<String>, ArrowColumn)>> {
    let method = obj.getattr("__arrow_c_stream__").map_err(|_| {
        bad(format!(
            "expected an Arrow table or stream (an object with __arrow_c_stream__, such as a DuckDB relation or a pyarrow Table), not {}",
            obj.get_type().name().map_or_else(|_| "this".to_string(), |n| format!("a {n}"))
        ))
    })?;
    let capsule = method.call0()?.cast_into::<PyCapsule>().map_err(|_| bad("__arrow_c_stream__ did not return a capsule"))?;
    let stream = capsule.pointer_checked(Some(c"arrow_array_stream"))?.as_ptr().cast::<ArrowArrayStream>();
    // SAFETY: the capsule holds a C stream interface struct (the protocol's contract) and stays alive until the end of
    // this function; its destructor releases the stream. What the structs show to be wrong is refused.
    unsafe {
        let s = &*stream;
        let (Some(get_schema), Some(get_next)) = (s.get_schema, s.get_next) else {
            return Err(bad("an Arrow stream without its functions, or one that was already released"));
        };
        if s.release.is_none() {
            return Err(bad("an Arrow stream that was already released"));
        }
        let last_error = |stream: *mut ArrowArrayStream| -> String {
            match (*stream).get_last_error {
                Some(f) => {
                    let p = f(stream);
                    if p.is_null() { "unknown error".into() } else { CStr::from_ptr(p).to_string_lossy().into_owned() }
                }
                None => "unknown error".into(),
            }
        };
        let mut schema = SchemaGuard(std::mem::zeroed());
        if get_schema(stream, &mut schema.0) != 0 {
            return Err(bad(format!("the Arrow stream failed: {}", last_error(stream))));
        }
        if schema.0.format.is_null() {
            return Err(bad("an Arrow stream schema without a format"));
        }
        let format = CStr::from_ptr(schema.0.format).to_str().map_err(|_| bad("an Arrow format that is not text"))?;
        let is_table = format == "+s";
        let columns = if is_table { count(schema.0.n_children, "child count")? } else { 1 };
        if is_table && columns > 0 && schema.0.children.is_null() {
            return Err(bad("an Arrow table schema without its children"));
        }
        let child_schema = |j: usize| -> &ArrowSchema { if is_table { &**schema.0.children.add(j) } else { &schema.0 } };
        let mut names: Vec<Option<String>> = vec![];
        let mut accs: Vec<Acc> = vec![];
        for j in 0..columns {
            let cs = child_schema(j);
            if cs.format.is_null() {
                return Err(bad("an Arrow column without a format"));
            }
            let name = if is_table && !cs.name.is_null() { CStr::from_ptr(cs.name).to_string_lossy().into_owned() } else { String::new() };
            let f = CStr::from_ptr(cs.format).to_str().map_err(|_| bad("an Arrow format that is not text"))?;
            if !cs.dictionary.is_null() {
                let what = if name.is_empty() { "an Arrow array".to_string() } else { format!("column {name:?}") };
                return Err(UnsupportedTypeError::new_err(format!("{what} is dictionary-encoded, which is not supported: cast it to its plain type first (in a query: `x::VARCHAR`)")));
            }
            accs.push(Acc::new(f, &name)?);
            names.push(is_table.then_some(name));
        }
        let mut keep = Keep::default();
        loop {
            let mut batch = ArrayGuard(std::mem::zeroed());
            if get_next(stream, &mut batch.0) != 0 {
                return Err(bad(format!("the Arrow stream failed: {}", last_error(stream))));
            }
            if batch.0.release.is_none() {
                break; // the end of the stream
            }
            if is_table {
                if count(batch.0.n_children, "child count")? != columns || (columns > 0 && batch.0.children.is_null()) {
                    return Err(bad("an Arrow table batch that does not match its schema"));
                }
                if batch.0.null_count != 0 {
                    return Err(bad("an Arrow table with null rows is not supported"));
                }
            }
            for (j, acc) in accs.iter_mut().enumerate() {
                let view;
                let array: &ArrowArray = if is_table {
                    let child = *batch.0.children.add(j);
                    if child.is_null() {
                        return Err(bad("an Arrow table batch without one of its columns"));
                    }
                    // the offset of a struct applies to its children
                    let mut copy = std::ptr::read(child);
                    copy.offset = copy.offset.checked_add(batch.0.offset).ok_or_else(|| bad("an Arrow array too long"))?;
                    copy.length = batch.0.length;
                    view = copy;
                    &view
                } else {
                    &batch.0
                };
                if acc.dec.is_some() {
                    acc.push_decimal(array)?;
                } else {
                    let a = import_raw(child_schema(j), array, &mut keep)?;
                    a.check().map_err(|e| bad(e.to_string()))?;
                    acc.push(&a);
                }
            }
        }
        drop(keep);
        Ok(names.into_iter().zip(accs.into_iter().map(Acc::finish)).collect())
    }
}

// The model is read from the Python dataclasses (`arrow2xmla._model`) by attribute: they are the one description of
// its shape, and they have checked the types. What the core's `validate()` knows, the core says.

fn attr<'py>(o: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    o.getattr(name)
}

fn member(o: &Bound<'_, PyAny>) -> PyResult<Member> {
    let mut m = Member::new(attr(o, "hierarchy")?.extract::<String>()?, attr(o, "level_name")?.extract::<String>()?, attr(o, "level_number")?.extract()?);
    m.unique_name = attr(o, "unique_name")?.extract()?;
    m.caption = attr(o, "caption")?.extract()?;
    m.parent_unique_name = attr(o, "parent_unique_name")?.extract()?;
    m.children_cardinality = attr(o, "children_cardinality")?.extract()?;
    let code: u8 = attr(o, "member_type")?.extract()?;
    m.member_type = MemberType::from_code(code).ok_or_else(|| CellsetError::new_err(format!("member_type {code} is not 1 (value), 2 (All), 3 (measure) or 4 (calculated)")))?;
    Ok(m)
}

/// The `.value` of each enum member of a sequence (`MemberProperty`, `CellProperty`).
fn names(o: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    o.try_iter()?.map(|e| e?.getattr("value")?.extract::<String>()).collect()
}

fn axis<'a>(o: &Bound<'_, PyAny>, keep: &mut Keep) -> PyResult<Axis<'a>> {
    let is_level = o.is_instance(&o.py().import("arrow2xmla._model")?.getattr("LevelAxis")?)?;
    let mut axis = if is_level {
        // a LevelAxis
        let all = attr(o, "all")?;
        let mut level = LevelAxis::new(member(&attr(o, "template")?)?, import(&attr(o, "keys")?, keep)?);
        level.all = if all.is_none() { None } else { Some(member(&all)?) };
        level.blank_caption = attr(o, "blank_caption")?.extract()?;
        Axis::level(level)
    } else {
        // a TupleAxis: the distinct members once, each tuple as indices into them
        let members = attr(o, "members")?.try_iter()?.map(|m| member(&m?)).collect::<PyResult<Vec<_>>>()?;
        let tuples = attr(o, "tuples")?.try_iter()?.map(|t| t?.extract::<Vec<u32>>()).collect::<PyResult<Vec<_>>>()?;
        Axis::tuples(Tuples::new(members, tuples))
    };
    axis.properties = names(&attr(o, "properties")?)?
        .iter()
        .map(|n| MemberProp::from_name(n).ok_or_else(|| CellsetError::new_err(format!("unknown member property {n}"))))
        .collect::<PyResult<_>>()?;
    Ok(axis)
}

fn build<'a>(cellset: &Bound<'_, PyAny>, keep: &mut Keep) -> PyResult<Cellset<'a>> {
    let mut cs = Cellset::new(attr(cellset, "cube")?.extract::<String>()?);
    cs.language = attr(cellset, "language")?.extract()?;
    cs.cell_properties = names(&attr(cellset, "cell_properties")?)?
        .iter()
        .map(|n| CellProp::from_name(n).ok_or_else(|| CellsetError::new_err(format!("unknown cell property {n} (written: VALUE, FORMAT_STRING, LANGUAGE)"))))
        .collect::<PyResult<_>>()?;
    for name in ["columns_axis", "rows_axis"] {
        let a = attr(cellset, name)?;
        if !a.is_none() {
            cs.axes.push(axis(&a, keep)?);
        }
    }
    for m in attr(cellset, "slicer")?.try_iter()? {
        cs.slicer.push(member(&m?)?);
    }
    for v in attr(cellset, "values")?.try_iter()? {
        let v = v?;
        cs.columns.push(Column::new(import(&attr(&v, "values")?, keep)?, attr(&v, "format")?.extract::<String>()?));
    }
    Ok(cs)
}

/// `cellset_xml(cellset, threads=0) -> bytes` (a `Cellset` of `arrow2xmla._model`): the answer whole. `threads`: 0 picks the machine's, 1 stays on this thread.
#[pyfunction]
#[pyo3(signature = (cellset, threads=0))]
fn cellset_xml<'py>(py: Python<'py>, cellset: &Bound<'py, PyAny>, threads: usize) -> PyResult<Bound<'py, PyBytes>> {
    let mut keep = Keep::default();
    let cs = build(cellset, &mut keep)?;
    let mut chunks: Vec<Vec<u8>> = vec![];
    py.detach(|| {
        cs.write_chunks(WriteOptions::with_threads(threads), &mut |c| {
            chunks.push(c);
            Ok(())
        })
    })
    .map_err(cellset_error)?;
    drop(cs);
    drop(keep);
    let total = chunks.iter().map(Vec::len).sum();
    PyBytes::new_with(py, total, |buf| {
        let mut at = 0;
        for c in &chunks {
            buf[at..at + c.len()].copy_from_slice(c);
            at += c.len();
        }
        Ok(())
    })
}

/// `cellset_write(cellset, write, threads=0)`: the answer in chunks of about 1 MB, each handed to `write(bytes)` as it is
/// made, so the whole answer never exists in memory. An error of `write` is raised as it is.
#[pyfunction]
#[pyo3(signature = (cellset, write, threads=0))]
fn cellset_write(py: Python<'_>, cellset: &Bound<'_, PyAny>, write: &Bound<'_, PyAny>, threads: usize) -> PyResult<()> {
    let mut keep = Keep::default();
    let cs = build(cellset, &mut keep)?;
    let write: Py<PyAny> = write.clone().unbind();
    let mut raised: Option<PyErr> = None;
    let result = py.detach(|| {
        cs.write_chunks(WriteOptions::with_threads(threads), &mut |chunk| {
            Python::attach(|py| {
                // a writer may take less than it is given (a socket, a pipe): the rest goes again; one that returns
                // nothing is taken to have written it all
                let mut sent = 0;
                let outcome = loop {
                    if let Err(e) = py.check_signals() {
                        break Err(e);
                    }
                    let part = PyBytes::new(py, &chunk[sent..]);
                    match write.call1(py, (part,)).and_then(|n| n.extract::<Option<usize>>(py)) {
                        Ok(Some(n)) if n < chunk.len() - sent => {
                            if n == 0 {
                                break Err(CellsetError::new_err("the writer took no bytes (write returned 0)"));
                            }
                            sent += n;
                        }
                        Ok(_) => break Ok(()),
                        Err(e) => break Err(e),
                    }
                };
                outcome.map_err(|e| {
                    raised = Some(e);
                    arrow2xmla::Error::Io(std::io::Error::other("the writer raised"))
                })
            })
        })
    });
    drop(cs);
    drop(keep);
    match (raised, result) {
        (Some(e), _) => Err(e),
        (None, Err(e)) => Err(cellset_error(e)),
        (None, Ok(())) => Ok(()),
    }
}

fn cellset_error(e: arrow2xmla::Error) -> PyErr {
    match e {
        arrow2xmla::Error::Unsupported(m) => UnsupportedTypeError::new_err(m),
        other => CellsetError::new_err(other.to_string()),
    }
}

/// `rowset_xml(rowset) -> bytes` (a `Rowset` of `arrow2xmla._model`).
#[pyfunction]
fn rowset_xml<'py>(py: Python<'py>, rowset: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let mut keep = Keep::default();
    let mut rs = Rowset::new();
    for c in attr(rowset, "columns")?.try_iter()? {
        let c = c?;
        let mut col = RowsetColumn::new(attr(&c, "name")?.extract::<String>()?, import(&attr(&c, "values")?, &mut keep)?);
        let t = attr(&c, "xsd_type")?;
        if !t.is_none() {
            col.xsd_type = Some(t.extract()?);
        }
        rs.columns.push(col);
    }
    let out = py.detach(|| rs.to_vec()).map_err(cellset_error)?;
    drop(rs);
    drop(keep);
    Ok(PyBytes::new(py, &out))
}

fn method(name: &str) -> PyResult<Method> {
    match name {
        "Discover" => Ok(Method::Discover),
        "Execute" => Ok(Method::Execute),
        other => Err(XmlaError::new_err(format!("method {other:?} is not Discover or Execute"))),
    }
}

/// `envelope(method, session, part, body)`: `part` is "head", "tail" or "wrap" (then `body` is wrapped).
#[pyfunction]
#[pyo3(signature = (method_name, session, part, body=None))]
fn envelope<'py>(py: Python<'py>, method_name: &str, session: Option<String>, part: &str, body: Option<&[u8]>) -> PyResult<Bound<'py, PyBytes>> {
    let mut e = Envelope::new(method(method_name)?);
    e.session = session;
    let bytes = match part {
        "head" => e.head(),
        "tail" => e.tail(),
        _ => e.wrap(body.unwrap_or_default()),
    };
    Ok(PyBytes::new(py, &bytes))
}

/// `fault(message, code, source, session) -> bytes`
#[pyfunction]
fn fault<'py>(py: Python<'py>, message: String, code: u32, source: String, session: Option<String>) -> Bound<'py, PyBytes> {
    let mut f = Fault::new(message);
    f.code = code;
    f.source = source;
    f.session = session;
    PyBytes::new(py, &f.to_vec())
}

#[cfg(feature = "request")]
mod request_py {
    use arrow2xmla::{parse_request_limited, Call, Session};
    use pyo3::prelude::*;
    use pyo3::types::PyDict;

/// `parse_request(body: bytes, max_size) -> dict`: `{"session": (kind, id), "call": {...}}`; raises `DoctypeError`,
/// `RequestTooLarge` or `RequestError`.
#[pyfunction(name = "parse_request")]
#[pyo3(signature = (body, max_size=arrow2xmla::DEFAULT_MAX_REQUEST))]
pub fn parse_request_py<'py>(py: Python<'py>, body: &[u8], max_size: usize) -> PyResult<Bound<'py, PyDict>> {
    let r = parse_request_limited(body, max_size).map_err(|e| match e {
        arrow2xmla::ParseError::Doctype => crate::DoctypeError::new_err("a DOCTYPE is not accepted"),
        arrow2xmla::ParseError::TooLarge { .. } => crate::RequestTooLarge::new_err(e.to_string()),
        other => crate::RequestError::new_err(other.to_string()),
    })?;
    let out = PyDict::new(py);
    let (kind, id) = match r.session {
        Session::None => ("none", None),
        Session::Begin => ("begin", None),
        Session::Use(i) => ("use", Some(i)),
        Session::End(i) => ("end", i),
        _ => ("none", None),
    };
    out.set_item("session", (kind, id))?;
    let call = PyDict::new(py);
    match r.call {
        Call::Discover { request_type, restrictions, properties } => {
            call.set_item("kind", "discover")?;
            call.set_item("request_type", request_type)?;
            call.set_item("restrictions", restrictions)?;
            call.set_item("properties", properties)?;
        }
        Call::Execute { statement, properties } => {
            call.set_item("kind", "execute")?;
            call.set_item("statement", statement)?;
            call.set_item("properties", properties)?;
        }
        _ => call.set_item("kind", "other")?,
    }
    out.set_item("call", call)?;
    Ok(out)
}

}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cellset_xml, m)?)?;
    #[cfg(feature = "request")]
    m.add("DEFAULT_MAX_REQUEST", arrow2xmla::DEFAULT_MAX_REQUEST)?;
    m.add_class::<ArrowColumn>()?;
    m.add_function(wrap_pyfunction!(read_stream, m)?)?;
    m.add_function(wrap_pyfunction!(cellset_write, m)?)?;
    m.add_function(wrap_pyfunction!(rowset_xml, m)?)?;
    m.add_function(wrap_pyfunction!(envelope, m)?)?;
    m.add_function(wrap_pyfunction!(fault, m)?)?;
    m.add("XmlaError", m.py().get_type::<XmlaError>())?;
    m.add("CellsetError", m.py().get_type::<CellsetError>())?;
    m.add("UnsupportedTypeError", m.py().get_type::<UnsupportedTypeError>())?;
    m.add("RequestError", m.py().get_type::<RequestError>())?;
    m.add("DoctypeError", m.py().get_type::<DoctypeError>())?;
    m.add("RequestTooLarge", m.py().get_type::<RequestTooLarge>())?;
    #[cfg(feature = "request")]
    m.add_function(wrap_pyfunction!(request_py::parse_request_py, m)?)?;
    Ok(())
}
