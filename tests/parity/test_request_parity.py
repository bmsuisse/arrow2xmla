"""The Rust request parser against Python's expat and ElementTree (the reference below), on real Excel requests
recorded from a PivotTable session (tests/fixtures/excel) and on edge cases."""

import glob
import os
import xml.etree.ElementTree as ET
from uuid import uuid4

import pytest

from xml.parsers import expat

import arrow2xmla
from arrow2xmla import Discover, Execute, SessionKind, XmlaError as _X  # noqa: F401


def parse_request(body: bytes) -> dict:
    """The typed request in the plain shape the reference reader produces."""
    r = arrow2xmla.parse_request(body)
    c = r.call
    if isinstance(c, Discover):
        call = {"kind": "discover", "request_type": c.request_type, "restrictions": [(k, list(v)) for k, v in c.restrictions], "properties": list(c.properties)}
    elif isinstance(c, Execute):
        call = {"kind": "execute", "statement": c.statement, "properties": list(c.properties)}
    else:
        call = {"kind": "other"}
    return {"session": (r.session.kind.value, r.session.id), "call": call}

XA = "urn:schemas-microsoft-com:xml-analysis"
NS = {"s": "http://schemas.xmlsoap.org/soap/envelope/", "x": XA}


class XmlaError(Exception):
    pass


def _no_doctype(body: bytes) -> None:
    """A DOCTYPE is refused by the parser itself, whatever the encoding (expat): the reference the Rust parser is held to."""

    def refuse(*_: object) -> None:
        raise XmlaError("DOCTYPE is not accepted")

    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = refuse
    p.EntityDeclHandler = refuse
    try:
        p.Parse(body, True)
    except expat.ExpatError as e:
        raise XmlaError(f"the request is not XML: {e}") from e


def _restrictions(root: ET.Element) -> dict[str, list[str]]:
    lst = root.find("x:Restrictions/x:RestrictionList", NS)
    if lst is None:
        return {}
    return {c.tag.split("}")[-1]: [v.text or "" for v in c.findall("x:Value", NS)] or [c.text or ""] for c in lst}


def _properties(root: ET.Element) -> dict[str, str]:
    lst = root.find("x:Properties/x:PropertyList", NS)
    return {} if lst is None else {c.tag.split("}")[-1]: c.text or "" for c in lst}

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "fixtures", "excel", "*request*.xml")))

EDGE = [
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><x:Execute xmlns:x="urn:schemas-microsoft-com:xml-analysis"><x:Command><x:Statement><![CDATA[SELECT <1> FROM [C]]]></x:Statement></x:Command><x:Properties><x:PropertyList><x:Format>Native</x:Format></x:PropertyList></x:Properties></x:Execute></s:Body></s:Envelope>',
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Header><BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis"/></s:Header><s:Body/></s:Envelope>',
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Header><EndSession xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="z&amp;1"/></s:Header><s:Body><Execute xmlns="urn:schemas-microsoft-com:xml-analysis"/></s:Body></s:Envelope>',
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><Discover xmlns="urn:schemas-microsoft-com:xml-analysis"><RequestType>X</RequestType><Restrictions><RestrictionList><A>a&#233;&lt;</A><B><Value>1</Value><Value/></B><A>last</A></RestrictionList></Restrictions></Discover></s:Body></s:Envelope>',
    b'<s:Envelope xmlns:s="http://other"><s:Body><Discover xmlns="urn:schemas-microsoft-com:xml-analysis"/></s:Body></s:Envelope>',
]


def reference(body: bytes) -> dict:
    """What service._handle reads out of a request."""
    _no_doctype(body)
    root = ET.fromstring(body)
    head = root.find("s:Header", NS)
    if head is None:
        sess = ("none", None)
    elif head.find("x:BeginSession", NS) is not None:
        sess = ("begin", None)
    else:
        end, use = head.find("x:EndSession", NS), head.find("x:Session", NS)
        sess = ("end", end.get("SessionId")) if end is not None else (("use", use.get("SessionId")) if use is not None and use.get("SessionId") else ("none", None))
    d = root.find("s:Body/x:Discover", NS)
    if d is not None:
        return {"session": sess, "call": {"kind": "discover", "request_type": d.findtext("x:RequestType", default="", namespaces=NS).strip(), "restrictions": [(k, v) for k, v in _restrictions(d).items()], "properties": list(_properties(d).items())}}
    x = root.find("s:Body/x:Execute", NS)
    if x is not None:
        return {"session": sess, "call": {"kind": "execute", "statement": x.findtext("x:Command/x:Statement", default="", namespaces=NS), "properties": list(_properties(x).items())}}
    return {"session": sess, "call": {"kind": "other"}}


def normalise(r: dict) -> dict:
    """Python's dicts keep one entry per name (the last value, at the first one's place); the Rust lists keep each."""
    c = dict(r["call"])
    for key in ("restrictions", "properties"):
        if key in c:
            d: dict = {}
            for k, v in c[key]:
                d[k] = v
            c[key] = list(d.items())
    return {"session": r["session"], "call": c}


@pytest.mark.parametrize("path", FIXTURES, ids=os.path.basename)
def test_real_excel_requests(path: str) -> None:
    body = open(path, "rb").read()
    assert normalise(parse_request(body)) == normalise(reference(body))


@pytest.mark.parametrize("body", EDGE)
def test_edge_cases(body: bytes) -> None:
    assert normalise(parse_request(body)) == normalise(reference(body))


def test_fixtures_were_found() -> None:
    assert len(FIXTURES) > 5


def test_doctype_refused_by_both() -> None:
    body = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>'
    with pytest.raises(XmlaError):
        _no_doctype(body)
    with pytest.raises(arrow2xmla.DoctypeError):
        parse_request(body)


@pytest.mark.parametrize("body", [b"", b"<a><b></a>", b"not xml", b"<a>&nope;</a>"])
def test_not_xml_refused_by_both(body: bytes) -> None:
    with pytest.raises((XmlaError, ET.ParseError)):
        _no_doctype(body)
        ET.fromstring(body)
    with pytest.raises(arrow2xmla.RequestError):
        parse_request(body)
