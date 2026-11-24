//! Python binding: `arrow2xmla._native.cellset_xml(spec)`. The spec is plain dicts and lists plus `pyarrow` arrays
//! (passed through the Arrow C data interface, no copy).

use std::ffi::{c_char, c_void, CStr};

use arrow2xmla::{Array, Axis, AxisSource, CellProperty, Cellset, Column, LevelAxis, Member, MemberProp, Tuples, Values, WriteOptions};
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyCapsule, PyCapsuleMethods, PyDict, PyList};

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
    PyValueError::new_err(msg.into())
}

/// `n` elements of `T` at `ptr` (null is an empty slice when `n` is 0 and an error otherwise).
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
    Ok(std::slice::from_raw_parts(ptr.cast::<T>(), n))
}

fn import<'a>(obj: &Bound<'_, PyAny>, keep: &mut Keep) -> PyResult<Array<'a>> {
    let method = obj
        .getattr("__arrow_c_array__")
        .map_err(|_| bad("expected an Arrow array (an object with __arrow_c_array__); combine a chunked array first"))?;
    let (schema_c, array_c): (Bound<'_, PyCapsule>, Bound<'_, PyCapsule>) = method.call0()?.extract()?;
    let schema = schema_c.pointer_checked(Some(c"arrow_schema"))?.as_ptr().cast::<ArrowSchema>();
    let array = array_c.pointer_checked(Some(c"arrow_array"))?.as_ptr().cast::<ArrowArray>();
    keep.capsules.push(schema_c.into_any().unbind());
    keep.capsules.push(array_c.into_any().unbind());
    // SAFETY: the capsules hold valid C data interface structs (the protocol's contract) and stay alive in `keep`.
    let (schema, array) = unsafe { (&*schema, &*array) };
    if !array.dictionary.is_null() || array.n_children != 0 {
        return Err(bad("a dictionary or nested array: cast it to its plain type first"));
    }
    let format = unsafe { CStr::from_ptr(schema.format) }.to_str().map_err(|_| bad("an Arrow format that is not text"))?;
    let (len, off) = (array.length as usize, array.offset as usize);
    let nb = array.n_buffers as usize;
    let buffers = unsafe { std::slice::from_raw_parts(array.buffers, nb) };
    // SAFETY (all the slices below): lengths come from the array and the buffers are those it declares
    unsafe {
        macro_rules! prim {
            ($t:ty, $v:ident) => {{
                if nb < 2 {
                    return Err(bad("an Arrow array with too few buffers"));
                }
                Values::$v(slice::<$t>(buffers[1], off + len, "data")?.get(off..).unwrap_or(&[]))
            }};
        }
        let values = match format {
            "g" => prim!(f64, F64),
            "f" => prim!(f32, F32),
            "l" => prim!(i64, I64),
            "i" => prim!(i32, I32),
            "s" => prim!(i16, I16),
            "c" => prim!(i8, I8),
            "L" => prim!(u64, U64),
            "I" => prim!(u32, U32),
            "S" => prim!(u16, U16),
            "C" => prim!(u8, U8),
            "u" | "U" => {
                if nb < 3 {
                    return Err(bad("an Arrow text array with too few buffers"));
                }
                if format == "u" {
                    let offsets = slice::<i32>(buffers[1], off + len + 1, "offsets")?;
                    let offsets = &offsets[off..];
                    let end = offsets.last().copied().unwrap_or(0).max(0) as usize;
                    Values::Utf8 { offsets, data: slice::<u8>(buffers[2], end, "text")? }
                } else {
                    let offsets = slice::<i64>(buffers[1], off + len + 1, "offsets")?;
                    let offsets = &offsets[off..];
                    let end = offsets.last().copied().unwrap_or(0).max(0) as usize;
                    Values::LargeUtf8 { offsets, data: slice::<u8>(buffers[2], end, "text")? }
                }
            }
            "vu" => {
                if nb < 3 {
                    return Err(bad("an Arrow string-view array with too few buffers"));
                }
                let views = slice::<[u8; 16]>(buffers[1], off + len, "views")?;
                let sizes = slice::<i64>(buffers[nb - 1], nb - 3, "sizes")?;
                let data: Vec<&'static [u8]> = (0..nb - 3)
                    .map(|i| slice::<u8>(buffers[2 + i], sizes[i].max(0) as usize, "text"))
                    .collect::<PyResult<_>>()?;
                keep.buffer_lists.push(data.into_boxed_slice());
                let list: &[&[u8]] = keep.buffer_lists.last().expect("just pushed");
                // SAFETY: the box is never dropped before `keep` is, and `keep` outlives every view.
                Values::Utf8View { views: &views[off..], buffers: std::mem::transmute::<&[&[u8]], &'static [&'static [u8]]>(list) }
            }
            other => return Err(bad(format!("an Arrow array of format {other:?} is not supported (floats, integers, text)"))),
        };
        let mut out = Array::new(values);
        if array.null_count != 0 && !buffers[0].is_null() {
            let bytes = (off + len).div_ceil(8);
            out = out.with_validity(slice::<u8>(buffers[0], bytes, "validity")?, off);
        }
        Ok(out)
    }
}

fn get<'py>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(k)?.ok_or_else(|| PyKeyError::new_err(k.to_string()))
}

fn member(d: &Bound<'_, PyDict>) -> PyResult<Member> {
    let opt = |k: &str| -> PyResult<Option<Bound<'_, PyAny>>> { d.get_item(k) };
    Ok(Member {
        hierarchy: get(d, "hierarchy")?.extract()?,
        unique_name: opt("unique_name")?.map_or(Ok(String::new()), |v| v.extract())?,
        caption: opt("caption")?.map_or(Ok(String::new()), |v| v.extract())?,
        level_name: get(d, "level_name")?.extract()?,
        level_number: get(d, "level_number")?.extract()?,
        parent_unique_name: opt("parent_unique_name")?.map_or(Ok(String::new()), |v| v.extract())?,
        children_cardinality: opt("children_cardinality")?.map_or(Ok(0), |v| v.extract())?,
        member_type: opt("member_type")?.map_or(Ok(1), |v| v.extract())?,
    })
}

fn props(d: &Bound<'_, PyDict>) -> PyResult<Vec<MemberProp>> {
    let Some(l) = d.get_item("properties")? else { return Ok(vec![]) };
    l.extract::<Vec<String>>()?
        .iter()
        .map(|n| MemberProp::from_name(n).ok_or_else(|| PyValueError::new_err(format!("unknown member property {n}"))))
        .collect()
}

fn axis<'a>(d: &Bound<'_, PyDict>, keep: &mut Keep) -> PyResult<Axis<'a>> {
    let properties = props(d)?;
    if let Some(l) = d.get_item("level")? {
        let l = l.cast_into::<PyDict>()?;
        let all = match l.get_item("all")? {
            Some(a) if !a.is_none() => Some(member(a.cast()?)?),
            _ => None,
        };
        let blank: String = l.get_item("blank_caption")?.map_or(Ok("(blank)".to_string()), |v| v.extract())?;
        let level = LevelAxis { all, template: member(get(&l, "template")?.cast()?)?, keys: import(&get(&l, "keys")?, keep)?, blank_caption: blank };
        return Ok(Axis { source: AxisSource::Level(Box::new(level)), properties });
    }
    let rows = get(d, "tuples")?;
    let rows = rows.cast::<PyList>()?;
    // either each tuple lists its member dicts, or `members` lists the distinct ones once and a tuple lists indices
    let tuples = if let Some(members) = d.get_item("members")? {
        let members = members.cast::<PyList>()?.iter().map(|m| member(m.cast()?)).collect::<PyResult<Vec<_>>>()?;
        let tuples = rows.iter().map(|t| t.extract::<Vec<u32>>()).collect::<PyResult<Vec<_>>>()?;
        Tuples { members, tuples }
    } else {
        Tuples::from_members(
            rows.iter()
                .map(|t| t.cast::<PyList>()?.iter().map(|m| member(m.cast()?)).collect::<PyResult<Vec<_>>>())
                .collect::<PyResult<Vec<_>>>()?,
        )
    };
    Ok(Axis { source: AxisSource::Tuples(tuples), properties })
}

fn build<'a>(spec: &Bound<'_, PyDict>, keep: &mut Keep) -> PyResult<Cellset<'a>> {
    let mut cs = Cellset::new(get(spec, "cube")?.extract::<String>()?);
    if let Some(l) = spec.get_item("language")? {
        cs.language = l.extract()?;
    }
    if let Some(p) = spec.get_item("cell_properties")? {
        cs.cell_properties = p
            .extract::<Vec<String>>()?
            .iter()
            .map(|n| CellProperty::standard(n).ok_or_else(|| PyValueError::new_err(format!("unknown cell property {n}"))))
            .collect::<PyResult<_>>()?;
    }
    for a in get(spec, "axes")?.cast::<PyList>()?.iter() {
        cs.axes.push(axis(a.cast()?, keep)?);
    }
    if let Some(s) = spec.get_item("slicer")? {
        for m in s.cast::<PyList>()?.iter() {
            cs.slicer.push(member(m.cast()?)?);
        }
    }
    if let Some(c) = spec.get_item("columns")? {
        for item in c.cast::<PyList>()?.iter() {
            let (values, format): (Bound<'_, PyAny>, String) = item.extract()?;
            cs.columns.push(Column { values: import(&values, keep)?, format });
        }
    }
    Ok(cs)
}

/// The chunks of the answer. The views read the Python arrays, which `keep` holds until the chunks are made.
fn chunks_of(py: Python<'_>, spec: &Bound<'_, PyDict>, threads: usize) -> PyResult<Vec<Vec<u8>>> {
    let mut keep = Keep::default();
    let cs = build(spec, &mut keep)?;
    let mut chunks: Vec<Vec<u8>> = vec![];
    py.detach(|| {
        cs.write_chunks(WriteOptions { threads }, &mut |c| {
            chunks.push(c);
            Ok(())
        })
    })
    .map_err(|e| PyValueError::new_err(e.to_string()))?;
    drop(cs);
    drop(keep);
    Ok(chunks)
}

/// `cellset_xml(spec, threads=0) -> bytes`: the answer whole. `threads`: 0 picks the machine's, 1 stays on this thread.
#[pyfunction]
#[pyo3(signature = (spec, threads=0))]
fn cellset_xml<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, threads: usize) -> PyResult<Bound<'py, PyBytes>> {
    let chunks = chunks_of(py, spec, threads)?;
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

/// `cellset_xml_chunks(spec, threads=0) -> list[bytes]`: the same answer in chunks of about 1 MB.
#[pyfunction]
#[pyo3(signature = (spec, threads=0))]
fn cellset_xml_chunks<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>, threads: usize) -> PyResult<Vec<Bound<'py, PyBytes>>> {
    Ok(chunks_of(py, spec, threads)?.iter().map(|c| PyBytes::new(py, c)).collect())
}

#[cfg(feature = "request")]
mod request_py {
    use arrow2xmla::{parse_request, Call, Session};
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;

/// `parse_request(body: bytes) -> dict`: `{"session": (kind, id), "call": {...}}`; raises ValueError("doctype") or
/// ValueError("not_xml: ...").
#[pyfunction(name = "parse_request")]
pub fn parse_request_py<'py>(py: Python<'py>, body: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let r = parse_request(body).map_err(|e| match e {
        arrow2xmla::ParseError::Doctype => PyValueError::new_err("doctype"),
        arrow2xmla::ParseError::NotXml(m) => PyValueError::new_err(format!("not_xml: {m}")),
    })?;
    let out = PyDict::new(py);
    let (kind, id) = match r.session {
        Session::None => ("none", None),
        Session::Begin => ("begin", None),
        Session::Use(i) => ("use", Some(i)),
        Session::End(i) => ("end", i),
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
        Call::Other => call.set_item("kind", "other")?,
    }
    out.set_item("call", call)?;
    Ok(out)
}

}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cellset_xml, m)?)?;
    m.add_function(wrap_pyfunction!(cellset_xml_chunks, m)?)?;
    #[cfg(feature = "request")]
    m.add_function(wrap_pyfunction!(request_py::parse_request_py, m)?)?;
    Ok(())
}
