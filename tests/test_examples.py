"""The examples run: quickstart and streaming as subprocesses, the server over real HTTP with every recorded Excel request."""

import glob
import http.client
import importlib.util
import os
import pathlib
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import pytest

duckdb = pytest.importorskip("duckdb", reason="the examples use DuckDB")

ROOT = pathlib.Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"
FIXTURES = sorted(glob.glob(str(ROOT / "tests" / "fixtures" / "excel" / "*-request.xml")))
XA = "urn:schemas-microsoft-com:xml-analysis"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


def run_example(name: str, **env: str) -> str:
    out = subprocess.run([sys.executable, str(EXAMPLES / name)], capture_output=True, text=True, timeout=300, env={**os.environ, **env})
    assert out.returncode == 0, out.stderr[-800:]
    return out.stdout


def test_quickstart_runs_and_says_what_it_should() -> None:
    out = run_example("quickstart.py")
    assert "bytes of XMLA" in out and "MDX: SELECT FROM [Sales]" in out


def test_streaming_writes_the_same_answer_both_ways() -> None:
    out = run_example("streaming.py", EXAMPLE_ROWS="20000")
    assert "write_to : 20,000 customers" in out and "iter_xml :" in out
    assert "both give the same size:" in out


def test_no_example_imports_pyarrow() -> None:
    for path in EXAMPLES.glob("*.py"):
        assert "pyarrow" not in path.read_text().replace("pyarrow Table", ""), path.name


# ---- the server -------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server_module():
    spec = importlib.util.spec_from_file_location("example_server", EXAMPLES / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def url(server_module):
    server = server_module.build_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/xmla"
    server.shutdown()
    server.server_close()
    thread.join(10)
    assert not thread.is_alive(), "the server did not shut down"


def post(url: str, body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, body, {"Content-Type": "text/xml"})
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def soap(inner: str, header: str = "") -> bytes:
    return f'<s:Envelope xmlns:s="{SOAP}">{header}<s:Body>{inner}</s:Body></s:Envelope>'.encode()


def execute(statement: str = "SELECT FROM [Sales]", header: str = "") -> bytes:
    return soap(f'<Execute xmlns="{XA}"><Command><Statement>{statement}</Statement></Command><Properties/></Execute>', header)


def discover(kind: str, restrictions: str = "") -> bytes:
    return soap(f'<Discover xmlns="{XA}"><RequestType>{kind}</RequestType><Restrictions>{restrictions}</Restrictions><Properties/></Discover>')


def rows(answer: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(answer)
    return [{c.tag.rpartition("}")[2]: c.text for c in row} for row in root.iter("{urn:schemas-microsoft-com:xml-analysis:rowset}row")]


@pytest.mark.parametrize("path", FIXTURES, ids=os.path.basename)
def test_every_recorded_excel_request_gets_a_well_formed_answer(url: str, path: str) -> None:
    status, answer = post(url, pathlib.Path(path).read_bytes())
    assert status == 200, answer[:300]
    ET.fromstring(answer)


def test_the_recordings_are_there() -> None:
    assert len(FIXTURES) >= 20


def test_the_pivot_is_the_data(url: str) -> None:
    pytest.importorskip("pyarrow", reason="the pivot reader of the e2e mock imports it")
    sys.path.insert(0, str(ROOT / "tests" / "e2e"))
    from xmla_mock import read_pivot

    status, answer = post(url, execute())
    assert status == 200
    pivot = read_pivot(answer)

    con = duckdb.connect()  # the same figures, worked out here from the same rows
    con.sql("create table s as select * from (values ('Acme', 3, 1000.5), ('Acme', 2, 500.0), ('Globex', 1, 920.0), (NULL, 1, 77.25), ('Initech & Co', 4, 12.0), ('Hooli', 5, NULL)) t(c, o, a)")
    expected = con.sql("select c, sum(a), sum(o) from s group by c order by c nulls last").fetchall()
    total = con.sql("select sum(a), sum(o) from s").fetchall()[0]
    grid = [("All", *total)] + [("(blank)" if c is None else c, a, o) for c, a, o in expected]

    assert pivot.columns == ["Revenue", "Orders"]
    assert pivot.rows == [name for name, *_ in grid]
    assert pivot.rows[0] == "All" and pivot.rows[-1] == "(blank)"
    for r, (_, revenue, orders) in enumerate(grid):
        if revenue is None:
            assert 2 * r not in pivot.cells, f"row {r}: a null is a cell with no value"
        else:
            assert pivot.cells[2 * r] == pytest.approx(float(revenue))
        assert pivot.cells[2 * r + 1] == orders
    assert "Hooli" in pivot.rows and 2 * pivot.rows.index("Hooli") not in pivot.cells
    assert isinstance(pivot.cells[0], float) and isinstance(pivot.cells[1], int)
    assert set(pivot.formats.values()) == {"#,##0.00", "0"}


def test_the_pivot_is_streamed_in_chunks(url: str) -> None:
    host, port = url.removeprefix("http://").split("/")[0].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=30)
    conn.request("POST", "/xmla", execute())
    response = conn.getresponse()
    assert response.status == 200 and response.getheader("Transfer-Encoding") == "chunked"
    assert response.getheader("Content-Length") is None
    ET.fromstring(response.read())
    conn.close()


def test_an_empty_statement_is_an_empty_result(url: str) -> None:
    status, answer = post(url, execute(""))
    assert status == 200
    assert ET.fromstring(answer).find(f".//{{{XA}}}return/{{urn:schemas-microsoft-com:xml-analysis:mddataset}}root") is not None
    assert b"<Axis " not in answer


def test_sessions_are_begun_and_echoed(url: str) -> None:
    _, begun = post(url, execute("", f'<s:Header><BeginSession xmlns="{XA}"/></s:Header>'))
    session = ET.fromstring(begun).find(f"{{{SOAP}}}Header/{{{XA}}}Session").get("SessionId")
    assert session
    _, used = post(url, execute("", f'<s:Header><Session xmlns="{XA}" SessionId="abc"/></s:Header>'))
    assert b'SessionId="abc"' in used
    _, pivot = post(url, execute(header=f'<s:Header><Session xmlns="{XA}" SessionId="abc"/></s:Header>'))
    assert b'SessionId="abc"' in pivot


@pytest.mark.parametrize(
    "kind,column,expect",
    [
        ("DISCOVER_DATASOURCES", "DataSourceName", {"Demo"}),
        ("DBSCHEMA_CATALOGS", "CATALOG_NAME", {"Demo"}),
        ("MDSCHEMA_CUBES", "CUBE_NAME", {"Sales"}),
        ("MDSCHEMA_DIMENSIONS", "DIMENSION_NAME", {"Customer", "Measures"}),
        ("MDSCHEMA_HIERARCHIES", "HIERARCHY_UNIQUE_NAME", {"[Customer].[Name]", "[Measures]"}),
        ("MDSCHEMA_LEVELS", "LEVEL_UNIQUE_NAME", {"[Customer].[Name].[(All)]", "[Customer].[Name].[Name]", "[Measures].[MeasuresLevel]"}),
        ("MDSCHEMA_MEASURES", "MEASURE_NAME", {"Revenue", "Orders"}),
    ],
)
def test_discover_rowsets(url: str, kind: str, column: str, expect: set[str]) -> None:
    status, answer = post(url, discover(kind))
    assert status == 200
    assert {r[column] for r in rows(answer)} == expect


def test_members_and_restrictions(url: str) -> None:
    _, answer = post(url, discover("MDSCHEMA_MEMBERS"))
    members = [r["MEMBER_CAPTION"] for r in rows(answer)]
    assert members == ["Revenue", "Orders", "All", "Acme", "Globex", "Hooli", "Initech & Co"]  # the blank member is not listed
    one = '<RestrictionList><HIERARCHY_UNIQUE_NAME>[Measures]</HIERARCHY_UNIQUE_NAME></RestrictionList>'
    _, answer = post(url, discover("MDSCHEMA_MEMBERS", one))
    assert [r["MEMBER_CAPTION"] for r in rows(answer)] == ["Revenue", "Orders"]
    none = "<RestrictionList><CUBE_NAME>Nope</CUBE_NAME></RestrictionList>"
    assert rows(post(url, discover("MDSCHEMA_CUBES", none))[1]) == []
    injection = "<RestrictionList><CUBE_NAME>x') or 1=1 --</CUBE_NAME></RestrictionList>"
    assert rows(post(url, discover("MDSCHEMA_CUBES", injection))[1]) == []


def test_booleans_are_declared_and_another_discover_is_empty(url: str) -> None:
    _, answer = post(url, discover("MDSCHEMA_DIMENSIONS"))
    assert b'type="xsd:boolean"' in answer and rows(answer)[0]["IS_VIRTUAL"] == "false"
    status, answer = post(url, discover("MDSCHEMA_PROPERTIES"))
    assert status == 200 and rows(answer) == []


@pytest.mark.parametrize("bad", [b"", b"not xml", b'<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>', soap(f'<Other xmlns="{XA}"/>')])
def test_a_bad_request_is_a_fault_and_the_server_stays_up(url: str, bad: bytes) -> None:
    status, answer = post(url, bad)
    assert status == 500
    assert ET.fromstring(answer).find(f".//{{{SOAP}}}Fault") is not None
    assert post(url, execute())[0] == 200


def test_the_server_serves_concurrent_callers(url: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda _: post(url, execute()), range(16)))
    assert all(s == 200 for s, _ in answers)
    assert len({a for _, a in answers}) == 1, "concurrent answers differ"


def test_the_server_refuses_what_parse_request_does_not_accept(server_module) -> None:
    status, parts = server_module.answer(server_module.make_database(), b"x" * ((1 << 20) + 1))
    assert status == 500 and b"<faultcode>" in b"".join(parts)
