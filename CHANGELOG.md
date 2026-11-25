# Changelog

## Unreleased

- **No Arrow library needed.** `columns(table)` reads the columns of anything that exports `__arrow_c_stream__`: a DuckDB
  relation, a polars DataFrame, a pyarrow Table. The batches are copied once into one array per column (`ArrowColumn`), which
  `CellValues`, `LevelAxis` and `Rowset` take like any Arrow array. A polars Series or any one-column stream is now taken
  directly (it needed pyarrow before). Floats, integers and text; anything else (DuckDB's `DECIMAL`, dates, booleans, ...)
  raises `UnsupportedTypeError` naming the column and saying to cast it in the query.
- The sample is DuckDB (`examples/quickstart.py`), and CI runs it in an environment without pyarrow.

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
