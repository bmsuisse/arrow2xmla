<h1 align="center">arrow2xmla</h1>

<p align="center">
  <b>Arrow in, XMLA out.</b><br>
  Write the cellsets an OLAP client such as Excel's PivotTable reads, straight from columnar data, and parse its requests.<br>
  Cellsets, rowsets, SOAP envelopes and faults from Arrow, request parsing, streaming output.<br>
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

### From DuckDB

The same pivot straight from a DuckDB query (500 000 customers × 3 measures, the All row first and the customers sorted: a
**330 MB** answer), measured from the query to the finished XML:

| Route | Time | Throughput |
|---|---:|---:|
| `ax.columns(rel)` → `cellset.write_to(sink)`, streamed | **32 ms** | **10.4 GB/s** |
| `ax.columns(rel)` → `cellset.iter_xml()`, streamed | 34 ms | 9.8 GB/s |
| `rel.to_arrow_table()` → `cellset.to_xml()` | 33 ms | 9.9 GB/s |
| `ax.columns(rel)` → `cellset.to_xml()`, whole bytes | 38 ms | 8.7 GB/s |
| `rel.fetchall()` → plain-Python writer | 1 538 ms | 0.2 GB/s |

Of those 32 ms, DuckDB running the queries and `columns()` copying the result into arrays take about 13 ms (the sort and the
second query for the All row are most of it; one unsorted query is 4.7 ms), and writing the XML takes about 20 ms. Streaming
with `write_to` beats `to_xml()` by about 20% and holds about 1 MB instead of the whole answer. At 3 000 000 customers (a 2 GB
answer) the routes take 190 to 240 ms. DuckDB's own threads matter: 1 thread 54 ms, 4 or more 37 ms.
Reproduce with `uv run --with duckdb python benchmarks/duckdb_bench.py --rows 500000`; it checks that every route writes the
same bytes (except the All row, whose parallel `sum` can differ in its last digits from run to run).

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

Python 3.10+, one `abi3` wheel per platform (Linux, macOS, Windows). Data comes in through the
Arrow PyCapsule interface: `columns(table)` reads the columns of anything that exports `__arrow_c_stream__` (a DuckDB relation, a
polars DataFrame, a pyarrow Table), and an array that exports `__arrow_c_array__` (pyarrow, nanoarrow, ...) is read in place.

The Rust crate is not published on crates.io; the package is PyPI only. To use the crate from Rust, depend on it from git
(Rust 1.88+):

```toml
arrow2xmla = { git = "https://github.com/bmsuisse/arrow2xmla", tag = "v0.4.0" }
```

## Quick start

With DuckDB. This is [`examples/quickstart.py`](examples/quickstart.py), which runs as it is
(`uv run --with duckdb python examples/quickstart.py`); the snippet shortens its data and leaves out the request body.

```python
import duckdb
import arrow2xmla as ax
from arrow2xmla import CellValues, Cellset, Discover, Execute, LevelAxis, Member, MemberProperty, MemberType, TupleAxis, parse_request

con = duckdb.connect()
con.sql("create table orders as select * from (values ('Acme', 1000.5), ('Acme', 500.0), ('Globex', 920.0), (NULL, 77.25)) t(customer, amount)")
by_customer = "select customer, sum(amount) as revenue, count(*) as orders from orders group by customer"

# the rows of the pivot: one member per customer, read from the result of a query (NULL is the blank member)
customers = ax.columns(con.sql(f"select customer from ({by_customer}) order by customer nulls last"))["customer"]
# the cells: the All row first, then the customers in the same order
cells = ax.columns(con.sql(f"""
    select revenue, orders from (
        select 0 as k, null as customer, sum(revenue) as revenue, sum(orders) as orders from ({by_customer})
        union all select 1, customer, revenue, orders from ({by_customer})
    ) order by k, customer nulls last"""))

customer = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
all_customers = Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL)
measure = lambda name: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=MemberType.MEASURE)

cellset = Cellset(
    cube="Sales",
    columns_axis=TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
    rows_axis=LevelAxis(customer, customers, all=all_customers, properties=[MemberProperty.MEMBER_CAPTION]),
    values=[CellValues(cells["revenue"], "#,##0.00"), CellValues(cells["orders"], "0")],  # one per column of the pivot
)
xml = cellset.to_xml()          # bytes: an XMLA MDDataset, for the body of an Execute response

request = parse_request(body)   # body: the bytes of the HTTP request
match request.call:
    case Execute(statement=mdx): ...
    case Discover(request_type=kind): ...
```

`ax.columns(...)` takes the result of any query, with no casts: DuckDB's `DECIMAL` (what `sum(amount)` and decimal literals are)
is read as a double, and a whole-number decimal or `HUGEINT` (a sum of integers) as an int64 when it fits. The same works with
polars (`ax.columns(df)`) and pyarrow (`ax.columns(table)`), and arrays you already hold (`pa.array([...])`) go straight into
`CellValues`. `column.slice(offset, length=None)` is a copy of some rows of a column (0.4 ms for 500 000 names), so one result can give
both the members and the cells (see From Databricks below).

Tested with DuckDB 1.1.3, 1.2.2, 1.3.2, 1.4.4, 1.5.2 and 2.0.1 (CI runs the examples against several of them). Older DuckDB does not export Arrow streams.

### Examples

Each runs as it is with `uv run --with duckdb python examples/<name>.py`:

- [`examples/quickstart.py`](examples/quickstart.py): a pivot answer from a DuckDB query, and a parsed request.
- [`examples/streaming.py`](examples/streaming.py): a million customers written with `write_to` and `iter_xml`, with the time
  and the memory it takes (`EXAMPLE_ROWS=20000` for a quick run).
- [`examples/databricks.py`](examples/databricks.py): a Databricks result from arrowbricks, straight into `columns()` and
  reshaped through DuckDB (`--with arrowbricks`; offline from a small Arrow file when no warehouse is configured).
- [`examples/server.py`](examples/server.py): a complete XMLA endpoint over the standard library's `http.server`
  (`python examples/server.py --port 8000`): sessions, the Discover rowsets, a streamed pivot, faults.

### From Databricks (arrowbricks)

[arrowbricks](https://pypi.org/project/arrowbricks/) runs SQL on a Databricks warehouse and returns Arrow data that exports
the Arrow stream protocol, so a result goes straight into `columns()`. This is [`examples/databricks.py`](examples/databricks.py)
(`uv run --with duckdb --with arrowbricks python examples/databricks.py`), shortened.

**Straight: one result, no other tool.** The warehouse sorts: the query returns the All row first (`customer` NULL, the
totals), then the customers by name, NULL last. The sort runs in Databricks, not here, so this path needs the query to return
the rows in that order.

```python
import asyncio, os
import arrow2xmla as ax
from arrowbricks import connect

async def run(sql):
    async with connect(host=os.environ["DATABRICKS_HOST"], warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"], token=os.environ["DATABRICKS_TOKEN"]) as conn:
        cursor = conn.cursor()
        await cursor.execute(sql)
        return await cursor.fetchall_arrow()

result = asyncio.run(run("""
    with t as (select customer, sum(amount) as revenue, count(*) as orders from sales.orders group by customer)
    select customer, revenue, orders from (
        select 0 as k, cast(null as string) as customer, sum(revenue) as revenue, sum(orders) as orders from t
        union all select 1, customer, revenue, orders from t
    ) order by k, customer nulls last"""))

cols = ax.columns(result)
keys = cols["customer"].slice(1)           # the members: every row but the All row
cells = [cols["revenue"], cols["orders"]]  # the values: their first row is the All row
# LevelAxis(customer_member, keys, all=all_member), CellValues(cells[0]), ... -> Cellset -> to_xml() / write_to(), as in the quick start
```

**Reshaping: a result that is not in that order.** Copy it into a DuckDB table once (a registered Arrow stream can be read only
once on DuckDB before 1.5, and the two queries below read the result twice), sort and add the All row in SQL, and read the
columns from the queries:

```python
import duckdb

con = duckdb.connect()
con.register("raw_result", raw)  # raw: an arrowbricks result, one row per customer
con.execute("create table result as select * from raw_result")  # one read of the stream; every query after reads the table
keys = ax.columns(con.sql("select customer from result order by customer nulls last"))["customer"]
cells = ax.columns(con.sql("""select revenue, orders from (
    select 0 as k, null as customer, sum(revenue) as revenue, sum(orders) as orders from result
    union all select 1, customer, revenue, orders from result) order by k, customer nulls last"""))
```

Both write the same cellset. With `DATABRICKS_HOST`, `DATABRICKS_WAREHOUSE_ID` and `DATABRICKS_TOKEN` set the example queries
the warehouse; without them it rebuilds small results from `examples/data/*.arrows` and runs the same code. A Databricks
result reads like any other: text, integers, floats and decimals (a `sum` of a `DECIMAL`) are read as they are, but
timestamps, dates and booleans raise `UnsupportedTypeError`, so cast them in the query (`cast(day as string)`).

Measured with the result already in memory as an arrowbricks table (`benchmarks/arrowbricks_bench.py`: Apple M6, 12 cores,
Python 3.14, DuckDB 2.0.1; best of 7 runs, 3 for the 3 000 000 rows; written to a counting sink). Not in the numbers: the
Databricks query and the download of the result, and sending the bytes to the client.

| Customers × 3 measures | Answer | Route | Time | Throughput |
|---|---:|---|---:|---:|
| 500 000 | 326 MB | straight: `columns()` → `slice(1)` → `write_to` | 27 to 30 ms | 11 to 12 GB/s |
| 500 000 | 326 MB | reshaping: register in DuckDB → SQL → `columns()` → `write_to` | 65 to 78 ms | 4.2 to 5.1 GB/s |
| 3 000 000 | 2.0 GB | straight | 150 ms | 13.1 GB/s |
| 3 000 000 | 2.0 GB | reshaping | 396 ms | 5.0 GB/s |

In the straight route at 500 000 rows, `columns()` takes 5 to 8 ms, the `slice` under 1 ms and the writing about 21 to 23 ms;
run to run the times vary by a few milliseconds. Reproduce with
`uv run --with duckdb --with arrowbricks --with pyarrow python benchmarks/arrowbricks_bench.py --rows 500000 --repeat 7`
(pyarrow only writes the Arrow IPC bytes of the synthetic result once).

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

## A server

The three writers (cellset, rowset, fault) and the envelope are all a server needs next to `parse_request`; what it
answers with is up to it. [`examples/server.py`](examples/server.py) is a complete one in the standard library's
`http.server` over an in-memory DuckDB table (`python examples/server.py --port 8000`): it answers the Discover requests
Excel opens with, honours their restrictions, echoes sessions, streams the pivot of an Execute and answers a request it
refuses with a fault. It does not parse MDX, which is the engine's job. Its core, abridged:

```python
def answer(con, body: bytes):
    """-> (HTTP status, a list of bytes, or an iterator of bytes to stream)"""
    try:
        request = ax.parse_request(body, max_size=MAX_REQUEST)
        session = {ax.SessionKind.BEGIN: str(uuid.uuid4()), ax.SessionKind.USE: request.session.id}.get(request.session.kind)
        match request.call:
            case ax.Discover() as call:   # a rowset from a DuckDB query: Rowset.from_columns(ax.columns(con.sql(...)))
                return 200, [ax.Envelope(ax.Method.DISCOVER, session).wrap(discover(con, call))]
            case ax.Execute(statement=mdx):
                envelope = ax.Envelope(ax.Method.EXECUTE, session)
                chunks = pivot(con).iter_xml()   # your engine: the Cellset for the statement
                first = next(chunks)             # whatever is wrong with the cellset is raised here, before anything is sent
                return 200, itertools.chain([envelope.head(), first], chunks, [envelope.tail()])   # lazy: ~1 MB in memory
        return 500, [ax.fault("only Discover and Execute are supported", session=session)]
    except ax.XmlaError as e:
        return 500, [ax.fault(str(e), session=session)]
```

The handler sends a list with a `Content-Length`, and an iterator as HTTP/1.1 chunked transfer, so a 300 MB answer is never
whole in memory.

### Streaming

`to_xml()` builds the whole answer (about twice its size in memory at the peak). For a big one,
[`examples/streaming.py`](examples/streaming.py) writes a million customers (505 MB) both ways and checks they are the same:

```python
with open("answer.xml", "wb") as fp:
    cellset.write_to(fp)          # chunks of ~1 MB as they are made

for chunk in cellset.iter_xml():  # or as an iterator, written ahead on another thread (e.g. a streaming response)
    ...
```

| 330 MB answer (600 000 rows × 3 measures) | Memory the process grows by |
|---|---:|
| `to_xml()` | 630 MB |
| `write_to(sink)` / `iter_xml()` | **22 MB** |

Everything that can be checked is checked before the first chunk, so a response that streams does not start with an invalid
cellset; an error of the sink itself (a closed socket) is raised as it is, mid-stream. A sink that takes part of a chunk
is offered the rest; closing or dropping the iterator stops the writer.

## The model

| Piece | What it is |
|---|---|
| `Member` | One member: a value of a field, its All member, a measure. `MemberType` says which. |
| `LevelAxis` | The values of **one level**, read from an Arrow text column. The fast form for big axes: unique name `<hierarchy>.&[key]` and caption are made from the key; a null key is the blank member. Optional All member. Optional `captions` column: a caption per member, with the keys then written exactly as given (see below). |
| `TupleAxis` | **Explicit tuples**: the distinct members once, each tuple as indices into them. `from_tuples` builds one from tuples of members. `DISPLAY_INFO` (children, drilled down, same parent) is worked out for you. |
| `CellValues` | One **column of cells**: an Arrow array and its number format. A null is a cell with no value. |
| `Rowset` | The answer to a `Discover`: Arrow columns written with the schema block clients read the column types from (`RowsetColumn` to override a type, e.g. `xsd:boolean`). |
| `Envelope`, `fault` | The SOAP around an answer (`head()` / `tail()` for a streamed one), and the fault for a request that failed. |
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
| `decimal32`/`64`/`128` (DuckDB's `DECIMAL` and `HUGEINT`; as an array or through `columns()`) | `xsd:int` / `xsd:long` when the scale is 0 and every value fits 64 bits, else `xsd:double` (the unscaled value divided by 10^scale) |
| `string`, `large_string`, `string_view` | `xsd:string`, escaped |
| null | no value: the cell is left out |
| anything else (bool, dates, 256-bit decimals, dictionaries, ...) | refused with `UnsupportedTypeError`, naming the column: cast it first |

Characters XML 1.0 forbids are replaced with U+FFFD, so Excel's parser accepts the answer. Chunked arrays are combined
first. Of the cell properties (`CellProperty`), `VALUE`, `FORMAT_STRING` and `LANGUAGE` are written in each cell. `BACK_COLOR`,
`FORE_COLOR`, `FONT_FLAGS`, `FORMATTED_VALUE`, `FONT_NAME`, `FONT_SIZE`, `UPDATEABLE`, `ACTION_TYPE` and `CELL_ORDINAL` are
declared only: `CellInfo` names them, with their XSD type, and no cell carries them (a client that asked for them gets
the defaults). `CellInfo` lists them in the order of `cell_properties`, interleaved with the written ones. A name in
neither group is refused, rather than silently left blank.

```python
# what Excel asks: CELL PROPERTIES VALUE, FORMAT_STRING, LANGUAGE, BACK_COLOR, FORE_COLOR, FONT_FLAGS
Cellset(..., cell_properties=["VALUE", "FORMAT_STRING", "LANGUAGE", "BACK_COLOR", "FORE_COLOR", "FONT_FLAGS"])
```

### Member captions and keys

By default a level's member has its unique name `<hierarchy>.&[key]` and its caption both made from the one text column:
an empty key is `&[]` with an empty caption, `~tilde` stays `~tilde`. Pass `captions` (a text array of the same length;
a null caption is `blank_caption`) to give each caption its own column. The keys are then written as given, with only the
XML escaping and `]` doubled to `]]`, so a caller with its own key rules passes the keys already escaped:

```python
keys = pa.array(["~empty", "~~tilde", "Acme"])          # '' and '~tilde', escaped by the caller
captions = pa.array(["(empty)", "~tilde", None])        # None: blank_caption
LevelAxis(customer, keys, captions=captions)           # &[~empty] "(empty)", &[~~tilde] "~tilde", &[Acme] "(blank)"
```

In Rust: `level.captions = Some(Array::new(...))`; `cs.cell_properties = vec![CellProp::Value, CellProp::BackColor, ...]`.

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
- **Independent client**: olap4j's XMLA driver (Java) connects to that endpoint, runs MDX and reads the pivot back: axes, members,
  typed cells and nulls. The cellset is also validated against Microsoft's `mddataset` schema.

Real Excel for Windows is the one client that cannot be run in CI: the request fixtures are recordings of it, and the mock
client stands in for it.

What Excel sends and expects, from the recorded session: [`docs/EXCEL-XMLA.md`](docs/EXCEL-XMLA.md). Changes: [`CHANGELOG.md`](CHANGELOG.md).

## Not built yet

An MDX parser and evaluator (the engine's job), binary XML (`sx`) and Xpress compression, which MSOLAP can negotiate (plain
XML is accepted), and the rowsets' *content*: what `MDSCHEMA_CUBES` and friends hold is the server's catalog, written with
`Rowset`. Real Excel and Power BI are untested ([`docs/EXCEL-XMLA.md`](docs/EXCEL-XMLA.md)).

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
