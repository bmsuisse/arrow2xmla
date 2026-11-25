"""From an arrowbricks (Databricks) result already in memory to an XMLA cellset: where does the time go?

    uv run --with duckdb --with arrowbricks --with pyarrow python benchmarks/arrowbricks_bench.py [--rows 500000] [--repeat 7]

What is timed: the result is an arrowbricks table in memory, as `await cursor.fetchall_arrow()` returns it. NOT timed: the
Databricks query and the download of the result, and sending the bytes to the client. The tables are built once, with
pyarrow writing the Arrow IPC bytes (the only use of pyarrow here) and `arrowbricks._core.read_ipc_stream` reading them back
into arrowbricks' own table type, the way a result is rebuilt without a warehouse.

The pivot is `--rows` customers (unique names with '&') on rows and 3 measures (double revenue, bigint orders, double margin)
on columns, the shape of benchmarks/duckdb_bench.py. Two routes, both ending in the same bytes (checked):
  straight:  ONE result, as a warehouse returns it when the query sorts: the All row first (customer NULL, fixed totals), then
             the customers sorted by name. columns(result) -> names = customer.slice(1), the cells are the other columns as
             they are -> Cellset -> write_to. The sort runs in Databricks, so it is not in the time.
  reshaping: ONE raw result, in no order, registered in DuckDB; SQL sorts it and adds the All row, columns(con.sql(...)) ->
             Cellset -> write_to.
Times are the best of `--repeat` runs.
"""

import argparse
import os
import subprocess
import sys
import time

import duckdb

try:
    import pyarrow.ipc as ipc
except ImportError:
    sys.exit("this benchmark needs pyarrow, only to write the Arrow IPC bytes of the synthetic result once: run it with `--with pyarrow`")
try:
    from arrowbricks._core import read_ipc_stream
except ImportError:
    sys.exit("this benchmark needs arrowbricks: run it with `--with arrowbricks`")

import arrow2xmla as ax

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from duckdb_bench import ALL, CUSTOMER, FORMATS, MEASURES, Count, best, cellset  # noqa: E402,F401  (the same Cellset and sink)

ORDERED_SQL = """
    select customer, revenue, orders, margin from (
        select 0 as k, null as customer, sum(revenue) as revenue, sum(orders)::bigint as orders, 0.5 as margin from sales
        union all select 1, customer, revenue, orders, margin from sales
    ) order by k, customer nulls last
"""
NAMES_SQL = "select customer from sales order by customer nulls last"
CELLS_SQL = """
    select revenue, orders, margin from (
        select 0 as k, null as customer, sum(revenue) as revenue, sum(orders)::bigint as orders, avg(margin) as margin from sales
        union all
        select 1, customer, revenue, orders, margin from sales
    ) order by k, customer nulls last
"""


def machine() -> str:
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        cpu = "unknown CPU"
    return f"{cpu}, {os.cpu_count()} cores"


def arrowbricks_table(arrow_table):
    """Arrow IPC stream bytes -> arrowbricks' own table (pyarrow only writes the bytes)."""
    import io

    sink = io.BytesIO()
    with ipc.new_stream(sink, arrow_table.schema) as w:
        w.write_table(arrow_table)
    return read_ipc_stream(sink.getvalue())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=500_000)
    p.add_argument("--repeat", type=int, default=7)
    args = p.parse_args()
    n, rep = args.rows, args.repeat

    gen = duckdb.connect()
    gen.sql("select setseed(0.5)")
    # revenue in multiples of 0.25: a sum is exact in any order, so the All row is the same to the last digit in both routes
    gen.sql(f"""
        create table sales as select 'Customer ' || i::varchar || ' & Co' as customer, (random() * 4e6)::bigint / 4.0 as revenue,
               (random() * 999)::bigint as orders, random()::double as margin from range({n}) t(i)
    """)
    unsorted = arrowbricks_table(gen.sql("select * from sales order by hash(customer)").to_arrow_table())
    ordered_result = arrowbricks_table(gen.sql(ORDERED_SQL).to_arrow_table())  # the All row first, then sorted

    # ---- the stages -------------------------------------------------------------------------------------------------
    t_columns = best(lambda: ax.columns(ordered_result), rep)
    cols = ax.columns(ordered_result)
    t_slice = best(lambda: cols["customer"].slice(1), rep)
    cs = cellset(cols["customer"].slice(1), [cols["revenue"], cols["orders"], cols["margin"]])
    size = len(cs.to_xml())
    t_w1 = best(lambda: cs.to_xml(threads=1), rep)
    t_wall = best(lambda: cs.to_xml(threads=0), rep)
    t_stream = best(lambda: cs.write_to(Count()), rep)

    ms = lambda t: f"{t * 1000:8.1f} ms"  # noqa: E731
    rate = lambda t: f"{size / 1e9 / t:6.2f} GB/s {n / t / 1e6:6.2f} M rows/s"  # noqa: E731
    print(f"{machine()}; Python {sys.version.split()[0]}, DuckDB {duckdb.__version__}")
    print(f"{n:,} customers x 3 measures, an arrowbricks result in memory, best of {rep}; the Databricks query and download are not in it\n")
    print(f"answer: {size / 1e6:.0f} MB of XML\n\nthe stages")
    print(f"  ax.columns(ordered result)               {ms(t_columns)}   the copy out of the arrowbricks table (4 columns)")
    print(f"  names.slice(1)                           {ms(t_slice)}   the keys without the All row")
    print(f"  cellset.to_xml(threads=1)                {ms(t_w1)}   {rate(t_w1)}")
    print(f"  cellset.to_xml()  all cores              {ms(t_wall)}   {rate(t_wall)}")
    print(f"  cellset.write_to(sink)                   {ms(t_stream)}   {rate(t_stream)}\n")

    # ---- the routes, end to end -------------------------------------------------------------------------------------
    def straight() -> int:
        c = ax.columns(ordered_result)
        sink = Count()
        cellset(c["customer"].slice(1), [c["revenue"], c["orders"], c["margin"]]).write_to(sink)
        return sink.n

    con = duckdb.connect()
    con.register("sales", unsorted)

    def reshaping_cs(con) -> ax.Cellset:
        c = ax.columns(con.sql(CELLS_SQL))
        return cellset(ax.columns(con.sql(NAMES_SQL))["customer"], [c["revenue"], c["orders"], c["margin"]])

    def reshaping() -> int:  # the registered arrowbricks table is the SQL's table
        sink = Count()
        reshaping_cs(con).write_to(sink)
        return sink.n

    def reshaping_ctas() -> int:  # copied into a DuckDB table first (inside the time), then the same SQL
        c2 = duckdb.connect()
        c2.register("raw", unsorted)
        c2.sql("create table sales as select * from raw")
        sink = Count()
        reshaping_cs(c2).write_to(sink)
        return sink.n

    # the same answer from every route, but the All row (DuckDB sums doubles in parallel: the last digits vary)
    import re

    body = lambda x: re.sub(rb'<Cell CellOrdinal="[012]">.*?</Cell>', b"", x, count=3)  # noqa: E731
    reference = body(cs.to_xml())
    assert body(reshaping_cs(con).to_xml()) == reference, "the reshaping route differs"
    assert straight() == size and abs(reshaping() - size) < 64 and abs(reshaping_ctas() - size) < 64, "sizes differ"

    print("the routes, arrowbricks result to XMLA (write_to a counting sink)")
    results = [
        ("straight, ONE result: columns -> slice(1) -> write_to", best(straight, rep)),
        ("reshaping: register -> SQL -> columns x2 -> write_to", best(reshaping, rep)),
        ("reshaping: create table as select, then the same", best(reshaping_ctas, rep)),
    ]
    for label, t in results:
        print(f"  {label:<54}{ms(t)}   {rate(t)}")
    print(f"\n  stages of the straight route: columns {ms(t_columns).strip()}, slice {ms(t_slice).strip()}, writing {ms(t_stream).strip()}")


if __name__ == "__main__":
    main()
