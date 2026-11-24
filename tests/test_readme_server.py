"""The server sketch in the README, run for real."""

import itertools
import xml.etree.ElementTree as ET

import pyarrow as pa

import arrow2xmla as ax

M = ax.Member("[H]", "[H].[L]", 1)
MEASURE = ax.Member("[Measures]", "[Measures].[ML]", 0, "[Measures].[Revenue]", "Revenue", member_type=ax.MemberType.MEASURE)


def run(mdx: str) -> ax.Cellset:
    return ax.Cellset("Sales", [ax.CellValues(pa.array([1.5, 2.5]), "0.0")], ax.TupleAxis.from_tuples([[MEASURE]]), ax.LevelAxis(M, pa.array(["a", "b"])))


def answer(body: bytes):
    """-> (HTTP status, an iterable of bytes)"""
    try:
        request = ax.parse_request(body, max_size=1 << 20)
    except ax.XmlaError as e:
        return 500, [ax.fault(str(e))]
    session = "new-id" if request.session.kind is ax.SessionKind.BEGIN else request.session.id
    match request.call:
        case ax.Discover(request_type="MDSCHEMA_CUBES"):
            rows = ax.Rowset.from_columns({"CUBE_NAME": pa.array(["Sales"]), "CUBE_TYPE": pa.array(["CUBE"])})
            return 200, [ax.Envelope(ax.Method.DISCOVER, session).wrap(rows.to_xml())]
        case ax.Execute(statement=mdx):
            envelope = ax.Envelope(ax.Method.EXECUTE, session)
            cellset = run(mdx)
            return 200, itertools.chain([envelope.head()], cellset.iter_xml(), [envelope.tail()])
    return 500, [ax.fault("not supported", session=session)]


def soap(inner: str, header: str = "") -> bytes:
    return (
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">{header}<s:Body>{inner}</s:Body></s:Envelope>'
    ).encode()


XA = 'xmlns="urn:schemas-microsoft-com:xml-analysis"'


def test_discover_execute_and_faults() -> None:
    status, parts = answer(soap(f"<Discover {XA}><RequestType>MDSCHEMA_CUBES</RequestType><Restrictions/><Properties/></Discover>"))
    assert status == 200 and b"<CUBE_NAME>Sales</CUBE_NAME>" in b"".join(parts)
    status, parts = answer(soap(f"<Execute {XA}><Command><Statement>SELECT FROM [Sales]</Statement></Command><Properties/></Execute>", f'<s:Header><BeginSession {XA}/></s:Header>'))
    assert not isinstance(parts, list), "the answer is lazy: not read until it is sent"
    xml = b"".join(parts)
    assert status == 200
    root = ET.fromstring(xml)  # the streamed parts are one well-formed envelope
    assert root.find("{http://schemas.xmlsoap.org/soap/envelope/}Header/{urn:schemas-microsoft-com:xml-analysis}Session").get("SessionId") == "new-id"
    status, parts = answer(b"not xml")
    assert status == 500 and b"<faultcode>" in b"".join(parts)
    status, parts = answer(soap(f"<Other {XA}/>"))
    assert status == 500
