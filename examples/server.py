"""A complete XMLA endpoint in the standard library: `python examples/server.py [--port 8000]`.

Point an OLAP client at http://127.0.0.1:8000/ (or POST a recorded request with curl). The data is an in-memory DuckDB
`sales` table (customers x orders x amount; one customer has no amount, so its Revenue cell is empty); every answer is made from DuckDB queries:

- every POST is read with `parse_request(body, max_size=...)`; a request that is refused is answered with `fault(...)` and
  HTTP 500, as is anything that is not a Discover or an Execute;
- `BeginSession` gets a new session id and a client's own session is echoed back (`Envelope(Method, session)`);
- Discover answers DISCOVER_DATASOURCES, DBSCHEMA_CATALOGS, MDSCHEMA_CUBES, MDSCHEMA_DIMENSIONS, MDSCHEMA_HIERARCHIES,
  MDSCHEMA_LEVELS, MDSCHEMA_MEASURES and MDSCHEMA_MEMBERS as rowsets (`Rowset.from_columns(columns(con.sql(...)))`),
  keeps the rows the request's restrictions select, and answers any other Discover with an empty rowset;
- an Execute with an empty statement (what Excel sends to open a session) is an empty result; any other statement gets
  the same pivot, streamed in chunks: customers on rows with the All row first, Revenue and Orders on columns.

It does not parse MDX: that is the engine's job. A real server reads the statement and builds the Cellset it asks for;
here every statement is answered with the one pivot, which is all the wire needs.

It is `build_server(port)` plus `main()`, so a test can run it on port 0 (any free port).
"""

from __future__ import annotations

import argparse
import itertools
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import duckdb

import arrow2xmla as ax

MAX_REQUEST = 1 << 20  # a request body is untrusted: refuse more than 1 MiB before reading it
MD = "urn:schemas-microsoft-com:xml-analysis:mddataset"

# ---- the data ---------------------------------------------------------------------------------------------------------


def make_database() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.sql("""
        create table sales as select * from (values
            ('Acme', 3, 1000.5), ('Acme', 2, 500.0), ('Globex', 1, 920.0),
            (NULL, 1, 77.25), ('Initech & Co', 4, 12.0), ('Hooli', 5, NULL)
        ) t(customer, orders, amount)
    """)
    return con


# ---- the pivot ----------------------------------------------------------------------------------------------------------

CUSTOMER = ax.Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]")
ALL = ax.Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=ax.MemberType.ALL)
measure = lambda name: ax.Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{name}]", name, member_type=ax.MemberType.MEASURE)  # noqa: E731

BY_CUSTOMER = "select customer, sum(amount) as revenue, sum(orders) as orders from sales group by customer"


def pivot(con: duckdb.DuckDBPyConnection) -> ax.Cellset:
    """Customers on rows (NULL is the blank member, last), the All row first; Revenue and Orders on columns."""
    customers = ax.columns(con.sql(f"select customer from ({BY_CUSTOMER}) order by customer nulls last"))["customer"]
    cells = ax.columns(con.sql(f"""
        select revenue, orders from (
            select 0 as k, null as customer, sum(revenue) as revenue, sum(orders) as orders from ({BY_CUSTOMER})
            union all
            select 1, customer, revenue, orders from ({BY_CUSTOMER})
        ) order by k, customer nulls last
    """))
    return ax.Cellset(
        cube="Sales",
        columns_axis=ax.TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
        rows_axis=ax.LevelAxis(CUSTOMER, customers, all=ALL, properties=[ax.MemberProperty.MEMBER_CAPTION]),
        values=[ax.CellValues(cells["revenue"], "#,##0.00"), ax.CellValues(cells["orders"], "0")],
    )


# ---- the metadata: what Excel asks for before it asks for a pivot -----------------------------------------------------------

BASE = "'Demo' as CATALOG_NAME, null::varchar as SCHEMA_NAME, 'Sales' as CUBE_NAME"
CUST, MEAS = "[Customer]", "[Measures]"
FALSE = "'false'"  # booleans are text `true` / `false`, declared xsd:boolean (see BOOLEANS)
BOOLEANS = {"IS_VIRTUAL", "IS_READWRITE", "MEASURE_IS_VISIBLE"}

SCHEMA_ROWSETS = {
    "DISCOVER_DATASOURCES": """
        select 'Demo' as DataSourceName, 'arrow2xmla example' as DataSourceDescription, 'Provider=arrow2xmla' as DataSourceInfo,
               'http://localhost/xmla' as URL, 'arrow2xmla' as ProviderName, 'MDP' as ProviderType, 'Unauthenticated' as AuthenticationMode""",
    "DBSCHEMA_CATALOGS": "select 'Demo' as CATALOG_NAME",
    "MDSCHEMA_CUBES": f"select {BASE}, 'CUBE' as CUBE_TYPE, 'sales by customer' as DESCRIPTION",
    "MDSCHEMA_DIMENSIONS": f"""
        select {BASE}, * from (values
            ('Customer', '{CUST}', 'Customer', 1, 3, '{CUST}.[Name]', {FALSE}, {FALSE}),
            ('Measures', '{MEAS}', 'Measures', 0, 2, '{MEAS}', {FALSE}, {FALSE})
        ) t(DIMENSION_NAME, DIMENSION_UNIQUE_NAME, DIMENSION_CAPTION, DIMENSION_ORDINAL, DIMENSION_TYPE, DEFAULT_HIERARCHY, IS_VIRTUAL, IS_READWRITE)""",
    "MDSCHEMA_HIERARCHIES": f"""
        select {BASE}, * from (values
            ('{CUST}', 'Name', '{CUST}.[Name]', 'Name', 3, (select count(distinct customer) + 1 from sales), '{CUST}.[All]', '{CUST}.[All]', 0, {FALSE}, {FALSE}),
            ('{MEAS}', 'Measures', '{MEAS}', 'Measures', 2, 2, '{MEAS}.[Revenue]', null, 0, {FALSE}, {FALSE})
        ) t(DIMENSION_UNIQUE_NAME, HIERARCHY_NAME, HIERARCHY_UNIQUE_NAME, HIERARCHY_CAPTION, DIMENSION_TYPE, HIERARCHY_CARDINALITY, DEFAULT_MEMBER, ALL_MEMBER, STRUCTURE, IS_VIRTUAL, IS_READWRITE)""",
    "MDSCHEMA_LEVELS": f"""
        select {BASE}, * from (values
            ('{CUST}', '{CUST}.[Name]', '(All)', '{CUST}.[Name].[(All)]', '(All)', 0, 1, 1),
            ('{CUST}', '{CUST}.[Name]', 'Name', '{CUST}.[Name].[Name]', 'Name', 1, (select count(distinct customer) + 1 from sales), 0),
            ('{MEAS}', '{MEAS}', 'MeasuresLevel', '{MEAS}.[MeasuresLevel]', 'MeasuresLevel', 0, 2, 0)
        ) t(DIMENSION_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME, LEVEL_NAME, LEVEL_UNIQUE_NAME, LEVEL_CAPTION, LEVEL_NUMBER, LEVEL_CARDINALITY, LEVEL_TYPE)""",
    "MDSCHEMA_MEASURES": f"""
        select {BASE}, name as MEASURE_NAME, '{MEAS}.[' || name || ']' as MEASURE_UNIQUE_NAME, name as MEASURE_CAPTION,
               1 as MEASURE_AGGREGATOR, 5 as DATA_TYPE, 'true' as MEASURE_IS_VISIBLE
        from (values ('Revenue'), ('Orders')) t(name)""",
    # the two measures, the All member, then one member per customer (a customer with no name is the blank member: not listed)
    "MDSCHEMA_MEMBERS": f"""
        select {BASE}, * from (
            select '{MEAS}' as DIMENSION_UNIQUE_NAME, '{MEAS}' as HIERARCHY_UNIQUE_NAME, '{MEAS}.[MeasuresLevel]' as LEVEL_UNIQUE_NAME,
                   0 as LEVEL_NUMBER, row_number() over () - 1 as MEMBER_ORDINAL, name as MEMBER_NAME, '{MEAS}.[' || name || ']' as MEMBER_UNIQUE_NAME,
                   3 as MEMBER_TYPE, name as MEMBER_CAPTION, 0 as CHILDREN_CARDINALITY, 0 as PARENT_LEVEL, '' as PARENT_UNIQUE_NAME, 0 as PARENT_COUNT
            from (values ('Revenue'), ('Orders')) t(name)
            union all
            select '{CUST}', '{CUST}.[Name]', '{CUST}.[Name].[(All)]', 0, 0, 'All', '{CUST}.[Name].[All]', 2, 'All',
                   (select count(distinct customer) + 1 from sales), 0, '', 0
            union all
            select '{CUST}', '{CUST}.[Name]', '{CUST}.[Name].[Name]', 1, row_number() over (order by customer),
                   customer, '{CUST}.[Name].[' || replace(customer, ']', ']]') || ']', 1, customer, 0, 0, '{CUST}.[Name].[All]', 1
            from (select distinct customer from sales where customer is not null)
        )""",
}


def discover(con: duckdb.DuckDBPyConnection, request: ax.Discover) -> bytes:
    """The rowset for a Discover: from a query, keeping the rows the restrictions select (a null column always matches)."""
    sql = SCHEMA_ROWSETS.get(request.request_type)
    if sql is None:
        return ax.Rowset([]).to_xml()  # any other Discover: an empty rowset
    names = con.sql(sql).columns
    where, params = [], []
    for name, values in request.restrictions:
        if name in names and values:  # only a column of this rowset is filtered on (the name is checked, the values are parameters)
            where.append(f'("{name}" is null or cast("{name}" as varchar) in ({", ".join("?" * len(values))}))')
            params += values
    query = f"select * from ({sql}) t" + (" where " + " and ".join(where) if where else "")
    table = ax.columns(con.sql(query, params=params))
    columns = [ax.RowsetColumn(name, values, "xsd:boolean" if name in BOOLEANS else None) for name, values in table.items()]
    return ax.Rowset(columns).to_xml()


# ---- the wire ---------------------------------------------------------------------------------------------------------------


def answer(con: duckdb.DuckDBPyConnection, body: bytes):
    """-> (HTTP status, a list of bytes, or an iterator of bytes to stream)."""
    session = None
    try:
        request = ax.parse_request(body, max_size=MAX_REQUEST)
        session = {ax.SessionKind.BEGIN: str(uuid.uuid4()), ax.SessionKind.USE: request.session.id}.get(request.session.kind)
        match request.call:
            case ax.Discover() as call:
                return 200, [ax.Envelope(ax.Method.DISCOVER, session).wrap(discover(con, call))]
            case ax.Execute(statement=mdx):
                envelope = ax.Envelope(ax.Method.EXECUTE, session)
                if not mdx.strip():  # the empty Execute that opens a session
                    return 200, [envelope.wrap(f'<root xmlns="{MD}"/>'.encode())]
                chunks = pivot(con).iter_xml()
                first = next(chunks)  # whatever is wrong with the cellset is raised here, while a fault can still be sent
                return 200, itertools.chain([envelope.head(), first], chunks, [envelope.tail()])
        return 500, [ax.fault("only Discover and Execute are supported", session=session)]
    except ax.XmlaError as e:
        return 500, [ax.fault(str(e), session=session)]
    except Exception as e:  # the engine failed: say so, and stay up
        return 500, [ax.fault(f"internal error: {type(e).__name__}: {e}", session=session)]


def build_server(port: int = 8000) -> ThreadingHTTPServer:
    """An XMLA endpoint on 127.0.0.1:`port` (0: any free port; see `server.server_address`). Not started: `serve_forever()`."""
    db = make_database()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, and chunked transfer for a streamed answer

        def log_message(self, *_: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST:  # not read: close the connection after the refusal
                self.close_connection = True
                return self.send_parts(500, [ax.fault(f"the request is larger than {MAX_REQUEST} bytes")])
            status, parts = answer(db.cursor(), self.rfile.read(length))  # a cursor per request: a connection is not shared
            self.send_parts(status, parts)

        def send_parts(self, status: int, parts) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            if isinstance(parts, list):
                self.send_header("Content-Length", str(sum(map(len, parts))))
                self.end_headers()
                for part in parts:
                    self.wfile.write(part)
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for part in parts:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                self.wfile.write(b"0\r\n\r\n")
            except Exception:  # a closed socket, or the writer failed mid-stream: the answer is cut, so cut the connection
                self.close_connection = True
            finally:
                getattr(parts, "close", lambda: None)()

    class Server(ThreadingHTTPServer):
        request_queue_size = 128  # the default of 5 resets connections when many clients arrive at once
        daemon_threads = True

    server = Server(("127.0.0.1", port), Handler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="an XMLA endpoint over DuckDB, with arrow2xmla")
    parser.add_argument("--port", type=int, default=8000)
    server = build_server(parser.parse_args().port)
    print(f"XMLA endpoint on http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C stops it)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
