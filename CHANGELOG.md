# Changelog

## Unreleased

- **`ArrowColumn.slice(offset, length=None)`**: a copy of some rows of a column. One result can now give both the
  members and the cells, e.g. `cols["customer"].slice(1)` for a result whose first row is the All row.
- **Sample: Databricks.** `examples/databricks.py` takes a query result from `arrowbricks` straight into `columns()`: one
  result with the All row first and the customers sorted (the warehouse sorts), the keys by `slice(1)`; or through DuckDB when
  the result is not in that order. It queries a warehouse when `DATABRICKS_HOST`,
  `DATABRICKS_WAREHOUSE_ID` and `DATABRICKS_TOKEN` are set, otherwise it rebuilds a small result from
  `examples/data/sales.arrows`. Tested and run in CI.

## 0.3.0

- **Removed:** the optional `pyarrow` extra (`pip install arrow2xmla[pyarrow]`): nothing in the package needs it.
- **Decimals as arrays.** A decimal array handed straight to `CellValues` or `Rowset` (DuckDB's `DECIMAL` and `HUGEINT` through `to_arrow_table()`, a pyarrow decimal array) is read like one from `columns()`: an int64 when whole and it fits, else a double.
- `benchmarks/duckdb_bench.py`: the routes from a DuckDB query to XMLA, with the time of each stage.
- The example server's listen queue is 128, not the standard library's 5 (it reset connections under concurrent clients).
- **`columns(table)`** reads the columns of anything that exports an Arrow stream (`__arrow_c_stream__`): a DuckDB
  relation, a polars DataFrame, a pyarrow Table. The batches are copied once into one array per column (`ArrowColumn`),
  which `CellValues`, `LevelAxis` and `Rowset` take like any Arrow array. A polars Series or any one-column stream is
  taken directly. Floats, integers, text and decimals; anything else (dates, booleans, ...) raises
  `UnsupportedTypeError` naming the column and saying to cast it in the query.
- **DuckDB's `DECIMAL` and `HUGEINT`** (the types of literals, `sum` and `avg`) are read through `columns()`: a whole-number
  decimal as int64 when it fits, otherwise a double. No casts are needed in a query.
- **Samples**, all on DuckDB, each runnable with `uv run --with duckdb python examples/<name>.py`: `quickstart.py` (a pivot
  and a parsed request), `streaming.py` (a million customers written with `write_to` and `iter_xml`, with the time and the
  memory it takes) and `server.py` (a complete XMLA endpoint over the standard library's `http.server`: sessions, the
  Discover rowsets, a streamed pivot, faults). `tests/test_examples.py` runs them; CI runs them against several DuckDB versions.

## 0.2.0

A server can now be written with this package alone: the answer to every request it parses has a writer.

- **Streaming.** `Cellset.write_to(fp)` hands the answer to `fp.write` in chunks of about 1 MB as they are made, and
  `Cellset.iter_xml()` yields them (for a streaming HTTP response). A 330 MB answer grows the process by about 22 MB
  instead of 630 MB. Whatever is wrong with the cellset is raised before the first chunk; an error of the writer is
  raised as it is.
- **SOAP.** `Envelope(Method.EXECUTE, session)` with `head()`, `tail()` and `wrap(body)`, and `fault(message, ...)`, in
  the form Analysis Services gives (`faultcode` `XMLAnalysisError.0x...`, the error in `detail`).
- **Rowsets.** `Rowset` / `RowsetColumn` write the answer to a `Discover` from Arrow columns, with the schema block clients
  read the column types from. Rust: `Rowset`, `RowsetColumn`, `Envelope`, `Fault`, `Method`.
- **Fix.** An Arrow type that is not written raises `UnsupportedTypeError` (it raised its parent, `CellsetError`).
- **Tests.** The mock endpoint now uses these writers, so olap4j (an independent Java client) reads the envelopes, the
  rowsets and the cellsets; the rows of a rowset are also validated against Microsoft's rowset schema (with the inline schema block taken out). CI runs the whole suite on Linux,
  macOS and Windows, on every supported Python, plus the MSRV, release-mode tests, docs, a big-endian machine, an audit and
  the benchmarks.

## 0.1.0

First release: the cellset writer (`Cellset`), the request parser (`parse_request`), typed Python model, Rust crate.
