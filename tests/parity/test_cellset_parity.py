import math
import random
from types import SimpleNamespace

import pyarrow as pa
import pytest

from compat import cellset_xml
import reference as query
import reference as service
from reference import member_uname

H = "[Customer].[Name]"
ALL = "All"
SPECIAL = ["plain", "a&b", "x<y>z", 'q"uote', "it's", "br]acket", "a]]b", "é ü 日本", "tab\there", "", " lead", "trail "]


def member_dict(m: query.Member) -> dict:
    return dict(
        hierarchy=m.hierarchy, unique_name=m.unique, caption=m.caption, level_name=m.level, level_number=m.number,
        parent_unique_name=m.parent, children_cardinality=m.children, member_type=m.kind,
    )  # fmt: skip


def all_member(h: str = H) -> query.Member:
    return query.Member(h, f"{h}.[All]", "All", f"{h}.[(All)]", 0, children=1000, kind=2)


def value_member(h: str, v: object, parent: str | None = None) -> query.Member:
    return query.Member(h, member_uname(h, v), str(v), f"{h}.[Name]", 1, parent=parent if parent is not None else f"{h}.[All]")


def measure_member(label: str) -> query.Member:
    return query.Member("[Measures]", f"[Measures].[{label}]", label, "[Measures].[MeasuresLevel]", 0, kind=3)


def python_cells(columns: list[tuple[pa.Array, str]]) -> dict:
    cols = [(c.to_pylist(), f) for c, f in columns]
    n = len(cols)
    return {r * n + k: (vals[r], fmt) for k, (vals, fmt) in enumerate(cols) for r in range(len(vals)) if vals[r] is not None}


def expected(cube, axes, cells, slicer, props, cell_props, light=None) -> str:
    cs = query.Cellset(cube, axes, cells, tuple(slicer), props, cell_props, light or {})
    return service.cellset_xml(cs)


def check(spec: dict, py: str) -> None:
    got = cellset_xml(spec).decode()
    assert got == py


CUBE = SimpleNamespace(name="Sales & <Cube>")


@pytest.mark.parametrize("seed", range(40))
def test_light_axis_one_measure(seed: int) -> None:
    rnd = random.Random(seed)
    n = rnd.choice([0, 1, 2, 7, 60])
    keys = [rnd.choice(SPECIAL) + (str(i) if rnd.random() < 0.7 else "") for i in range(n)]
    keys = list(dict.fromkeys(keys))
    if keys and rnd.random() < 0.3:
        keys[rnd.randrange(len(keys))] = None
    with_all = rnd.random() < 0.7
    values = [rnd.choice([rnd.uniform(-1e6, 1e6), rnd.random() * 10 ** rnd.randint(-12, 25), float(rnd.randint(0, 10**6)), None]) for _ in range(len(keys) + with_all)]
    prop_sets = [(), ("MEMBER_CAPTION", "PARENT_UNIQUE_NAME", "MEMBER_UNIQUE_NAME"), ("CHILDREN_CARDINALITY", "MEMBER_TYPE", "LEVEL_NUMBER", "PARENT_COUNT", "HIERARCHY_UNIQUE_NAME", "LEVEL_UNIQUE_NAME", "PARENT_LEVEL")]
    props = rnd.choice(prop_sets)
    cell_props = rnd.choice([("VALUE", "FORMAT_STRING"), ("VALUE", "FORMAT_STRING", "LANGUAGE"), ("FORMAT_STRING", "LANGUAGE"), ("VALUE",), ("LANGUAGE", "VALUE")])
    fmt = rnd.choice(["#,##0", '"CHF" #,##0.00', "0.0%", "a&b<c>"])
    show = [None] * 0
    shown_keys = keys
    members_all = all_member() if with_all else None
    first = members_all or (value_member(H, keys[0]) if keys else None)
    light = query.LightAxis(value_member(H, "x"), members_all, shown_keys)
    dim_members = [(first,)] if first is not None else []
    col = pa.array(values[: len(keys) + with_all], type=pa.float64())
    cells = python_cells([(col, fmt)])
    py = expected(CUBE, [dim_members], cells, [measure_member("Sales")], [props], cell_props, {0: light})
    spec = {
        "cube": CUBE.name, "cell_properties": list(cell_props),
        "axes": [{"properties": list(props), "level": {"all": member_dict(members_all) if members_all else None, "template": member_dict(value_member(H, "x")), "keys": pa.array(shown_keys, type=pa.string())}}],
        "slicer": [member_dict(measure_member("Sales"))], "columns": [(col, fmt)],
    }  # fmt: skip
    check(spec, py)


@pytest.mark.parametrize("seed", range(20))
def test_measures_on_columns_field_on_rows(seed: int) -> None:
    rnd = random.Random(1000 + seed)
    n = rnd.choice([1, 3, 25])
    keys = [f"{rnd.choice(SPECIAL)}{i}" for i in range(n)]
    labels = ["Sales", "Qty", "Margin %"][: rnd.randint(1, 3)]
    rows = n + 1
    cols = []
    for k, label in enumerate(labels):
        if k == 1:
            arr = pa.array([rnd.choice([rnd.randint(-5, 10**12), None]) for _ in range(rows)], type=pa.int64())
        else:
            arr = pa.array([rnd.choice([rnd.gauss(0, 1e5), None, float("nan"), float("inf"), -0.0, 1e-7]) for _ in range(rows)], type=pa.float64())
        cols.append((arr, ["#,##0", "0", "0.0%"][k]))
    props = ("MEMBER_CAPTION",)
    light = query.LightAxis(value_member(H, "x"), all_member(), keys)
    measure_tuples = [(measure_member(lb),) for lb in labels]
    py = expected(CUBE, [measure_tuples, [(all_member(),)]], python_cells(cols), [], [(), props], ("VALUE", "FORMAT_STRING"), {1: light})
    spec = {
        "cube": CUBE.name,
        "axes": [
            {"tuples": [[member_dict(m) for m in t] for t in measure_tuples]},
            {"properties": list(props), "level": {"all": member_dict(all_member()), "template": member_dict(value_member(H, "x")), "keys": pa.array(keys, type=pa.large_string())}},
        ],
        "columns": cols,
    }  # fmt: skip
    check(spec, py)


def test_string_cells_and_int_types() -> None:
    cols = [(pa.array(["a&b", None, "x<y"], pa.string()), "@"), (pa.array([1, 2, None], pa.int32()), "0"), (pa.array([1.5, None, 3.0], pa.float32()), "0.0")]
    keys = ["k1", "k2", "k3"]
    light = query.LightAxis(value_member(H, "x"), None, keys)
    measures = [(measure_member(n),) for n in "ABC"]
    py = expected(CUBE, [measures, [(value_member(H, "k1"),)]], python_cells(cols), [], [(), ()], ("VALUE", "FORMAT_STRING"), {1: light})
    spec = {
        "cube": CUBE.name,
        "axes": [{"tuples": [[member_dict(m) for m in t] for t in measures]}, {"level": {"template": member_dict(value_member(H, "x")), "keys": pa.array(keys)}}],
        "columns": cols,
    }  # fmt: skip
    check(spec, py)


def hier(name: str) -> str:
    return f"[T].[{name}]"


@pytest.mark.parametrize("seed", range(30))
def test_explicit_tuples_display_info(seed: int) -> None:
    """Several hierarchies nested on an axis, with All and drilled members: DisplayInfo comes out as in Python."""
    rnd = random.Random(2000 + seed)
    h1, h2 = hier("A"), hier("B")
    a_keys = rnd.sample(SPECIAL, rnd.randint(1, 4))
    b_keys = rnd.sample(SPECIAL, rnd.randint(1, 4))
    tuples = []
    for a in [None, *a_keys]:
        ma = all_member(h1) if a is None else value_member(h1, a)
        tuples.append((ma, all_member(h2)) if rnd.random() < 0.8 else (ma,) * 0 or (ma, all_member(h2)))
        if a is not None or rnd.random() < 0.5:
            for b in b_keys:
                tuples.append((ma, value_member(h2, b)))
    tuples = [t for t in tuples if rnd.random() < 0.9] or tuples
    props = (("MEMBER_CAPTION", "PARENT_UNIQUE_NAME"), ())[rnd.randrange(2)]
    cols = [(pa.array([float(i) for i in range(len(tuples))], pa.float64()), "#,##0")]
    py = expected(CUBE, [tuples], python_cells(cols), [all_member(h1), measure_member("X")], [props], ("VALUE", "FORMAT_STRING"))
    spec = {"cube": CUBE.name, "axes": [{"properties": list(props), "tuples": [[member_dict(m) for m in t] for t in tuples]}], "slicer": [member_dict(all_member(h1)), member_dict(measure_member("X"))], "columns": cols}
    check(spec, py)


def test_empty_cellset() -> None:
    py = expected(CUBE, [[]], {}, [], [()], ("VALUE", "FORMAT_STRING"))
    check({"cube": CUBE.name, "axes": [{"tuples": []}]}, py)


def test_large_floats_repr() -> None:
    rnd = random.Random(7)
    vals = [rnd.random() * 10 ** rnd.randint(-30, 30) * rnd.choice([1, -1]) for _ in range(20000)] + [5e-324, 1.7976931348623157e308, 1e16, 1e15, 9999999999999998.0, 0.0001, 0.00009999]
    assert all(math.isfinite(v) for v in vals)
    keys = [f"k{i}" for i in range(len(vals))]
    col = pa.array(vals, pa.float64())
    light = query.LightAxis(value_member(H, "x"), None, keys)
    py = expected(CUBE, [[(value_member(H, "k0"),)]], python_cells([(col, "0")]), [], [()], ("VALUE", "FORMAT_STRING"), {0: light})
    spec = {"cube": CUBE.name, "axes": [{"level": {"template": member_dict(value_member(H, "x")), "keys": pa.array(keys)}}], "columns": [(col, "0")]}
    check(spec, py)


def test_indexed_tuples_are_the_same_as_member_tuples() -> None:
    h1, h2 = hier("A"), hier("B")
    mem = [all_member(h1), value_member(h1, "x"), all_member(h2), value_member(h2, "y"), value_member(h2, "z")]
    idx = [[0, 2], [0, 3], [0, 4], [1, 2], [1, 3]]
    cols = [(pa.array([1.0, 2.0, 3.0, 4.0, 5.0]), "0")]
    by_member = {"cube": CUBE.name, "axes": [{"tuples": [[member_dict(mem[k]) for k in t] for t in idx]}], "columns": cols}
    by_index = {"cube": CUBE.name, "axes": [{"members": [member_dict(m) for m in mem], "tuples": idx}], "columns": cols}
    py = expected(CUBE, [[tuple(mem[k] for k in t) for t in idx]], python_cells(cols), [], [()], ("VALUE", "FORMAT_STRING"))
    check(by_member, py)
    check(by_index, py)


def test_threads_do_not_change_a_byte() -> None:
    n = 50_000
    keys = pa.array([f"k{i} & <{i}> ]" for i in range(n)])
    spec = {
        "cube": "C",
        "axes": [
            {"tuples": [[member_dict(measure_member("A"))], [member_dict(measure_member("B"))]]},
            {"properties": ["MEMBER_CAPTION"], "level": {"template": member_dict(value_member(H, "x")), "keys": keys}},
        ],
        "columns": [(pa.array([i * 0.37 for i in range(n)]), "0"), (pa.array(range(n)), "0")],
    }  # fmt: skip
    one = cellset_xml(spec, threads=1)
    assert one == cellset_xml(spec, threads=3) == cellset_xml(spec, threads=0)


CAPTION_KEYS = ["~empty", "~~tilde", "~tilde", "", "a]b", "x]]y", "a&b<c>", "é 日本", "tab\there", "it's \"q\"", "k\r\nn"]


def level_spec(keys, captions, with_all, props, col, **level):
    return {
        "cube": CUBE.name,
        "axes": [{"properties": list(props), "level": {"all": member_dict(all_member()) if with_all else None, "template": member_dict(value_member(H, "x")), "keys": pa.array(keys, type=pa.string()), "captions": None if captions is None else pa.array(captions, type=pa.string()), **level}}],
        "slicer": [member_dict(measure_member("Sales"))], "columns": [(col, "0")],
    }  # fmt: skip


def level_expected(keys, captions, with_all, props, col, **level):
    members_all = all_member() if with_all else None
    light = query.LightAxis(value_member(H, "x"), members_all, keys, captions, **level)
    first = members_all or value_member(H, keys[0])
    return expected(CUBE, [[(first,)]], python_cells([(col, "0")]), [measure_member("Sales")], [props], ("VALUE", "FORMAT_STRING"), {0: light})


@pytest.mark.parametrize("seed", range(30))
@pytest.mark.parametrize("threads", [1, 3])
def test_separate_captions(seed: int, threads: int) -> None:
    rnd = random.Random(5000 + seed)
    n = rnd.choice([1, 2, 9, 40])
    keys = [rnd.choice(CAPTION_KEYS) + (str(i) if rnd.random() < 0.5 else "") for i in range(n)]
    captions = [rnd.choice(CAPTION_KEYS + ["(empty)", "(blank)", "Acme & Co"]) for _ in range(n)]
    for i in range(n):
        if rnd.random() < 0.15:
            keys[i] = None
        if rnd.random() < 0.15:
            captions[i] = None
    with_all = rnd.random() < 0.6
    props = rnd.choice([(), ("MEMBER_CAPTION", "MEMBER_UNIQUE_NAME"), ("MEMBER_UNIQUE_NAME", "PARENT_UNIQUE_NAME", "MEMBER_CAPTION")])
    col = pa.array([float(i) for i in range(n + with_all)], type=pa.float64())
    extra = {"blank_caption": rnd.choice(["(blank)", "(none) & <x>"])} if rnd.random() < 0.5 else {}
    py = level_expected(keys, captions, with_all, props, col, **extra)
    got = cellset_xml(level_spec(keys, captions, with_all, props, col, **extra), threads=threads).decode()
    assert got == py


def test_captions_in_parallel_chunks_do_not_change_a_byte() -> None:
    n = 50_000
    keys = [f"{CAPTION_KEYS[i % len(CAPTION_KEYS)]}{i}" for i in range(n)]
    captions = [None if i % 7 == 0 else f"c{i} & <{i}> ]" for i in range(n)]
    col = pa.array([i * 0.5 for i in range(n)])
    spec = level_spec(keys, captions, False, ("MEMBER_CAPTION", "MEMBER_UNIQUE_NAME"), col)
    py = level_expected(keys, captions, False, ("MEMBER_CAPTION", "MEMBER_UNIQUE_NAME"), col)
    one = cellset_xml(spec, threads=1)
    assert one.decode() == py
    assert one == cellset_xml(spec, threads=3) == cellset_xml(spec, threads=0)


def test_keys_are_written_as_given_without_captions() -> None:
    """The default: no special handling of an empty key or one that starts with a tilde."""
    col = pa.array([1.0, 2.0, 3.0])
    out = cellset_xml(level_spec(["", "~tilde", "a]b"], None, False, ("MEMBER_CAPTION",), col), threads=1).decode()
    assert f"<UName>{H}.&amp;[]</UName><Caption></Caption>" in out
    assert f"<UName>{H}.&amp;[~tilde]</UName><Caption>~tilde</Caption>" in out
    assert f"<UName>{H}.&amp;[a]]b]</UName><Caption>a]b</Caption>" in out


def test_pre_escaped_keys_and_captions() -> None:
    col = pa.array([1.0, 2.0, 3.0])
    out = cellset_xml(level_spec(["~empty", "~~tilde", "ok"], ["(empty)", "~tilde", None], False, (), col), threads=1).decode()
    assert f"<UName>{H}.&amp;[~empty]</UName><Caption>(empty)</Caption>" in out
    assert f"<UName>{H}.&amp;[~~tilde]</UName><Caption>~tilde</Caption>" in out
    assert f"<UName>{H}.&amp;[ok]</UName><Caption>(blank)</Caption>" in out


def test_captions_must_match_the_keys() -> None:
    col = pa.array([1.0, 2.0])
    with pytest.raises(Exception, match="captions"):
        cellset_xml(level_spec(["a", "b"], ["x"], False, (), col))
    with pytest.raises(Exception, match="text"):
        bad = level_spec(["a", "b"], None, False, (), col)
        bad["axes"][0]["level"]["captions"] = pa.array([1, 2])
        cellset_xml(bad)


def test_declared_cell_properties_are_declared_and_not_written() -> None:
    n = 5
    col = pa.array([1.5, None, 3.0, 4.0, 5.0])
    for cell_props in [
        ("VALUE", "FORMAT_STRING", "LANGUAGE", "BACK_COLOR", "FORE_COLOR", "FONT_FLAGS"),
        ("BACK_COLOR", "VALUE", "FORE_COLOR", "FORMAT_STRING", "FONT_FLAGS", "LANGUAGE"),
        ("FORMATTED_VALUE", "FONT_NAME", "FONT_SIZE", "UPDATEABLE", "ACTION_TYPE", "CELL_ORDINAL", "VALUE"),
        ("BACK_COLOR", "FORE_COLOR"),
        ("FORMAT_STRING", "BACK_COLOR", "LANGUAGE"),
    ]:
        for threads in (1, 3):
            spec = level_spec([f"k{i}" for i in range(n)], None, False, (), col)
            spec["cell_properties"] = list(cell_props)
            py = expected(CUBE, [[(value_member(H, "k0"),)]], python_cells([(col, "0")]), [measure_member("Sales")], [()], cell_props, {0: query.LightAxis(value_member(H, "x"), None, [f"k{i}" for i in range(n)])})
            assert cellset_xml(spec, threads=threads).decode() == py
    declared = cellset_xml({**spec, "cell_properties": ["VALUE", "BACK_COLOR", "FORMAT_STRING", "FONT_FLAGS"]}).decode()
    assert '<CellInfo><Value name="VALUE"/><BackColor name="BACK_COLOR" type="xsd:unsignedInt"/><FormatString name="FORMAT_STRING" type="xsd:string"/><FontFlags name="FONT_FLAGS" type="xsd:int"/></CellInfo>' in declared
    assert "<BackColor>" not in declared and "<FontFlags>" not in declared
    assert '<Cell CellOrdinal="0"><Value xsi:type="xsd:double">1.5</Value><FormatString>0</FormatString></Cell>' in declared
