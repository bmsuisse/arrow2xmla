"""From a DuckDB query to an XMLA cellset: which way is fastest, and where does the time go?

    uv run --with duckdb python benchmarks/duckdb_bench.py [--rows 500000] [--repeat 5]

A pivot of `--rows` customers on rows and 3 measures (revenue, orders, margin) on columns: the All row first, then the
customers sorted by name, NULL (the blank member) last, as in examples/quickstart.py. Every route produces the same bytes
(checked); only the way the data gets from DuckDB into the writer differs. Times are the best of `--repeat` runs.
"""

import argparse
import os
import sys
import time

import duckdb

import arrow2xmla as ax

try:
    import pyarrow as pa  # only for the route that goes through pyarrow, to compare
except ImportError:
    pa = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

CUSTOMER = ax.Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
ALL = ax.Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=ax.MemberType.ALL)
MEASURES = [ax.Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{n}]", n, member_type=ax.MemberType.MEASURE) for n in ("Revenue", "Orders", "Margin")]
FORMATS = ["#,##0.00", "0", "0.0%"]

NAMES_SQL = "select customer from sales order by customer nulls last"
CELLS_SQL = """
    select revenue, orders, margin from (
        select 0 as k, null as customer, sum(revenue) as revenue, sum(orders) as orders, avg(margin) as margin from sales
        union all
        select 1, customer, revenue, orders, margin from sales
    ) order by k, customer nulls last
"""


def best(fn, repeat: int) -> float:
    fn()  # warm up
    times = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return min(times)


def cellset(names, cells) -> ax.Cellset:
    return ax.Cellset(
        "Sales",
        [ax.CellValues(c, f) for c, f in zip(cells, FORMATS)],
        ax.TupleAxis.from_tuples([[m] for m in MEASURES]),
        ax.LevelAxis(CUSTOMER, names, ALL, properties=[ax.MemberProperty.MEMBER_CAPTION]),
    )


class Count:
    n = 0

    def write(self, b: bytes) -> int:
        self.n += len(b)
        return len(b)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=500_000)
    p.add_argument("--repeat", type=int, default=5)
    args = p.parse_args()
    n, rep = args.rows, args.repeat

    con = duckdb.connect()
    con.sql("select setseed(0.5)")
    con.sql(f"""
        create table sales as select
            'Customer ' || i::varchar || ' & Co' as customer,
            (random() * 1e6)::double as revenue,
            (random() * 999)::bigint as orders,
            random()::double as margin
        from range({n}) t(i)
    """)
    print(f"{n:,} customers x 3 measures, DuckDB {duckdb.__version__}, best of {rep}\n")

    # ---- the pieces -------------------------------------------------------------------------------------------------
    t_names_q = best(lambda: con.sql(NAMES_SQL).fetchall(), rep)
    t_cells_q = best(lambda: con.sql(CELLS_SQL).fetchall(), rep)
    t_read_names = best(lambda: ax.columns(con.sql(NAMES_SQL)), rep)
    t_read_cells = best(lambda: ax.columns(con.sql(CELLS_SQL)), rep)
    names = ax.columns(con.sql(NAMES_SQL))["customer"]
    cols = ax.columns(con.sql(CELLS_SQL))
    cells = [cols["revenue"], cols["orders"], cols["margin"]]
    cs = cellset(names, cells)
    xml = cs.to_xml()
    size = len(xml)
    t_write_1 = best(lambda: cs.to_xml(threads=1), rep)
    t_write_all = best(lambda: cs.to_xml(threads=0), rep)
    t_stream = best(lambda: cs.write_to(Count()), rep)
    t_iter = best(lambda: sum(len(c) for c in cs.iter_xml()), rep)

    ms = lambda t: f"{t * 1000:8.1f} ms"  # noqa: E731
    print(f"answer: {size / 1e6:.0f} MB of XML\n")
    print("the pieces")
    print(f"  DuckDB runs the rows query (sort)           {ms(t_names_q)}   (fetchall: Python objects, for reference)")
    print(f"  DuckDB runs the cells query (union + sort)  {ms(t_cells_q)}   (fetchall)")
    print(f"  ax.columns(rows query)                      {ms(t_read_names)}   query + copy into arrays")
    print(f"  ax.columns(cells query)                     {ms(t_read_cells)}   query + copy into arrays")
    print(f"  cellset.to_xml(threads=1)                   {ms(t_write_1)}   {size / 1e6 / t_write_1 / 1000:5.1f} GB/s")
    print(f"  cellset.to_xml()  all cores                 {ms(t_write_all)}   {size / 1e6 / t_write_all / 1000:5.1f} GB/s")
    print(f"  cellset.write_to(sink)                      {ms(t_stream)}   streamed, ~1 MB held")
    print(f"  sum(len(c) for c in cellset.iter_xml())     {ms(t_iter)}   streamed on a worker thread\n")

    # ---- the routes, end to end: DuckDB query -> bytes --------------------------------------------------------------
    def route_columns_to_xml() -> bytes:
        c = ax.columns(con.sql(CELLS_SQL))
        return cellset(ax.columns(con.sql(NAMES_SQL))["customer"], [c["revenue"], c["orders"], c["margin"]]).to_xml()

    def route_columns_stream() -> int:
        c = ax.columns(con.sql(CELLS_SQL))
        sink = Count()
        cellset(ax.columns(con.sql(NAMES_SQL))["customer"], [c["revenue"], c["orders"], c["margin"]]).write_to(sink)
        return sink.n

    def route_columns_iter() -> int:
        c = ax.columns(con.sql(CELLS_SQL))
        return sum(len(x) for x in cellset(ax.columns(con.sql(NAMES_SQL))["customer"], [c["revenue"], c["orders"], c["margin"]]).iter_xml())

    arrow_table = lambda rel: (rel.to_arrow_table() if hasattr(rel, "to_arrow_table") else rel.fetch_arrow_table())  # noqa: E731
    cast_sql = CELLS_SQL.replace("sum(orders) as orders", "sum(orders)::bigint as orders")  # an Arrow array of DuckDB's HUGEINT is a decimal

    def route_pyarrow() -> bytes:
        t = arrow_table(con.sql(cast_sql))
        nm = arrow_table(con.sql(NAMES_SQL)).column(0).combine_chunks()
        return cellset(nm, [t.column(i).combine_chunks() for i in range(3)]).to_xml()

    def route_python_rows() -> str:
        import reference as ref  # the repo's plain-Python writer (tests/reference.py)
        from types import SimpleNamespace

        rows = con.sql(CELLS_SQL).fetchall()
        keys = [r[0] for r in con.sql(NAMES_SQL).fetchall()]
        cell = {r * 3 + k: (v, FORMATS[k]) for r, row in enumerate(rows) for k, v in enumerate(row) if v is not None}
        member = lambda m: ref.Member(m.hierarchy, m.unique_name, m.caption, m.level_name, m.level_number, parent=m.parent_unique_name, children=m.children_cardinality, kind=int(m.member_type))  # noqa: E731
        light = ref.LightAxis(member(CUSTOMER), member(ALL), keys)
        axes = [[(member(m),) for m in MEASURES], [(member(ALL),)]]
        return ref.cellset_xml(ref.Cellset(SimpleNamespace(name="Sales"), axes, cell, (), [(), ("MEMBER_CAPTION",)], ("VALUE", "FORMAT_STRING"), {1: light}))

    routes = [
        ("duckdb -> ax.columns -> to_xml()   (bytes)", route_columns_to_xml),
        ("duckdb -> ax.columns -> write_to() (streamed)", route_columns_stream),
        ("duckdb -> ax.columns -> iter_xml() (streamed)", route_columns_iter),
    ]
    if pa is not None:
        routes.append(("duckdb -> fetch_arrow_table -> to_xml()", route_pyarrow))
    rows_for_python = n <= 600_000
    if rows_for_python:
        routes.append(("duckdb -> fetchall -> plain-Python writer", route_python_rows))

    # every route must write the same answer. (Not the All row: DuckDB sums doubles in parallel, so the last digits of
    # `sum(revenue)` can differ from one run to the next.)
    import re

    body = lambda x: re.sub(rb'<Cell CellOrdinal="[012]">.*?</Cell>', b"", x if isinstance(x, bytes) else x.encode(), count=3)  # noqa: E731
    reference = body(route_columns_to_xml())
    if pa is not None:
        assert body(route_pyarrow()) == reference, "the pyarrow route differs"
    if rows_for_python:
        assert body(route_python_rows()) == reference, "the plain-Python route differs"

    print("the routes, DuckDB query to XMLA (best of {})".format(rep))
    results = [(label, best(fn, rep if "plain-Python" not in label else min(rep, 2))) for label, fn in routes]
    fastest = min(t for _, t in results)
    for label, t in results:
        print(f"  {label:<48}{ms(t)}   {size / 1e6 / t / 1000:5.2f} GB/s   x{t / fastest:4.1f}")

    # ---- what the SQL costs ----------------------------------------------------------------------------------------
    print("\nwhat the SQL costs (query + copy into arrays; the writing is the same)")
    one = "select customer, revenue, orders, margin from sales"
    print(f"  one query, no sort, no All row   {ms(best(lambda: ax.columns(con.sql(one)), rep))}   lower bound")
    print(f"  one query, sorted                {ms(best(lambda: ax.columns(con.sql(one + ' order by customer nulls last')), rep))}")
    print(f"  the two queries above, sorted    {ms(t_read_names + t_read_cells)}   (rows + cells with the All row)")

    # ---- DuckDB's own threads ---------------------------------------------------------------------------------------
    print("\nDuckDB threads for the same route (columns -> to_xml)")
    for th in (1, 4, os.cpu_count() or 8):
        con.sql(f"set threads = {th}")
        print(f"  threads = {th:<3}{ms(best(route_columns_to_xml, rep))}")


if __name__ == "__main__":
    main()
