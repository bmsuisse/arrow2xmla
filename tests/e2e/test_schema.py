"""Answers checked against Microsoft's mddataset schema. Skipped unless `lxml` is installed and the schemas can be had:

    uv run --with lxml pytest tests/e2e/test_schema.py          # fetches them into .schema-cache (see schemas.py)
    XMLA_SCHEMA_DIR=/path pytest ...                            # or use a directory that already has them
"""

import os
from pathlib import Path

import pyarrow as pa
import pytest
from xmla_mock import Table

import arrow2xmla as ax

etree = pytest.importorskip("lxml.etree")
from schemas import fetch  # noqa: E402


@pytest.fixture(scope="module")
def schema():
    directory = Path(os.environ.get("XMLA_SCHEMA_DIR", Path(__file__).parent / ".schema-cache"))
    try:
        patched = fetch(directory)
    except OSError as e:
        pytest.skip(f"the XMLA schemas are not available: {e}")
    return etree.XMLSchema(etree.parse(str(patched)))


def check(schema, xml: bytes) -> None:
    ok = schema.validate(etree.fromstring(xml))
    assert ok, "\n".join(f"line {e.line}: {e.message}" for e in list(schema.error_log)[:5])


def test_a_pivot_is_valid(schema) -> None:
    t = Table(pa.array(["Acme", None, "a&b <c> ]x]"]), pa.array([1.5, None, 2.0, 3.25]), pa.array([1, 2, 3, 2**40]))
    check(schema, t.cellset().to_xml())


def test_a_slicer_and_zero_axes_are_valid(schema) -> None:
    m = ax.Member("[Measures]", "[Measures].[MeasuresLevel]", 0, "[Measures].[Revenue]", "Revenue", member_type=ax.MemberType.MEASURE)
    one_cell = ax.Cellset("Sales", [ax.CellValues(pa.array([1.0]), "0")], slicer=[m])
    check(schema, one_cell.to_xml())


def test_an_empty_answer_is_valid(schema) -> None:
    t = Table(pa.array([], pa.string()), pa.array([None], pa.float64()), pa.array([None], pa.int64()))
    check(schema, t.cellset().to_xml())


def test_every_tuple_axis_with_properties_is_valid(schema) -> None:
    a = ax.Member("[H]", "[H].[L]", 1, "[H].[a]", "a", "[H].[All]", 2)
    b = ax.Member("[H]", "[H].[L]", 1, "[H].[b]", "b", "[H].[All]", 0)
    cs = ax.Cellset(
        "C", [ax.CellValues(pa.array(["x", None]), "@")],
        ax.TupleAxis.from_tuples([[a], [b]], list(ax.MemberProperty)),
    )  # fmt: skip
    check(schema, cs.to_xml())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.replace(b'CellOrdinal="0"', b'CellOrdinal="-1"', 1),
        lambda x: x.replace(b"<Axes>", b"<Axes><Bogus/>", 1),
        lambda x: x.replace(b"<CellData>", b"<Cells>", 1).replace(b"</CellData>", b"</Cells>", 1),
        lambda x: x.replace(b"<OlapInfo>", b"<Extra/><OlapInfo>", 1),
    ],
    ids=["negative-ordinal", "unknown-axes-child", "renamed-celldata", "element-before-olapinfo"],
)
def test_the_check_does_catch_a_broken_answer(schema, mutate) -> None:
    t = Table(pa.array(["a"]), pa.array([1.0, 2.0]), pa.array([1, 2]))
    assert not schema.validate(etree.fromstring(mutate(t.cellset().to_xml())))


def test_a_rowset_is_valid_without_its_schema_block(schema) -> None:
    """The mirror's rowset schema describes `<root><row>...` and has no place for the inline schema block real servers
    send, so the block is taken out before validating: this checks the rows and the namespaces."""
    from schemas import fetch  # noqa: F401

    directory = Path(os.environ.get("XMLA_SCHEMA_DIR", Path(__file__).parent / ".schema-cache"))
    rs = etree.XMLSchema(etree.parse(str(directory / "rowset-patched.xsd")))
    doc = etree.fromstring(ax.Rowset.from_columns({"CUBE_NAME": pa.array(["Sales", "a&b"]), "N": pa.array([1, None])}).to_xml())
    for block in doc.findall("{http://www.w3.org/2001/XMLSchema}schema"):
        doc.remove(block)
    assert rs.validate(doc), "\n".join(str(e) for e in list(rs.error_log)[:3])
