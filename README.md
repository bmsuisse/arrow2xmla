<h1 align="center">arrow2xmla</h1>

<p align="center">
  <b>Arrow in, XMLA out.</b><br>
  Write the cellsets an OLAP client such as Excel's PivotTable reads, straight from columnar data, and parse its requests.<br>
  Pure Rust core · Python binding · no engine, no catalog, no runtime dependencies.
</p>

<p align="center">
  <a href="https://github.com/bmsuisse/arrow2xmla/actions/workflows/workflow.yml"><img alt="CI" src="https://github.com/bmsuisse/arrow2xmla/actions/workflows/workflow.yml/badge.svg"></a>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="Rust 1.88+" src="https://img.shields.io/badge/rust-1.88%2B-orange">
</p>

---

## Speed

A pivot of 490 000 rows × 3 measures is **296 MB of XML**. arrow2xmla writes it in **23 ms**.

| 490 000 rows × 3 measures (296 MB) | Time | Throughput | vs pure Python |
|---|---:|---:|---:|
| Pure-Python writer (`tests/reference.py`) | 1 287 ms | 0.23 GB/s | 1× |
| **arrow2xmla, 1 thread** | **81 ms** | **3.6 GB/s** | **16×** |
| **arrow2xmla, all cores** (`threads=0`) | **23 ms** | **12.8 GB/s** | **57×** |

More cases (`uv run pytest benchmarks --benchmark-only`, mean time per call):

| Case | Answer | Time |
|---|---:|---:|
| 60 000 rows × 1 float | 25 MB | 1.5 ms |
| 60 000 rows × 3 mixed (float, int, text) | 38 MB | 2.4 ms |
| 60 000 rows × 3, every value needs escaping | 42 MB | 3.2 ms |
| 490 000 rows × 3 mixed | 316 MB | 21 ms |
| Parse a real Excel request | – | ≈ 4 µs (23 recorded requests: 0.1 ms) |
| Parse a request with a large MDX statement | – | ≈ 0.3 ms |

<sub>Apple M-series, 12 cores, Python 3.14, best of several runs. The pure-Python writer is a plain, readable reference
implementation, not a tuned one: it is the oracle the tests compare every byte against. Reproduce with
`uv run python tests/bench_cellset.py 490000` and `uv run pytest benchmarks --benchmark-only`
(add `--benchmark-save=NAME` / `--benchmark-compare` to track a change).</sub>

**Why it is fast.** Rows are rendered straight from the Arrow buffers into byte chunks, in parallel, with no per-value
objects: integers through `itoa`, floats through `zmij` (Python's shortest `repr`, laid out for XML), text copied when it
needs no escaping. A level's members are written from the text column with no member object built per value. Many
answers written at once share the cores (`threads=0` never takes more than are free), so a web server does not
oversubscribe.

**And small.** The Python extension is **494 KB**, the wheel **261 KB** (`uv run python benchmarks/size.py`). There are no
Arrow crates in it: arrays are read through the Arrow C interface.

---

## Install

```
pip install arrow2xmla
```

Python 3.10+, one `abi3` wheel per platform (Linux, macOS, Windows). `pyarrow` is optional: any library that exports the
Arrow PyCapsule interface works (`pyarrow`, `nanoarrow`, ...; a `polars` Series goes through `pyarrow`, or `.to_arrow()` it first).

```toml
# Cargo.toml (Rust 1.88+)
arrow2xmla = "0.1"
```

## Quick start

Abridged from [`examples/quickstart.py`](examples/quickstart.py), which runs as it is (the snippet leaves out the request body).

```python
import pyarrow as pa
from arrow2xmla import CellValues, Cellset, Discover, Execute, LevelAxis, Member, MemberProperty, MemberType, TupleAxis, parse_request

# the rows: one member per customer, read straight from an Arrow text column (nothing is built per value)
customers = pa.array(["Acme", "Globex", None, "Initech & Co"])
customer = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
all_customers = Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL)

# the columns: two measures, listed once
measure = lambda name: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=MemberType.MEASURE)

cellset = Cellset(
    cube="Sales",
    columns_axis=TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
    rows_axis=LevelAxis(customer, customers, all=all_customers, properties=[MemberProperty.MEMBER_CAPTION]),
    # one array per column of cells, in the order of the columns axis; a null is an empty cell
    values=[
        CellValues(pa.array([1500.5, 920.0, None, 77.25, 12.0]), "#,##0.00"),  # the All row, then the customers
        CellValues(pa.array([30, 12, 1, 4, 2]), "0"),
    ],
)
xml = cellset.to_xml()          # bytes: an XMLA MDDataset, for the body of an Execute response

request = parse_request(body)   # body: the bytes of the HTTP request
match request.call:
    case Execute(statement=mdx): ...
    case Discover(request_type=kind): ...
```

### In Rust

[`crates/arrow2xmla/examples/basic.rs`](crates/arrow2xmla/examples/basic.rs): the same model over zero-copy `Array`s.

```rust
let mut rows = Axis::level(LevelAxis::new(customer, Array::new(Values::Utf8 { offsets: &offsets, data: &data })));
rows.properties = vec![MemberProp::MemberCaption];

let mut cellset = Cellset::new("Sales");
cellset.axes = vec![columns, rows];
cellset.columns.push(Column::new(Array::new(Values::F64(&amounts)), "#,##0.00"));

let xml = cellset.to_vec()?;   // or cellset.write_to(&mut writer), or write_chunks(..) for a stream of ~1 MB chunks
```

## The model

| Piece | What it is |
|---|---|
| `Member` | One member: a value of a field, its All member, a measure. `MemberType` says which. |
| `LevelAxis` | The values of **one level**, read from an Arrow text column. The fast form for big axes: unique name `<hierarchy>.&[key]` and caption are made from the key; a null key is the blank member. Optional All member. |
| `TupleAxis` | **Explicit tuples**: the distinct members once, each tuple as indices into them. `from_tuples` builds one from tuples of members. `DISPLAY_INFO` (children, drilled down, same parent) is worked out for you. |
| `CellValues` | One **column of cells**: an Arrow array and its number format. A null is a cell with no value. |
| `Cellset` | The answer to an MDX `Execute`: a cube name, the axes, the cell columns, an optional slicer (`WHERE`), the cell properties and the language. |

A cell's ordinal is `row * len(values) + column`. The axes must list exactly those cells, or the call is refused before a
byte is written:

- `columns_axis` and `rows_axis`: the first lists the columns, the second the rows.
- Only `columns_axis`: it lists `rows * len(values)` tuples, the cells in order.
- Neither: at most one cell (`SELECT FROM [Cube] WHERE ...`).
- `rows_axis` alone, or more than two axes: refused.

### Data types

| Arrow type | Written as |
|---|---|
| `int8` … `int64`, `uint8` … `uint64` | `xsd:int`; `xsd:long` / `xsd:unsignedLong` when 32 bits do not hold the value |
| `float16`, `float32`, `float64` | `xsd:double`, Python's shortest `repr` digits; `NaN`, `INF`, `-INF` |
| `string`, `large_string`, `string_view` | `xsd:string`, escaped |
| null | no value: the cell is left out |
| anything else (bool, dates, decimals, dictionaries, ...) | refused with `UnsupportedTypeError`: cast it first |

Characters XML 1.0 forbids are replaced with U+FFFD, so Excel's parser accepts the answer. Chunked arrays are combined
first. Only `VALUE`, `FORMAT_STRING` and `LANGUAGE` are written in a cell (`CellProperty`); a property that is not
written is not accepted, rather than silently left blank.

### Requests

`parse_request(body, max_size=4 MiB)` reads a SOAP envelope holding a `Discover` or an `Execute` (UTF-8, or UTF-16 with a
BOM) into frozen dataclasses you can `match` on: `Request(session, call)`, with `Session`, `Discover`
(`request_type`, `restrictions`, `properties`), `Execute` (`statement`, `properties`) and `Other`.

It is strict and safe for bodies from the network: no DOCTYPE is ever accepted (no entity expansion), nesting and the
number of elements are bounded, a longer body than `max_size` is refused before it is read, and what a strict XML parser
refuses is refused (a second root, text outside the root, an undeclared prefix, characters XML forbids).

### Errors

Everything this package refuses derives from `XmlaError` (a `ValueError`):

```
XmlaError
├── CellsetError              an invalid cellset: shapes that do not add up, bad arrays, unknown names
│   └── UnsupportedTypeError  an Arrow type that is not written
└── RequestError              a request that is not accepted
    ├── DoctypeError
    └── RequestTooLarge
```

A wrong Python type (a `str` where a `Member` belongs) is a plain `TypeError`. `Cellset.from_dict({...})` builds a
cellset from plain data (JSON, a config); an unknown key is an error.

## Trust

- **Request bodies are untrusted**: see above. Lower `max_size` for a public endpoint.
- **Arrow arrays are trusted.** The Arrow C data interface does not say how long a buffer is, so a variable-size array's
  extent is read from its own offsets. The binding refuses what the structs themselves show to be wrong (negative
  lengths, missing buffers, released arrays, offsets that overrun) with an error and never a crash, and the core checks
  offsets, UTF-8 and view bounds before writing. It is not a validator for hostile Arrow memory; see
  [`SECURITY.md`](SECURITY.md).

## Correctness

The output is byte-identical to a plain Python implementation, which is what the tests hold it to:

- **Parity**: random and hand-picked cellsets (all dtypes, escapes, nulls, display info, slicers, 1–3 threads) against
  `tests/reference.py`; the request parser against Python's `expat` + `ElementTree` on recorded real Excel requests
  (`tests/fixtures/excel`). The one deliberate difference is U+FFFD for characters XML forbids.
- **Hardening**: a data-type matrix, hostile C-interface structs in a subprocess (it must error, never crash), number
  forms at their boundaries, and a strict-parser suite.
- **Contract**: every name the Python enums know is known to the Rust core.
- **End to end**: a mock XMLA endpoint over real HTTP (`tests/e2e`) replays the recorded Excel requests; a mock client reads
  each answer the way a pivot client does (strict XML, axes as tuples, cells by ordinal, typed by `xsi:type`) and checks the
  grid against the data, including nulls, escaping, sessions, faults on bad requests and 200 000 rows from concurrent callers.

Real Excel for Windows is the one client that cannot be run in CI: the request fixtures are recordings of it, and the mock
client stands in for it.

What Excel sends and expects, from the recorded session: [`docs/EXCEL-XMLA.md`](docs/EXCEL-XMLA.md).

## Not built yet

SOAP envelope and fault writers, the `Discover` rowset writers, an MDX parser, binary XML (`sx`) and Xpress compression,
and a true streaming API (today a cellset is built in ~1 MB chunks and joined, so peak memory is about twice the
answer). The Rust `write_chunks` already hands chunks to a sink as they are made; Python returns the whole answer.

## Develop

```
cargo test                                   # Rust unit tests
uv sync && uv run maturin develop -r         # build the Python module
uv run pytest                                # parity, data types, hardening, contract
uv run pytest benchmarks --benchmark-only    # speed
uv run python benchmarks/size.py             # size of the module and the wheel
cargo run -p arrow2xmla --example basic      # the Rust example
```

CI (`.github/workflows/workflow.yml`) runs clippy and the tests, builds wheels for Linux (glibc, musl, arm64), macOS
(Intel, Apple silicon) and Windows, and publishes to PyPI on a `v*` tag.

## License

[MIT](LICENSE)
