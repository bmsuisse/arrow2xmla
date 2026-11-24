"""A pivot answer from Arrow data: run with `uv run python examples/quickstart.py`."""

import pyarrow as pa

from arrow2xmla import CellValues, Cellset, LevelAxis, Member, MemberProperty, MemberType, TupleAxis, parse_request

# the rows: one member per customer, read straight from an Arrow text column (nothing is built per value)
customers = pa.array(["Acme", "Globex", None, "Initech & Co"])
customer = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
all_customers = Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL)

# the columns: two measures, listed once
measure = lambda name: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=MemberType.MEASURE)  # noqa: E731

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
print(cellset.to_xml().decode()[:600], "...")

# and the request that asked for it
body = b"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement>SELECT FROM [Sales]</Statement></Command>
<Properties><PropertyList><Catalog>Sales</Catalog></PropertyList></Properties></Execute></soap:Body></soap:Envelope>"""
request = parse_request(body)
print(request.call)
