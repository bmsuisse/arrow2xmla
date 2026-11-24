//! Python binding: `arrow2xmla._native.cellset_xml(cellset, threads)`. The cellset is an `arrow2xmla._model.Cellset`
//! (read by attribute); its arrays come through the Arrow C data interface, no copy.

use std::ffi::{c_char, c_void, CStr};

use arrow2xmla::{Array, Axis, CellProp, Cellset, Column, LevelAxis, Member, MemberProp, MemberType, Tuples, Values, WriteOptions};
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyCapsule, PyCapsuleMethods};

// The exceptions; they are a hierarchy under ValueError, which the Python package re-exports.
create_exception!(_native, XmlaError, PyValueError, "Anything this package refuses.");
create_exception!(_native, CellsetError, XmlaError, "A cellset that is not valid: wrong shapes, bad arrays, unknown names.");
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
        other => return Err(bad(format!("an Arrow array of format {other:?} is not supported (floats, integers, text): cast it first"))),
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
    .map_err(|e| match e {
        arrow2xmla::Error::Unsupported(m) => UnsupportedTypeError::new_err(m),
        other => CellsetError::new_err(other.to_string()),
    })?;
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
