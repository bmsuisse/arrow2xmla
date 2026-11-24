"""The SOAP envelope, the fault and the rowset writers."""

import xml.etree.ElementTree as ET

import pyarrow as pa
import pytest

import arrow2xmla as ax

SOAP = "{http://schemas.xmlsoap.org/soap/envelope/}"
XA = "{urn:schemas-microsoft-com:xml-analysis}"
RS = "{urn:schemas-microsoft-com:xml-analysis:rowset}"
XSD = "{http://www.w3.org/2001/XMLSchema}"


@pytest.mark.parametrize("method", [ax.Method.DISCOVER, ax.Method.EXECUTE])
def test_the_envelope_wraps_a_body(method: ax.Method) -> None:
    e = ax.Envelope(method, "sess&1")
    root = ET.fromstring(e.wrap(b"<x/>"))
    assert root.tag == f"{SOAP}Envelope"
    assert root.find(f"{SOAP}Header/{XA}Session").get("SessionId") == "sess&1"
    assert root.find(f"{SOAP}Body/{XA}{method.value}Response/{XA}return/{XA}x") is not None


def test_head_body_tail_are_the_wrapped_answer() -> None:
    e = ax.Envelope(ax.Method.EXECUTE)
    assert e.head() + b"<x/>" + e.tail() == e.wrap(b"<x/>")
    assert b"<soap:Header>" not in e.wrap(b"")


def test_the_session_must_be_text() -> None:
    with pytest.raises(TypeError):
        ax.Envelope(ax.Method.EXECUTE, 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ax.Envelope("Nope")  # type: ignore[arg-type]


def test_a_fault() -> None:
    root = ET.fromstring(ax.fault('bad <MDX> & "q"', code=0xC10E0000, session="s1"))
    fault = root.find(f"{SOAP}Body/{SOAP}Fault")
    assert fault.findtext("faultcode") == "XMLAnalysisError.0xc10e0000"
    assert fault.findtext("faultstring") == 'bad <MDX> & "q"'
    assert fault.findtext("faultactor") == "Server"
    error = fault.find("detail/{urn:schemas-microsoft-com:xml-analysis:exception}Error")
    assert error.get("ErrorCode") == str(0xC10E0000) and error.get("Description") == 'bad <MDX> & "q"'
    assert root.find(f"{SOAP}Header/{XA}Session").get("SessionId") == "s1"


def test_a_fault_replaces_what_xml_forbids() -> None:
    assert "�" in ET.fromstring(ax.fault("a\x00b")).findtext(f"{SOAP}Body/{SOAP}Fault/faultstring")


@pytest.mark.parametrize("bad", [-1, 2**32, True, "1"])
def test_a_bad_fault_code(bad: object) -> None:
    with pytest.raises(ValueError):
        ax.fault("x", code=bad)  # type: ignore[arg-type]


def test_a_rowset_has_its_schema_and_rows() -> None:
    xml = ax.Rowset(
        [
            ax.RowsetColumn("CUBE_NAME", pa.array(["Sales", "a&b <c>", None])),
            ax.RowsetColumn("SIZE", pa.array([1, 2**40, None], pa.int64())),
            ax.RowsetColumn("RATIO", pa.array([0.5, None, 1e-7])),
            ax.RowsetColumn("IS_VIRTUAL", pa.array(["true", "false", "true"]), "xsd:boolean"),
        ]
    ).to_xml()
    root = ET.fromstring(xml)
    assert root.tag == f"{RS}root"
    declared = {e.get("name"): e.get("type") for e in root.iter(f"{XSD}element") if e.get("name") not in ("root", "row")}
    assert declared == {"CUBE_NAME": "xsd:string", "SIZE": "xsd:long", "RATIO": "xsd:double", "IS_VIRTUAL": "xsd:boolean"}
    rows = [{c.tag.removeprefix(RS): c.text for c in row} for row in root.findall(f"{RS}row")]
    assert rows == [
        {"CUBE_NAME": "Sales", "SIZE": "1", "RATIO": "0.5", "IS_VIRTUAL": "true"},
        {"CUBE_NAME": "a&b <c>", "SIZE": str(2**40), "IS_VIRTUAL": "false"},
        {"RATIO": "1e-07", "IS_VIRTUAL": "true"},
    ]


@pytest.mark.parametrize(
    ("array", "xsd"),
    [
        (pa.array([1], pa.int8()), "xsd:byte"), (pa.array([1], pa.int16()), "xsd:short"), (pa.array([1], pa.int32()), "xsd:int"),
        (pa.array([1], pa.uint8()), "xsd:unsignedByte"), (pa.array([1], pa.uint16()), "xsd:unsignedShort"),
        (pa.array([1], pa.int64()), "xsd:long"), (pa.array([1], pa.uint32()), "xsd:unsignedInt"),
        (pa.array([1], pa.uint64()), "xsd:unsignedLong"), (pa.array([1.0], pa.float32()), "xsd:double"),
        (pa.array([1.0], pa.float16()), "xsd:double"), (pa.array(["a"], pa.large_string()), "xsd:string"),
        (pa.array(["a"], pa.string_view()), "xsd:string"),
    ],
)  # fmt: skip
def test_the_declared_type_follows_the_array(array: pa.Array, xsd: str) -> None:
    root = ET.fromstring(ax.Rowset.from_columns({"C": array}).to_xml())
    assert next(e.get("type") for e in root.iter(f"{XSD}element") if e.get("name") == "C") == xsd


def test_an_empty_rowset() -> None:
    root = ET.fromstring(ax.Rowset([]).to_xml())
    assert root.findall(f"{RS}row") == []
    root = ET.fromstring(ax.Rowset.from_columns({"A": pa.array([], pa.string())}).to_xml())
    assert root.findall(f"{RS}row") == []


@pytest.mark.parametrize("bad", ["", "1A", "A B", "A<B", "é", "A/B"])
def test_a_bad_column_name_is_refused(bad: str) -> None:
    with pytest.raises(ax.CellsetError, match="column name"):
        ax.Rowset.from_columns({bad: pa.array([1])}).to_xml()


def test_columns_must_agree() -> None:
    with pytest.raises(ax.CellsetError, match="length"):
        ax.Rowset.from_columns({"A": pa.array([1]), "B": pa.array([1, 2])}).to_xml()
    with pytest.raises(ax.CellsetError, match="twice"):
        ax.Rowset([ax.RowsetColumn("A", pa.array([1])), ax.RowsetColumn("A", pa.array([2]))]).to_xml()
    with pytest.raises(ax.UnsupportedTypeError):
        ax.Rowset.from_columns({"A": pa.array([1], pa.timestamp("s"))}).to_xml()
    with pytest.raises(TypeError):
        ax.Rowset(["A"])  # type: ignore[list-item]


def test_a_big_rowset_is_complete() -> None:
    n = 200_000
    root = ET.fromstring(ax.Rowset.from_columns({"K": pa.array([f"k{i}&" for i in range(n)]), "V": pa.array(range(n))}).to_xml())
    rows = root.findall(f"{RS}row")
    assert len(rows) == n and rows[-1][0].text == f"k{n - 1}&" and rows[-1][1].text == str(n - 1)


@pytest.mark.parametrize("bad", ['x"/><evil/>', "boolean", "xsd:", "xsd:int32", ""])
def test_a_declared_type_cannot_inject_xml(bad: str) -> None:
    with pytest.raises(ax.CellsetError, match="not a type"):
        ax.Rowset([ax.RowsetColumn("A", pa.array([1]), bad)]).to_xml()


def test_booleans_and_nan() -> None:
    root = ET.fromstring(ax.Rowset.from_columns({"FLAG": pa.array([True, False, None]), "X": pa.array([float("nan"), float("inf"), -0.0])}).to_xml())
    declared = {e.get("name"): e.get("type") for e in root.iter(f"{XSD}element") if e.get("name") in ("FLAG", "X")}
    assert declared == {"FLAG": "xsd:boolean", "X": "xsd:double"}
    rows = [{c.tag.removeprefix(RS): c.text for c in row} for row in root.findall(f"{RS}row")]
    assert rows == [{"FLAG": "true", "X": "NaN"}, {"FLAG": "false", "X": "INF"}, {"X": "-0.0"}]


def test_wrap_takes_bytes_like_only() -> None:
    e = ax.Envelope(ax.Method.EXECUTE)
    assert e.wrap(bytearray(b"<x/>")) == e.wrap(b"<x/>") == e.wrap(memoryview(b"<x/>"))
    with pytest.raises(TypeError):
        e.wrap(3)  # type: ignore[arg-type]
