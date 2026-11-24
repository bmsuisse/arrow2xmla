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


def envelope(body: str, session: str | None) -> bytes:
    head = f'<soap:Header><Session xmlns="{XA}" SessionId="{session}"/></soap:Header>' if session else ""
    return f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="{SOAP}">{head}<soap:Body>{body}</soap:Body></soap:Envelope>'.encode()


def fault(message: str) -> bytes:
    from xml.sax.saxutils import escape

    return f'<?xml version="1.0"?><soap:Envelope xmlns:soap="{SOAP}"><soap:Body><soap:Fault><faultcode>XMLAnalysisError.0xc10e0000</faultcode><faultstring>{escape(message)}</faultstring></soap:Fault></soap:Body></soap:Envelope>'.encode()


class Server:
    """`with Server(table) as url:` serves XMLA on a free local port."""

    def __init__(self, table: Table, threads: int = 0) -> None:
        self.table, self.threads, self.seen = table, threads, []
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
            return 500, fault(str(e))
        self.seen.append(req)
        session = {ax.SessionKind.BEGIN: "mock-session"}.get(req.session.kind, req.session.id)
        call = req.call
        if isinstance(call, ax.Discover):
            return 200, envelope(f'<DiscoverResponse xmlns="{XA}"><return><root xmlns="{ROWSET}"/></return></DiscoverResponse>', session)
        if isinstance(call, ax.Execute):
            if not call.statement.strip():  # Excel's empty Execute that opens a session
                return 200, envelope(f'<ExecuteResponse xmlns="{XA}"><return><root xmlns="{MD}"/></return></ExecuteResponse>', session)
            cells = self.table.cellset().to_xml(self.threads).decode()
            return 200, envelope(f'<ExecuteResponse xmlns="{XA}"><return>{cells}</return></ExecuteResponse>', session)
        return 500, fault("only Discover and Execute are supported")

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
