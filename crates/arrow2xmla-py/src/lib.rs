//! Python binding: `arrow2xmla._native.cellset_xml(spec)`. The spec is plain dicts and lists plus `pyarrow` arrays
//! (passed through the Arrow C data interface, no copy).

use arrow::array::{make_array, ArrayData};
use arrow::pyarrow::FromPyArrow;
use arrow2xmla::{parse_request, Call, Session, Axis, AxisSource, CellProperty, Cellset, Column, LevelAxis, Member, MemberProp};
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

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

fn array(o: &Bound<'_, PyAny>) -> PyResult<arrow::array::ArrayRef> {
    let data = ArrayData::from_pyarrow_bound(o)?;
    Ok(make_array(data))
}

fn props(d: &Bound<'_, PyDict>) -> PyResult<Vec<MemberProp>> {
    let Some(l) = d.get_item("properties")? else { return Ok(vec![]) };
    l.extract::<Vec<String>>()?
        .iter()
        .map(|n| MemberProp::from_name(n).ok_or_else(|| PyValueError::new_err(format!("unknown member property {n}"))))
        .collect()
}

fn axis(d: &Bound<'_, PyDict>) -> PyResult<Axis> {
    let properties = props(d)?;
    if let Some(l) = d.get_item("level")? {
        let l = l.cast_into::<PyDict>()?;
        let all = match l.get_item("all")? {
            Some(a) if !a.is_none() => Some(member(a.cast()?)?),
            _ => None,
        };
        let blank: String = l.get_item("blank_caption")?.map_or(Ok("(blank)".to_string()), |v| v.extract())?;
        let level = LevelAxis { all, template: member(get(&l, "template")?.cast()?)?, keys: array(&get(&l, "keys")?)?, blank_caption: blank };
        return Ok(Axis { source: AxisSource::Level(level), properties });
    }
    let tuples = get(d, "tuples")?
        .cast::<PyList>()?
        .iter()
        .map(|t| t.cast::<PyList>()?.iter().map(|m| member(m.cast()?)).collect::<PyResult<Vec<_>>>())
        .collect::<PyResult<Vec<_>>>()?;
    Ok(Axis { source: AxisSource::Tuples(tuples), properties })
}

/// Collects each chunk the writer flushes (about 1 MB), so a caller can send them one by one.
struct Chunks(Vec<Vec<u8>>);

impl std::io::Write for Chunks {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.0.push(b.to_vec());
        Ok(b.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn build(spec: &Bound<'_, PyDict>) -> PyResult<Cellset> {
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
        cs.axes.push(axis(a.cast()?)?);
    }
    if let Some(s) = spec.get_item("slicer")? {
        for m in s.cast::<PyList>()?.iter() {
            cs.slicer.push(member(m.cast()?)?);
        }
    }
    if let Some(c) = spec.get_item("columns")? {
        for item in c.cast::<PyList>()?.iter() {
            let (values, format): (Bound<'_, PyAny>, String) = item.extract()?;
            cs.columns.push(Column { values: array(&values)?, format });
        }
    }
    Ok(cs)
}

#[pyfunction]
fn cellset_xml<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyBytes>> {
    let cs = build(spec)?;
    let bytes = py.detach(|| cs.to_vec()).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &bytes))
}

/// The same answer as a list of byte chunks of about 1 MB.
#[pyfunction]
fn cellset_xml_chunks<'py>(py: Python<'py>, spec: &Bound<'py, PyDict>) -> PyResult<Vec<Bound<'py, PyBytes>>> {
    let cs = build(spec)?;
    let mut sink = Chunks(vec![]);
    py.detach(|| cs.write_to(&mut sink)).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(sink.0.iter().map(|c| PyBytes::new(py, c)).collect())
}

/// `parse_request(body: bytes) -> dict`: `{"session": (kind, id), "call": {...}}`; raises ValueError("doctype") or
/// ValueError("not_xml: ...").
#[pyfunction(name = "parse_request")]
fn parse_request_py<'py>(py: Python<'py>, body: &[u8]) -> PyResult<Bound<'py, PyDict>> {
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

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cellset_xml, m)?)?;
    m.add_function(wrap_pyfunction!(cellset_xml_chunks, m)?)?;
    m.add_function(wrap_pyfunction!(parse_request_py, m)?)?;
    Ok(())
}
