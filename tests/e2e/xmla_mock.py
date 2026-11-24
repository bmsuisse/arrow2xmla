"""A mock XMLA endpoint and a mock Excel client, for end-to-end tests over real HTTP.

The server is the smallest thing a pivot client can talk to: SOAP over HTTP, `parse_request` for what came in, a
`Cellset` for the answer to an Execute, an empty rowset for a Discover. Its "engine" answers every statement with the
same pivot (customers on rows, two measures on columns); what is tested is the wire, not MDX.

The client replays recorded Excel requests and reads each answer the way a client does: a strict XML parse, the axes
as tuples of members, the cells by ordinal, put back together as a grid.
"""

from __future__ import annotations

import threading
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pyarrow as pa

import arrow2xmla as ax

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
XA = "urn:schemas-microsoft-com:xml-analysis"
MD = "urn:schemas-microsoft-com:xml-analysis:mddataset"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ROWSET = "urn:schemas-microsoft-com:xml-analysis:rowset"

CUSTOMER = ax.Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
ALL = ax.Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=ax.MemberType.ALL)


def measure(name: str) -> ax.Member:
    return ax.Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=ax.MemberType.MEASURE)


@dataclass
class Table:
    """What the mock engine holds: customers and two measures (revenue, orders), with an All row first."""

    customers: pa.Array
    revenue: pa.Array  # one longer than customers: the All row first
    orders: pa.Array

    def cellset(self) -> ax.Cellset:
        return ax.Cellset(
            "Sales",
            [ax.CellValues(self.revenue, "#,##0.00"), ax.CellValues(self.orders, "0")],
            ax.TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
            ax.LevelAxis(CUSTOMER, self.customers, ALL, properties=[ax.MemberProperty.MEMBER_CAPTION]),
        )


def rowset(rows: list[dict]) -> bytes:
    """A Discover answer written by the library: the columns are the keys, in order of first appearance."""
    names = list(dict.fromkeys(k for r in rows for k in r))
    columns = []
    for name in names:
        values = [None if r.get(name) is None else r[name] for r in rows]
        if all(v is None or isinstance(v, str) for v in values):
            boolean = all(v in (None, "true", "false") for v in values) and any(v is not None for v in values)
            columns.append(ax.RowsetColumn(name, pa.array(values, pa.string()), "xsd:boolean" if boolean else None))
        else:
            columns.append(ax.RowsetColumn(name, pa.array(values, pa.int64())))
    return ax.Rowset(columns).to_xml()


def metadata(customers: list[str | None], cube: str = "Sales", catalog: str = "Demo") -> dict[str, list[dict]]:
    """The schema rowsets of the mock's one cube: a Customer dimension (All + names) and a Measures dimension."""
    base = {"CATALOG_NAME": catalog, "SCHEMA_NAME": None, "CUBE_NAME": cube}
    cust, meas = "[Customer]", "[Measures]"
    def member(hier: str, level: str, number: int, name: str, caption: str, ordinal: int, kind: int, parent: str, children: int) -> dict:
        unique = f"{hier}.[{name}]" if kind != 2 else f"{hier}.[All]"
        return {
            **base, "DIMENSION_UNIQUE_NAME": hier.split(".")[0], "HIERARCHY_UNIQUE_NAME": hier, "LEVEL_UNIQUE_NAME": level, "LEVEL_NUMBER": number,
            "MEMBER_ORDINAL": ordinal, "MEMBER_NAME": caption, "MEMBER_UNIQUE_NAME": unique, "MEMBER_TYPE": kind, "MEMBER_CAPTION": caption,
            "CHILDREN_CARDINALITY": children, "PARENT_LEVEL": max(number - 1, 0), "PARENT_UNIQUE_NAME": parent, "PARENT_COUNT": int(number > 0),
        }  # fmt: skip

    members = [member(meas, f"{meas}.[MeasuresLevel]", 0, "Revenue", "Revenue", 0, 3, "", 0), member(meas, f"{meas}.[MeasuresLevel]", 0, "Orders", "Orders", 1, 3, "", 0)]
    members.append(member(f"{cust}.[Name]", f"{cust}.[Name].[(All)]", 0, "All", "All", 0, 2, "", len(customers)))
    for i, name in enumerate(n for n in customers if n is not None):
        members.append(member(f"{cust}.[Name]", f"{cust}.[Name].[Name]", 1, name, name, i + 1, 1, f"{cust}.[Name].[All]", 0))
    return {
        "MDSCHEMA_MEMBERS": members,
        "DISCOVER_DATASOURCES": [{"DataSourceName": "Mock", "DataSourceDescription": "arrow2xmla mock", "DataSourceInfo": "Provider=arrow2xmla", "URL": "http://localhost/xmla", "ProviderName": "arrow2xmla", "ProviderType": "MDP", "AuthenticationMode": "Unauthenticated"}],
        "DBSCHEMA_CATALOGS": [{"CATALOG_NAME": catalog}],
        "MDSCHEMA_CUBES": [{**base, "CUBE_TYPE": "CUBE", "DESCRIPTION": "mock"}],
        "MDSCHEMA_DIMENSIONS": [
            {**base, "DIMENSION_NAME": "Customer", "DIMENSION_UNIQUE_NAME": cust, "DIMENSION_CAPTION": "Customer", "DIMENSION_ORDINAL": 1, "DIMENSION_TYPE": 3, "DEFAULT_HIERARCHY": f"{cust}.[Name]", "IS_VIRTUAL": "false", "IS_READWRITE": "false"},
            {**base, "DIMENSION_NAME": "Measures", "DIMENSION_UNIQUE_NAME": meas, "DIMENSION_CAPTION": "Measures", "DIMENSION_ORDINAL": 0, "DIMENSION_TYPE": 2, "DEFAULT_HIERARCHY": meas, "IS_VIRTUAL": "false", "IS_READWRITE": "false"},
        ],
        "MDSCHEMA_HIERARCHIES": [
            {**base, "DIMENSION_UNIQUE_NAME": cust, "HIERARCHY_NAME": "Name", "HIERARCHY_UNIQUE_NAME": f"{cust}.[Name]", "HIERARCHY_CAPTION": "Name", "DIMENSION_TYPE": 3, "HIERARCHY_CARDINALITY": 100, "DEFAULT_MEMBER": f"{cust}.[All]", "ALL_MEMBER": f"{cust}.[All]", "STRUCTURE": 0, "IS_VIRTUAL": "false", "IS_READWRITE": "false"},
            {**base, "DIMENSION_UNIQUE_NAME": meas, "HIERARCHY_NAME": "Measures", "HIERARCHY_UNIQUE_NAME": meas, "HIERARCHY_CAPTION": "Measures", "DIMENSION_TYPE": 2, "HIERARCHY_CARDINALITY": 2, "DEFAULT_MEMBER": f"{meas}.[Revenue]", "STRUCTURE": 0, "IS_VIRTUAL": "false", "IS_READWRITE": "false"},
        ],
        "MDSCHEMA_LEVELS": [
            {**base, "DIMENSION_UNIQUE_NAME": cust, "HIERARCHY_UNIQUE_NAME": f"{cust}.[Name]", "LEVEL_NAME": "(All)", "LEVEL_UNIQUE_NAME": f"{cust}.[Name].[(All)]", "LEVEL_CAPTION": "(All)", "LEVEL_NUMBER": 0, "LEVEL_CARDINALITY": 1, "LEVEL_TYPE": 1},
            {**base, "DIMENSION_UNIQUE_NAME": cust, "HIERARCHY_UNIQUE_NAME": f"{cust}.[Name]", "LEVEL_NAME": "Name", "LEVEL_UNIQUE_NAME": f"{cust}.[Name].[Name]", "LEVEL_CAPTION": "Name", "LEVEL_NUMBER": 1, "LEVEL_CARDINALITY": 100, "LEVEL_TYPE": 0},
            {**base, "DIMENSION_UNIQUE_NAME": meas, "HIERARCHY_UNIQUE_NAME": meas, "LEVEL_NAME": "MeasuresLevel", "LEVEL_UNIQUE_NAME": f"{meas}.[MeasuresLevel]", "LEVEL_CAPTION": "MeasuresLevel", "LEVEL_NUMBER": 0, "LEVEL_CARDINALITY": 2, "LEVEL_TYPE": 0},
        ],
        "MDSCHEMA_MEASURES": [
            {**base, "MEASURE_NAME": n, "MEASURE_UNIQUE_NAME": f"{meas}.[{n}]", "MEASURE_CAPTION": n, "MEASURE_AGGREGATOR": 1, "DATA_TYPE": 5, "MEASURE_IS_VISIBLE": "true"}
            for n in ("Revenue", "Orders")
        ],
    }


class Server:
    """`with Server(table) as url:` serves XMLA on a free local port."""

    def __init__(self, table: Table, threads: int = 0) -> None:
        self.table, self.threads, self.seen, self.meta = table, threads, [], metadata(table.customers.to_pylist())
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                status, out = outer.handle(body)
                self.send_response(status)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def handle(self, body: bytes) -> tuple[int, bytes]:
        try:
            req = ax.parse_request(body, max_size=1 << 20)
        except ax.XmlaError as e:
            return 500, ax.fault(str(e))
        self.seen.append(req)
        session = {ax.SessionKind.BEGIN: "mock-session"}.get(req.session.kind, req.session.id)
        call = req.call
        if isinstance(call, ax.Discover):
            rows = self.meta.get(call.request_type, [])
            for name, values in call.restrictions:  # a restriction keeps the rows whose column equals one of its values
                rows = [r for r in rows if r.get(name) is None or r[name] in values]
            return 200, ax.Envelope(ax.Method.DISCOVER, session).wrap(rowset(rows))
        if isinstance(call, ax.Execute):
            envelope = ax.Envelope(ax.Method.EXECUTE, session)
            if not call.statement.strip():  # Excel's empty Execute that opens a session
                return 200, envelope.wrap(f'<root xmlns="{MD}"/>'.encode())
            # streamed: the head, the answer as it is written, the tail
            return 200, b"".join([envelope.head(), *self.table.cellset().iter_xml(self.threads), envelope.tail()])
        return 500, ax.fault("only Discover and Execute are supported", session=session)

    def __enter__(self) -> str:
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.http.server_address[1]}/xmla"

    def __exit__(self, *_: object) -> None:
        self.http.shutdown()
        self.http.server_close()


def post(url: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(url, body, {"Content-Type": "text/xml", "SOAPAction": '"urn:schemas-microsoft-com:xml-analysis:Execute"'})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- the client's reading of an answer --------------------------------------------------------------------------------


@dataclass
class Pivot:
    columns: list[str]  # the column captions
    rows: list[str | None]  # the row captions
    cells: dict[int, object]  # ordinal -> value, typed by its xsi:type
    formats: dict[int, str]


def q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def read_pivot(answer: bytes) -> Pivot:
    """An Execute response read as a client reads it: strictly, axes first, cells by ordinal."""
    root = ET.fromstring(answer)  # a strict parse: anything XML forbids raises here
    ret = root.find(f".//{q(XA, 'return')}")
    assert ret is not None, "no <return> in the envelope"
    data = ret.find(q(MD, "root"))
    assert data is not None
    axes = {a.get("name"): a for a in data.iter(q(MD, "Axis"))}
    names = {"Axis0": [], "Axis1": []}
    for axis, out in ((axes["Axis0"], names["Axis0"]), (axes["Axis1"], names["Axis1"])):
        for tup in axis.iter(q(MD, "Tuple")):
            member = tup.find(q(MD, "Member"))
            out.append(member.findtext(q(MD, "Caption")))
    cells, formats = {}, {}
    for cell in data.iter(q(MD, "Cell")):
        ordinal = int(cell.get("CellOrdinal"))
        value = cell.find(q(MD, "Value"))
        kind = value.get(q(XSI, "type"))
        cells[ordinal] = {"xsd:int": int, "xsd:long": int, "xsd:unsignedLong": int, "xsd:double": float, "xsd:string": str}[kind](value.text or "")
        formats[ordinal] = cell.findtext(q(MD, "FormatString"))
    return Pivot(names["Axis0"], names["Axis1"], cells, formats)
