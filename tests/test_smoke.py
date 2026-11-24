"""A published wheel works: it imports, writes a cellset and parses a request. Needs only pyarrow."""

import pyarrow as pa
import pytest

import arrow2xmla

XA = "urn:schemas-microsoft-com:xml-analysis"


def member(**kw: object) -> dict:
    return {"hierarchy": "[T].[F]", "level_name": "[T].[F].[F]", "level_number": 1, **kw}


def test_cellset() -> None:
    xml = arrow2xmla.cellset_xml(
        {
            "cube": "C",
            "axes": [{"level": {"template": member(), "keys": pa.array(["a&b", None])}}],
            "columns": [(pa.array([1.5, 2]), "#,##0")],
        }
    ).decode()
    assert "<CubeName>C</CubeName>" in xml
    assert "<Caption>a&amp;b</Caption>" in xml and "<Caption>(blank)</Caption>" in xml
    assert '<Value xsi:type="xsd:double">1.5</Value>' in xml and '<Value xsi:type="xsd:double">2.0</Value>' in xml


def test_chunks_join_to_the_same_bytes() -> None:
    spec = {"cube": "C", "axes": [{"level": {"template": member(), "keys": pa.array([f"k{i}" for i in range(50_000)])}}], "columns": [(pa.array(range(50_000)), "0")]}
    assert b"".join(arrow2xmla.cellset_xml_chunks(spec)) == arrow2xmla.cellset_xml(spec)


def test_request() -> None:
    body = f'<E xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body><Execute xmlns="{XA}"><Command><Statement>SELECT 1</Statement></Command></Execute></Body></E>'.encode()
    r = arrow2xmla.parse_request(body)
    assert r["call"] == {"kind": "execute", "statement": "SELECT 1", "properties": []}


def test_doctype_is_refused() -> None:
    with pytest.raises(ValueError, match="doctype"):
        arrow2xmla.parse_request(b'<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>')
