"""A pivot answer from DuckDB, no pyarrow: `uv run --with duckdb python examples/quickstart.py`.

DuckDB runs the query and hands its result over as Arrow; arrow2xmla reads the columns from it and writes the cellset.
"""

import duckdb

import arrow2xmla as ax
from arrow2xmla import CellValues, Cellset, Discover, Execute, LevelAxis, Member, MemberProperty, MemberType, TupleAxis, parse_request

con = duckdb.connect()
con.sql("""
    create table orders as select * from (values
        ('Acme', 1000.5, 'a1'), ('Acme', 500.0, 'a2'), ('Globex', 920.0, 'g1'),
        (NULL, 77.25, 'x1'), ('Initech & Co', 12.0, 'i1')
    ) t(customer, amount, id)
""")

# one row per customer: the rows of the pivot (NULL is the blank member, sorted last)
by_customer = """
    select customer, sum(amount)::double as revenue, count(*)::bigint as orders
    from orders group by customer
"""
customers = ax.columns(con.sql(f"select customer from ({by_customer}) order by customer nulls last"))["customer"]

# the cells: the All row first, then the customers in the same order. DuckDB types sums and literals as DECIMAL or HUGEINT,
# which are not written: cast to DOUBLE / BIGINT in the query.
cells = ax.columns(con.sql(f"""
    select revenue, orders from (
        select 0 as k, null as customer, sum(revenue) as revenue, sum(orders)::bigint as orders from ({by_customer})
        union all
        select 1, customer, revenue, orders from ({by_customer})
    ) order by k, customer nulls last
"""))

customer = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
all_customers = Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL)
measure = lambda name: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=MemberType.MEASURE)  # noqa: E731

cellset = Cellset(
    cube="Sales",
    columns_axis=TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
    rows_axis=LevelAxis(customer, customers, all=all_customers, properties=[MemberProperty.MEMBER_CAPTION]),
    values=[CellValues(cells["revenue"], "#,##0.00"), CellValues(cells["orders"], "0")],  # one per column of the pivot
)
xml = cellset.to_xml()
print(len(xml), "bytes of XMLA:", xml.decode()[:160], "...")

# and the request that asked for it
body = b"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement>SELECT FROM [Sales]</Statement></Command>
<Properties><PropertyList><Catalog>Sales</Catalog></PropertyList></Properties></Execute></soap:Body></soap:Envelope>"""
match parse_request(body).call:
    case Execute(statement=mdx):
        print("MDX:", mdx)
    case Discover(request_type=kind):
        print("Discover:", kind)
