"""A big pivot answer written in chunks instead of built whole in memory.

DuckDB generates a million customers (EXAMPLE_ROWS, or the first argument, changes the count); arrow2xmla writes the
pivot twice: with `cellset.write_to(fp)` into a temporary file, and as an iterator of chunks with `cellset.iter_xml()`
(what a streaming HTTP response sends). Both give the same bytes; the script prints their size, the time, and how far the
process's peak memory grew (`to_xml()` would hold the whole answer, and about as much again while it is made).

Run it with `uv run --with duckdb python examples/streaming.py`.
"""

import os
import sys
import tempfile
import time

import duckdb

import arrow2xmla as ax
from arrow2xmla import CellValues, Cellset, LevelAxis, Member, MemberProperty, MemberType, TupleAxis

try:
    import resource  # not on Windows
except ImportError:
    resource = None


def peak_mb() -> float | None:
    """The peak resident memory of this process so far, in MB (None where `resource` is missing)."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1 << 20 if sys.platform == "darwin" else 1 << 10)  # bytes on macOS, KB on Linux


def grown(since: float | None) -> str:
    # the peak is a high-water mark: a later run that fits in an earlier peak shows +0
    return "" if since is None else f", peak memory +{max(peak_mb() - since, 0):.0f} MB"


rows = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("EXAMPLE_ROWS", 1_000_000))

con = duckdb.connect()
con.sql(f"""
    create table customers as
    select i, 'Customer ' || lpad(i::varchar, 8, '0') as customer, (i * 37 % 100000) / 100.0 as revenue, 1 + i % 50 as orders
    from range({rows}) t(i)
""")

# the rows of the pivot, and the cells: the All row first, then the customers in the same order
names = ax.columns(con.sql("select customer from customers order by i"))["customer"]
cells = ax.columns(con.sql("""
    select revenue, orders from (
        select 0 as k, -1 as i, sum(revenue) as revenue, sum(orders) as orders from customers
        union all
        select 1, i, revenue, orders from customers
    ) order by k, i
"""))

customer = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
all_customers = Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL)
measure = lambda name: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=MemberType.MEASURE)  # noqa: E731

cellset = Cellset(
    cube="Sales",
    columns_axis=TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
    rows_axis=LevelAxis(customer, names, all=all_customers, properties=[MemberProperty.MEMBER_CAPTION]),
    values=[CellValues(cells["revenue"], "#,##0.00"), CellValues(cells["orders"], "0")],
)

# (a) into a file, in chunks of about 1 MB as they are made
before = peak_mb()
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "answer.xml")
    start = time.perf_counter()
    with open(path, "wb") as fp:
        cellset.write_to(fp)
    seconds = time.perf_counter() - start
    size = os.path.getsize(path)
print(f"write_to : {rows:,} customers -> {size / 1e6:,.1f} MB in {seconds * 1000:,.0f} ms{grown(before)}")

# (b) as an iterator of chunks: what a streaming HTTP response sends, one chunk at a time
before = peak_mb()
start = time.perf_counter()
chunks = total = 0
for chunk in cellset.iter_xml():
    chunks += 1
    total += len(chunk)
seconds = time.perf_counter() - start
print(f"iter_xml : {chunks} chunks, {total / 1e6:,.1f} MB in {seconds * 1000:,.0f} ms{grown(before)}")

assert total == size, f"write_to wrote {size} bytes, iter_xml gave {total}"
print("both give the same size:", size, "bytes")
