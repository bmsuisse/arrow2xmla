# arrow2xmla

Arrow in, XMLA out. A generic Rust library (with a Python binding) that writes the XML for Analysis answers an OLAP
client such as Excel's PivotTable reads, and parses its requests. It depends on no engine, catalog or product: callers
fill a small model (`Cellset`, `Axis`, `Member`, Arrow `Column`s) from their own data.

| Piece | Where |
|---|---|
| Cellset writer (MDDataset), streamed in ~1 MB chunks, straight from Arrow arrays | `crates/arrow2xmla/src/cellset.rs` |
| `DISPLAY_INFO` of tuples (children, drilled down, same parent) | `display.rs` |
| Request parser (SOAP: Discover / Execute / session), no DOCTYPE, bounded depth, no Python `xml.etree` or `expat` | `request.rs` |
| Python-`repr` floats, `saxutils`-exact escaping (byte parity with an existing Python server) | `float.rs`, `esc.rs` |
| Python binding (`pyarrow` arrays, no copy) | `crates/arrow2xmla-py` |

Not built yet: the SOAP envelope/fault and rowset (`Discover`) writers, the MDX parser, binary XML (`sx`) and Xpress.

## Install

```
pip install arrow2xmla
```

## Use

```python
from arrow2xmla import cellset_xml, parse_request
xml = cellset_xml({"cube": "Sales", "axes": [...], "columns": [(pa_array, "#,##0")]})
req = parse_request(body)   # {"session": (kind, id), "call": {"kind": "execute", "statement": ...}}
```

## Develop

```
cargo test                                   # Rust unit tests
uv sync && uv run maturin develop -r         # build the Python module
uv run pytest                                # byte parity against the pure-Python reference (tests/reference.py)
uv run python tests/bench_cellset.py 60000   # Rust against the reference on one big pivot
```

The parity tests hold the Rust output to the plain Python implementation in `tests/reference.py` and the request parser
to Python's `expat` and `ElementTree`, on real Excel requests (`tests/fixtures/excel`).
