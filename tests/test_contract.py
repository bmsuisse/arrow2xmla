"""The names the Python enums and the Rust core both know: a name added on one side and not the other fails here."""

import pyarrow as pa
import pytest

import arrow2xmla as ax

M = ax.Member("[H]", "[H].[L]", 1)


def cellset(**kw) -> ax.Cellset:
    return ax.Cellset("C", [ax.CellValues(pa.array([1.0]), "0")], ax.LevelAxis(M, pa.array(["a"]), **kw.pop("axis", {})), **kw)


@pytest.mark.parametrize("p", list(ax.MemberProperty))
def test_every_member_property_is_known_to_the_core(p: ax.MemberProperty) -> None:
    xml = cellset(axis={"properties": [p]}).to_xml().decode()
    assert f"[H].[{p.value}]" in xml


@pytest.mark.parametrize("p", list(ax.CellProperty))
def test_every_cell_property_is_known_to_the_core(p: ax.CellProperty) -> None:
    assert f'name="{p.value}"' in cellset(cell_properties=[p]).to_xml().decode()


def test_a_level_axis_takes_captions_directly() -> None:
    axis = ax.LevelAxis(M, pa.array(["~empty", "~~t"]), captions=pa.array(["(empty)", None]), blank_caption="-")
    xml = ax.Cellset("C", [ax.CellValues(pa.array([1.0, 2.0]), "0")], axis).to_xml().decode()
    assert "<UName>[H].&amp;[~empty]</UName><Caption>(empty)</Caption>" in xml
    assert "<UName>[H].&amp;[~~t]</UName><Caption>-</Caption>" in xml
    with pytest.raises(ax.CellsetError, match="captions"):
        ax.Cellset("C", [ax.CellValues(pa.array([1.0]), "0")], ax.LevelAxis(M, pa.array(["a"]), captions=pa.array(["x", "y"]))).to_xml()


def test_declared_cell_properties_keep_the_listed_order() -> None:
    props = [ax.CellProperty.BACK_COLOR, "VALUE", "FORE_COLOR", ax.CellProperty.FORMAT_STRING, "FONT_FLAGS"]
    xml = cellset(cell_properties=props).to_xml().decode()
    assert xml.index('name="BACK_COLOR"') < xml.index('name="VALUE"') < xml.index('name="FORE_COLOR"') < xml.index('name="FORMAT_STRING"') < xml.index('name="FONT_FLAGS"')
    assert "<BackColor>" not in xml


@pytest.mark.parametrize("t", list(ax.MemberType))
def test_every_member_type_is_known_to_the_core(t: ax.MemberType) -> None:
    m = ax.Member("[H]", "[H].[L]", 1, member_type=t)
    cs = ax.Cellset("C", [ax.CellValues(pa.array([1.0]), "0")], ax.TupleAxis.from_tuples([[m]], [ax.MemberProperty.MEMBER_TYPE]))
    assert f">{int(t)}</" in cs.to_xml().decode()


def test_the_default_request_limit_is_the_cores() -> None:
    with pytest.raises(ax.RequestTooLarge):
        ax.parse_request(b" " * ((4 << 20) + 1))


def test_the_core_owns_the_rules() -> None:
    bad = ax.Member("[H]", "[H].[L]", -1)
    with pytest.raises(ax.CellsetError, match="negative"):
        cellset(slicer=[bad]).to_xml()
    with pytest.raises(ax.CellsetError, match="twice"):
        cellset(axis={"properties": [ax.MemberProperty.MEMBER_CAPTION] * 2}).to_xml()


def test_the_default_request_limit_is_read_from_the_core() -> None:
    from arrow2xmla import _native, _request

    assert _request.DEFAULT_MAX_SIZE == _native.DEFAULT_MAX_REQUEST == 4 << 20


@pytest.mark.parametrize("bad", [-1, True, 2**70 + 0.5, "4"])
def test_a_bad_max_size_is_a_value_error(bad: object) -> None:
    with pytest.raises(ValueError):
        ax.parse_request(b"<a/>", bad)  # type: ignore[arg-type]


def test_tuples_are_checked_and_frozen() -> None:
    with pytest.raises(ax.CellsetError, match="not in members"):
        ax.TupleAxis([M], [[1]])
    with pytest.raises(ax.CellsetError, match="not in members"):
        ax.TupleAxis([M], [[-1]])
    with pytest.raises(ax.CellsetError, match="at least one member"):
        ax.Cellset("C", [ax.CellValues(pa.array([1.0]), "0")], ax.TupleAxis([M], [[]])).to_xml()
    t = ax.TupleAxis([M], [[0]])
    assert isinstance(t.tuples, tuple)


def test_a_huge_level_number_or_thread_count_is_an_xmla_error_or_clamped() -> None:
    with pytest.raises(ax.CellsetError):
        ax.Member("[H]", "[H].[L]", 2**40)
    assert cellset().to_xml(threads=2**70) == cellset().to_xml(threads=1)


def test_a_mapping_is_not_an_axis() -> None:
    with pytest.raises(TypeError):
        ax.Cellset("C", [ax.CellValues(pa.array([1.0]), "0")], {"keys": 1})  # type: ignore[arg-type]
