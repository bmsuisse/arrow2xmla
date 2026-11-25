"""A Databricks query result as an XMLA answer: arrowbricks result -> arrow2xmla.columns(result) -> Cellset -> XMLA.

Run it with `uv run --with duckdb --with arrowbricks python examples/databricks.py`.

arrowbricks is a Databricks SQL client whose results are Arrow data (they export `__arrow_c_stream__`), so
`arrow2xmla.columns(result)` reads them as they are. Two ways to the same cellset:
  straight:  the warehouse sorts. ONE result whose first row is the All row (customer NULL, the totals), then the customers
             sorted by name, NULL last. `columns(result)`; the keys are `cols["customer"].slice(1)`, the cells are the
             revenue and orders columns as they are (their first row is the All row). No other tool is involved.
  reshaping: for a result that is not in that order. Register it in DuckDB, sort and add the All row in SQL, read the
             columns from `con.sql(...)`.

LIVE mode runs when these variables are set (the names arrowbricks' own command line reads):
    DATABRICKS_HOST          the workspace host, e.g. adb-123.4.azuredatabricks.net
    DATABRICKS_WAREHOUSE_ID  the id of a SQL warehouse
    DATABRICKS_TOKEN         an access token
    DATABRICKS_QUERY         optional: a query returning customer (text), revenue and orders (numbers), one row per customer.
                             The default is an aggregate over `samples.tpch`, the sample data every workspace ships with.
Otherwise OFFLINE mode rebuilds the results from examples/data/sales_pivot.arrows (the straight path's) and sales.arrows
(the raw one) and runs the same code from there; the two paths must then write the same bytes.

Databricks timestamps, dates and booleans are not read by `columns()` (UnsupportedTypeError): cast them in the query.
Decimals (`sum` of a DECIMAL column) and every integer and float width are read.
"""

import asyncio
import os
import pathlib

import duckdb

import arrow2xmla as ax
from arrow2xmla import CellValues, Cellset, LevelAxis, Member, MemberProperty, MemberType, TupleAxis


HERE = pathlib.Path(__file__).parent
LIVE_VARIABLES = ("DATABRICKS_HOST", "DATABRICKS_WAREHOUSE_ID", "DATABRICKS_TOKEN")
RAW_QUERY = """
    select c.c_name as customer, sum(o.o_totalprice) as revenue, count(*) as orders
    from samples.tpch.orders o join samples.tpch.customer c on c.c_custkey = o.o_custkey
    group by c.c_name limit 50
"""


# the straight path's query: the All row first (k = 0), then the customers sorted, NULL last. The warehouse does the sorting.
def pivot_query(raw: str) -> str:
    return f"""
    with t as ({raw})
    select customer, revenue, orders from (
        select 0 as k, cast(null as string) as customer, sum(revenue) as revenue, sum(orders) as orders from t
        union all select 1, customer, revenue, orders from t
    ) order by k, customer nulls last"""


async def run_queries(*queries: str):
    from arrowbricks import connect

    async with connect(host=os.environ["DATABRICKS_HOST"], warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"], token=os.environ["DATABRICKS_TOKEN"]) as conn:
        results = []
        for sql in queries:
            cursor = conn.cursor()
            await cursor.execute(sql)
            results.append(await cursor.fetchall_arrow())  # an arrowbricks Table: Arrow data, exports __arrow_c_stream__
        return results


def offline_results():
    # How a result is rebuilt without a warehouse: arrowbricks reads Arrow IPC stream bytes into the same Table type that
    # `fetchall_arrow()` returns (the files were written by examples/data/make_sales.py).
    from arrowbricks._core import read_ipc_stream

    return [read_ipc_stream((HERE / "data" / name).read_bytes()) for name in ("sales_pivot.arrows", "sales.arrows")]


def build(keys, cells) -> Cellset:
    customer = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
    all_customers = Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL)
    measure = lambda name: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=MemberType.MEASURE)  # noqa: E731
    return Cellset(
        cube="Sales",
        columns_axis=TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
        rows_axis=LevelAxis(customer, keys, all=all_customers, properties=[MemberProperty.MEMBER_CAPTION]),
        values=[CellValues(cells["revenue"], "#,##0.00"), CellValues(cells["orders"], "0")],
    )


live = all(os.environ.get(name) for name in LIVE_VARIABLES)
print("mode:", "LIVE (Databricks)" if live else "OFFLINE (examples/data/*.arrows, no warehouse)")
if live:
    raw_sql = os.environ.get("DATABRICKS_QUERY") or RAW_QUERY
    ordered, raw = asyncio.run(run_queries(pivot_query(raw_sql), raw_sql))
else:
    ordered, raw = offline_results()
print(f"ordered result: {ordered.num_rows} rows (the All row, then the customers), columns {ordered.column_names}")

# 1. straight: ONE result, in the order wanted; the keys are its customer column without the All row
cols = ax.columns(ordered)
straight = build(cols["customer"].slice(1), cols).to_xml()
print(f"straight : {len(straight):,} bytes of XMLA from `columns(result)` and `slice(1)`")

# 2. reshaping, for a raw result: register it in DuckDB (it can be read as often as the SQL needs), sort it, add the All row
con = duckdb.connect()
con.register("result", raw)
keys = ax.columns(con.sql("select customer from result order by customer nulls last"))["customer"]
cells = ax.columns(con.sql("""
    select revenue, orders from (
        select 0 as k, null as customer, sum(revenue) as revenue, sum(orders) as orders from result
        union all select 1, customer, revenue, orders from result
    ) order by k, customer nulls last"""))
reshaped = build(keys, cells).to_xml()
print(f"reshaping: {len(reshaped):,} bytes of XMLA from a raw result of {raw.num_rows} rows sorted in DuckDB")
if not live:
    assert straight == reshaped, "the two paths differ"
    print("both paths wrote the same bytes")
print(straight.decode()[:160], "...")
