"""End to end over HTTP: recorded Excel requests in, a mock endpoint answering with arrow2xmla, read back as a client reads."""

import glob
import os

import pyarrow as pa
import pytest
from xmla_mock import Server, Table, post, read_pivot

import arrow2xmla as ax

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "fixtures", "excel", "*-request.xml")))
NAMES = ["Acme", "Globex", None, "Initech & Co", "Ünï <cödé> ]x]"]


def _statements() -> list[str]:
    out = []
    for p in FIXTURES:
        call = ax.parse_request(open(p, "rb").read()).call
        if isinstance(call, ax.Execute) and call.statement.strip():
            out.append(p)
    return out


EXECUTES = _statements()
TABLE = Table(
    pa.array(NAMES),
    pa.array([2509.75, 1500.5, 920.0, None, 89.25, 0.0]),
    pa.array([49, 30, 12, 1, 4, 2]),
)


@pytest.fixture(scope="module")
def url():
    with Server(TABLE) as u:
        yield u


@pytest.mark.parametrize("path", FIXTURES, ids=os.path.basename)
def test_every_recorded_excel_request_gets_a_well_formed_answer(url: str, path: str) -> None:
    status, answer = post(url, open(path, "rb").read())
    assert status == 200, answer[:300]
    import xml.etree.ElementTree as ET

    ET.fromstring(answer)  # well-formed
    req = ax.parse_request(open(path, "rb").read())
    if isinstance(req.call, ax.Execute) and req.call.statement.strip():
        read_pivot(answer)


def test_the_recordings_have_real_executes() -> None:
    assert len(EXECUTES) >= 3


def test_the_pivot_comes_back_as_the_data(url: str) -> None:
    execute = EXECUTES[0]
    _, answer = post(url, open(execute, "rb").read())
    pivot = read_pivot(answer)
    assert pivot.columns == ["Revenue", "Orders"]
    assert pivot.rows == ["All", "Acme", "Globex", "(blank)", "Initech & Co", "Ünï <cödé> ]x]"]
    n = len(pivot.columns)
    revenue = TABLE.revenue.to_pylist()
    orders = TABLE.orders.to_pylist()
    for r in range(len(pivot.rows)):
        if revenue[r] is None:
            assert r * n not in pivot.cells, "a null is a cell with no value"
        else:
            assert pivot.cells[r * n] == revenue[r]
        assert pivot.cells[r * n + 1] == orders[r]
    assert isinstance(pivot.cells[1], int) and isinstance(pivot.cells[0], float)
    assert set(pivot.formats.values()) == {"#,##0.00", "0"}


def test_the_session_is_echoed(url: str) -> None:
    body = lambda head: (
        f'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">{head}<soap:Body><Execute xmlns="urn:schemas-microsoft-com:xml-analysis">'
        "<Command><Statement>SELECT FROM [Sales]</Statement></Command><Properties/></Execute></soap:Body></soap:Envelope>"
    ).encode()
    _, begun = post(url, body('<soap:Header><BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis" mustUnderstand="1"/></soap:Header>'))
    assert b'SessionId="mock-session"' in begun
    _, used = post(url, body('<soap:Header><Session xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="abc"/></soap:Header>'))
    assert b'SessionId="abc"' in used


@pytest.mark.parametrize("bad", [b"", b"not xml", b'<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>', b"<a><b></a>"])
def test_a_bad_request_is_a_fault_not_a_crash(url: str, bad: bytes) -> None:
    status, answer = post(url, bad)
    assert status == 500 and b"<soap:Fault>" in answer
    post(url, open(FIXTURES[0], "rb").read())  # and the server is still there


def test_a_big_answer_over_http_is_complete_and_parallel_safe() -> None:
    n = 200_000
    big = Table(
        pa.array([f"Customer {i} & Co" for i in range(n)]),
        pa.array([float(i) / 7 for i in range(n + 1)]),
        pa.array(range(n + 1)),
    )
    execute = EXECUTES[0]
    body = open(execute, "rb").read()
    from concurrent.futures import ThreadPoolExecutor

    with Server(big) as u, ThreadPoolExecutor(4) as pool:
        answers = list(pool.map(lambda _: post(u, body), range(8)))
    assert all(s == 200 for s, _ in answers)
    assert len({a for _, a in answers}) == 1, "concurrent answers differ"
    pivot = read_pivot(answers[0][1])
    assert len(pivot.rows) == n + 1 and len(pivot.cells) == 2 * (n + 1)
    assert pivot.cells[2 * 123_456] == pytest.approx(123_456 / 7) and pivot.cells[2 * 123_456 + 1] == 123_456
    assert pivot.rows[-1] == f"Customer {n - 1} & Co"
