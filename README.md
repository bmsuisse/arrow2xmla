# arrow2xmla

Arrow in, XMLA out. A generic Rust library (with a Python binding) that writes the XML for Analysis answers an OLAP
client such as Excel's PivotTable reads, and parses its requests. It depends on no engine, catalog or product: callers
fill a small model (`Cellset`, `Axis`, `Member`, Arrow `Column`s) from their own data.

| Piece | Where |
|---|---|
| Cellset writer (MDDataset): rows rendered in chunks on several threads, straight from columnar data | `crates/arrow2xmla/src/cellset.rs` |
| Zero-copy column views (`Array`, `Values`): floats, integers, text (`Utf8`, `LargeUtf8`, `Utf8View`), nulls, offsets; no Arrow library needed (an optional `arrow` feature converts `arrow_array::ArrayRef`) | `array.rs` |
| `DISPLAY_INFO` of tuples (children, drilled down, same parent) | `display.rs` |
| Request parser (SOAP: Discover / Execute / session): no DOCTYPE, bounded depth, UTF-8 and UTF-16 (feature `request`) | `request.rs` |
| Python-`repr` floats (via `zmij`), `saxutils`-exact escaping; characters XML 1.0 forbids become U+FFFD | `float.rs`, `esc.rs` |
| Python binding: reads any Arrow array through the PyCapsule interface (`__arrow_c_array__`), no Arrow dependency | `crates/arrow2xmla-py` |

Not built yet: the SOAP envelope/fault and rowset (`Discover`) writers, the MDX parser, binary XML (`sx`) and Xpress.

## Install

```
pip install arrow2xmla
```

## Use

```python
from arrow2xmla import cellset_xml, parse_request
xml = cellset_xml(
    {
        "cube": "Sales",
        "axes": [{"properties": ["MEMBER_CAPTION"], "level": {"template": member, "keys": pa_text_array}}],
        "columns": [(pa_array, "#,##0")],   # a cell's ordinal is row * len(columns) + column
    },
    threads=0,   # 0: the machine's cores (at most 8); 1: the calling thread
)
req = parse_request(body)   # {"session": (kind, id), "call": {"kind": "execute", "statement": ...}}
```

Explicit tuples can be given as member dicts, or (faster, 4-5x on big axes) as `{"members": [...], "tuples": [[0, 3], ...]}`.

Columns may be any float (16, 32, 64 bit), integer, or text type; nulls are cells with no value. Other types (bool, dates,
decimals, dictionaries, ...) are refused with a `ValueError`; cast them first. A cell's ordinal is
`row * len(columns) + column`, and the axes must list exactly those cells (two axes: the columns, then the rows; one
axis: `rows * columns` members), else the call is refused before anything is written.

Numbers are typed as XMLA expects: integers `xsd:int`, or `xsd:long` / `xsd:unsignedLong` when they do not fit 32 bits;
floats `xsd:double` in Python's shortest `repr` form, with `NaN`, `INF`, `-INF`.

Threads: `threads=0` uses the machine's cores (at most 8), but never more than are free: many answers written at once
share them, so a web server does not oversubscribe. `threads=1` stays on the calling thread.

Arrays are trusted (see `SECURITY.md`); request bodies are not.

## Develop

```
cargo test                                   # Rust unit tests
uv sync && uv run maturin develop -r         # build the Python module
uv run pytest                                # byte parity against the pure-Python reference (tests/reference.py), and the data-type matrix
uv run pytest benchmarks --benchmark-only    # speed (pytest-benchmark); add --benchmark-save=NAME / --benchmark-compare
uv run python benchmarks/size.py             # size of the module and the wheel
```

The parity tests hold the Rust output to the plain Python implementation in `tests/reference.py` and the request parser
to Python's `expat` and `ElementTree`, on real Excel requests (`tests/fixtures/excel`). The reference passes characters XML
forbids through; this crate replaces them with U+FFFD (so Excel's parser accepts the answer): that is the deliberate difference,
and the tests keep those inputs out of the parity comparison.

## Speed and size

490 000 rows, 3 measures (296 MB of XML): about 20 ms on 8 threads, 65 ms on one (a pure-Python writer: 1 100 ms). The
module is about 500 KB, the wheel 250 KB; there are no Arrow crates in it. Per-version numbers: `benchmarks/`.
