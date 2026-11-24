"""Hardening: every kind of data the writer is handed. Supported types are held to the pure-Python reference; the rest
must be refused with a clear error (never a crash); every answer must be well-formed XML."""

import itertools
import math
import random
import struct
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow as pa
import pytest
import reference as ref

import arrow2xmla

H = "[T].[F]"
CUBE = SimpleNamespace(name="C")
M = "urn:schemas-microsoft-com:xml-analysis:mddataset"


def md(m: ref.Member) -> dict:
    return dict(hierarchy=m.hierarchy, unique_name=m.unique, caption=m.caption, level_name=m.level, level_number=m.number, parent_unique_name=m.parent, children_cardinality=m.children, member_type=m.kind)


SAMPLE = ref.Member(H, f"{H}.&[x]", "x", f"{H}.[L]", 1, parent=f"{H}.[All]")


def measure(i: int) -> ref.Member:
    return ref.Member("[Measures]", f"[Measures].[M{i}]", f"M{i}", "[Measures].[MeasuresLevel]", 0, kind=3)


def spec(keys: pa.Array, columns: list[tuple[pa.Array, str]], **kw: object) -> dict:
    """One column: the level on the only axis. Several: the measures on columns, the level on rows."""
    level = {"level": {"template": md(SAMPLE), "keys": keys}}
    axes = [level] if len(columns) <= 1 else [{"tuples": [[md(measure(i))] for i in range(len(columns))]}, level]
    return {"cube": "C", "axes": axes, "columns": columns, **kw}


def reference(keys: pa.Array, columns: list[tuple[pa.Array, str]]) -> str:
    n = len(columns)
    cells = {r * n + k: (v, f) for k, (c, f) in enumerate(columns) for r, v in enumerate(c.to_pylist()) if v is not None}
    light = ref.LightAxis(SAMPLE, None, keys.to_pylist())
    first = ref.Member(H, ref.member_uname(H, "k"), "k", f"{H}.[L]", 1, parent=f"{H}.[All]")
    rows = [(first,)] if len(keys) else []
    if n <= 1:
        cs = ref.Cellset(CUBE, [rows], cells, (), [()], ("VALUE", "FORMAT_STRING"), {0: light})
    else:
        cs = ref.Cellset(CUBE, [[(measure(i),) for i in range(n)], rows], cells, (), [(), ()], ("VALUE", "FORMAT_STRING"), {1: light})
    return ref.cellset_xml(cs)


def check(keys: pa.Array, columns: list[tuple[pa.Array, str]]) -> str:
    """Same bytes as the reference, and well-formed XML. Returns the answer."""
    got = arrow2xmla.cellset_xml(spec(keys, columns)).decode()
    assert got == reference(keys, columns)
    ET.fromstring(got)
    return got


# ---- every numeric type, extremes and nulls, plain and sliced --------------------------------------------------

INT_RANGE = {
    pa.int8(): (-(2**7), 2**7 - 1), pa.int16(): (-(2**15), 2**15 - 1), pa.int32(): (-(2**31), 2**31 - 1), pa.int64(): (-(2**63), 2**63 - 1),
    pa.uint8(): (0, 2**8 - 1), pa.uint16(): (0, 2**16 - 1), pa.uint32(): (0, 2**32 - 1), pa.uint64(): (0, 2**64 - 1),
}  # fmt: skip


@pytest.mark.parametrize("typ", INT_RANGE, ids=str)
@pytest.mark.parametrize("sliced", [False, True])
def test_integers_extremes_and_nulls(typ: pa.DataType, sliced: bool) -> None:
    lo, hi = INT_RANGE[typ]
    vals = [lo, hi, 0, 1, hi - 1, lo + 1 if lo < hi else lo, None, 7, None, hi]
    col = pa.array(vals, typ)
    keys = pa.array([f"k{i}" for i in range(len(vals))])
    if sliced:
        col, keys = col.slice(3, 6), keys.slice(3, 6)
    out = check(keys, [(col, "0")])
    assert out.count("<Cell ") == sum(v is not None for v in col.to_pylist())


FLOATS = [0.0, -0.0, 1.0, -1.5, 0.1, 1 / 3, 1e15, 1e16, 1.5e16, 9999999999999998.0, 1e-4, 9.999e-5, 1e-5, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308, math.nan, math.inf, -math.inf, 123456789.123456789, 4.35, 0.30000000000000004]


@pytest.mark.parametrize("typ", [pa.float64(), pa.float32()], ids=str)
@pytest.mark.parametrize("sliced", [False, True])
def test_floats_special_values(typ: pa.DataType, sliced: bool) -> None:
    vals = [*FLOATS, None]
    if typ == pa.float32():
        vals = [v if v is None or not math.isfinite(v) else struct.unpack("f", struct.pack("f", max(min(v, 3.0e38), -3.0e38)))[0] for v in vals]
    col = pa.array(vals, typ)
    keys = pa.array([f"k{i}" for i in range(len(vals))])
    if sliced:
        col, keys = col.slice(2, 15), keys.slice(2, 15)
    check(keys, [(col, "0.00")])


def test_random_double_bit_patterns_print_as_python_does() -> None:
    rnd = random.Random(11)
    vals = [struct.unpack("d", struct.pack("Q", rnd.getrandbits(64)))[0] for _ in range(60_000)]
    col = pa.array(vals)
    keys = pa.array([str(i) for i in range(len(vals))])
    check(keys, [(col, "0")])


def test_random_doubles_across_magnitudes() -> None:
    rnd = random.Random(12)
    vals = [rnd.choice([1, -1]) * rnd.random() * 10 ** rnd.randint(-320, 305) for _ in range(60_000)]
    check(pa.array([str(i) for i in range(len(vals))]), [(pa.array(vals), "0")])


# ---- text: every string type, as keys and as cell values -------------------------------------------------------

TEXTS = ["", "plain", "a&b", "<tag>", "x>y", "it's", 'say "hi"', "br]acket", "]]", "&amp;", "é ü ñ", "日本語", "😀 emoji", "é combining", "عربى", "tab\there", "line\nbreak", "cr\rhere", " lead", "trail ", "x" * 1000, "&" * 500, "]" * 300, "0123456789abcdef" * 3]
STRING_TYPES = [pa.string(), pa.large_string(), pa.string_view()]


@pytest.mark.parametrize("typ", STRING_TYPES, ids=str)
@pytest.mark.parametrize("sliced", [False, True])
def test_strings_as_keys_and_as_values(typ: pa.DataType, sliced: bool) -> None:
    vals = [*TEXTS, None, "last"]
    keys = pa.array(vals, typ)
    values = pa.array(vals[::-1], typ)
    if sliced:
        keys, values = keys.slice(4, 15), values.slice(4, 15)
    check(keys, [(values, "@"), (pa.array(range(len(keys))), "0")])


@pytest.mark.parametrize("typ", STRING_TYPES, ids=str)
def test_all_null_and_empty_columns(typ: pa.DataType) -> None:
    check(pa.array([None, None, None], typ), [(pa.array([None, None, None], pa.float64()), "0"), (pa.array([None, None, None], typ), "@")])
    check(pa.array([], typ), [(pa.array([], pa.float64()), "0")])


def test_a_long_string_view_is_read_from_its_buffer() -> None:
    vals = ["short", "exactly12byte", "a string that is much longer than twelve bytes " * 5, None, "x"]
    check(pa.array(vals, pa.string_view()), [(pa.array(vals, pa.string_view()), "@")])


def test_random_arrays_of_random_types() -> None:
    rnd = random.Random(5)
    makers = [
        lambda n: pa.array([rnd.choice([None, rnd.randint(-(2**40), 2**40)]) for _ in range(n)], pa.int64()),
        lambda n: pa.array([rnd.choice([None, rnd.randint(0, 255)]) for _ in range(n)], pa.uint8()),
        lambda n: pa.array([rnd.choice([None, rnd.random() * 10 ** rnd.randint(-8, 9)]) for _ in range(n)], pa.float64()),
        lambda n: pa.array([rnd.choice([None, rnd.choice(TEXTS)]) for _ in range(n)], rnd.choice(STRING_TYPES)),
    ]
    for _ in range(150):
        n = rnd.choice([0, 1, 2, 9, 40, 300])
        keys = pa.array([rnd.choice([None, *TEXTS]) for _ in range(n)], rnd.choice(STRING_TYPES))
        cols = [(rnd.choice(makers)(n), rnd.choice(["0", "#,##0.00", "a&b"])) for _ in range(rnd.randint(1, 4))]
        if n > 4 and rnd.random() < 0.5:
            a, b = rnd.randint(0, n // 2), rnd.randint(1, n // 2)
            keys, cols = keys.slice(a, b), [(c.slice(a, b), f) for c, f in cols]
        check(keys, cols)


# ---- what is refused, and how ----------------------------------------------------------------------------------

UNSUPPORTED = {
    "bool": pa.array([True, False, None]),
    "date32": pa.array([1, 2, None], pa.date32()),
    "date64": pa.array([1, 2, None], pa.date64()),
    "timestamp": pa.array([1, 2, None], pa.timestamp("us")),
    "time64": pa.array([1, 2, None], pa.time64("us")),
    "duration": pa.array([1, 2, None], pa.duration("s")),
    "decimal128": pa.array([1, 2, None], pa.decimal128(10, 2)),
    "binary": pa.array([b"a", b"b", None]),
    "large_binary": pa.array([b"a", b"b", None], pa.large_binary()),
    "list": pa.array([[1], [2], None]),
    "struct": pa.array([{"a": 1}, {"a": 2}, None]),
    "null": pa.nulls(3),
    "dictionary": pa.array(["a", "b", "a"]).dictionary_encode(),
    "fixed_size_binary": pa.array([b"ab", b"cd", None], pa.binary(2)),
}


@pytest.mark.parametrize("name", UNSUPPORTED)
def test_unsupported_types_are_refused_as_columns(name: str) -> None:
    with pytest.raises(ValueError):
        arrow2xmla.cellset_xml(spec(pa.array(["a", "b", "c"]), [(UNSUPPORTED[name], "0")]))


@pytest.mark.parametrize("name", UNSUPPORTED)
def test_unsupported_types_are_refused_as_keys(name: str) -> None:
    with pytest.raises(ValueError):
        arrow2xmla.cellset_xml(spec(UNSUPPORTED[name], [(pa.array([1.0, 2.0, 3.0]), "0")]))


def test_numeric_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="text"):
        arrow2xmla.cellset_xml(spec(pa.array([1, 2, 3]), [(pa.array([1.0, 2.0, 3.0]), "0")]))


def test_not_an_arrow_object_is_refused() -> None:
    with pytest.raises(ValueError, match="Arrow"):
        arrow2xmla.cellset_xml(spec(["a", "b"], [([1.0, 2.0], "0")]))


def test_columns_of_different_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="length"):
        arrow2xmla.cellset_xml(spec(pa.array(["a", "b"]), [(pa.array([1.0, 2.0]), "0"), (pa.array([1.0]), "0")]))


def test_unknown_properties_are_refused() -> None:
    with pytest.raises(ValueError, match="BOGUS"):
        arrow2xmla.cellset_xml(spec(pa.array(["a"]), [(pa.array([1.0]), "0")], cell_properties=["BOGUS"]))
    s = spec(pa.array(["a"]), [(pa.array([1.0]), "0")])
    s["axes"][0]["properties"] = ["BOGUS"]
    with pytest.raises(ValueError, match="BOGUS"):
        arrow2xmla.cellset_xml(s)


# ---- chunked arrays, no columns, many threads -------------------------------------------------------------------


def test_chunked_arrays_are_combined() -> None:
    chunks = [[f"k{i}" for i in range(a, a + 5)] for a in (0, 5, 10)]
    keys = pa.chunked_array(chunks)
    col = pa.chunked_array([[float(i) for i in range(a, a + 5)] for a in (0, 5, 10)])
    flat_keys, flat_col = pa.array(sum(chunks, [])), pa.array([float(i) for i in range(15)])
    assert arrow2xmla.cellset_xml(spec(keys, [(col, "0")])) == arrow2xmla.cellset_xml(spec(flat_keys, [(flat_col, "0")]))


def test_a_cellset_with_no_columns_has_no_cells() -> None:
    out = arrow2xmla.cellset_xml(spec(pa.array(["a", "b"]), [])).decode()
    assert "<Cell " not in out
    ET.fromstring(out)


def test_cell_properties_without_a_value() -> None:
    s = spec(pa.array(["a", "b", "c"]), [(pa.array([1.0, None, 3.0]), "0"), (pa.array(["x", None, "z"]), "@")], cell_properties=["FORMAT_STRING", "LANGUAGE"])
    out = arrow2xmla.cellset_xml(s).decode()
    assert out.count("<Cell ") == 4 and "<Value" not in out
    ET.fromstring(out)


def test_calls_from_many_threads_agree() -> None:
    n = 40_000
    s = spec(pa.array([f"k{i} & <{i}> ]" for i in range(n)]), [(pa.array([i * 0.37 for i in range(n)]), "0"), (pa.array(range(n)), "0")])
    want = arrow2xmla.cellset_xml(s, threads=1)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda t: arrow2xmla.cellset_xml(s, threads=t), itertools.islice(itertools.cycle([0, 1, 2, 4]), 16)))
    assert all(r == want for r in results)


# ---- XML well-formedness: characters XML 1.0 forbids --------------------------------------------------------------

ILLEGAL = ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "￾", "￿"]


@pytest.mark.parametrize("bad", ILLEGAL, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("typ", STRING_TYPES, ids=str)
def test_characters_xml_forbids_never_reach_the_answer(bad: str, typ: pa.DataType) -> None:
    vals = [f"a{bad}b", bad, f"{bad}{bad}", "ok", f"x]{bad}&"]
    out = arrow2xmla.cellset_xml(
        {
            "cube": f"cube{bad}",
            "axes": [{"properties": ["MEMBER_CAPTION"], "level": {"template": {**md(SAMPLE), "hierarchy": f"[T{bad}]"}, "keys": pa.array(vals, typ)}}],
            "columns": [(pa.array(vals, typ), f"fmt{bad}")],
        }
    )
    root = ET.fromstring(out)  # a parser that rejects the control character would raise here
    assert len(list(root.iter(f"{{{M}}}Cell"))) == len(vals)
    assert bad.encode() not in out


def test_legal_whitespace_controls_survive_a_parse() -> None:
    out = arrow2xmla.cellset_xml(spec(pa.array(["a\tb\nc\rd"]), [(pa.array(["a\tb\nc\rd"]), "@")]))
    root = ET.fromstring(out)
    assert root.find(f".//{{{M}}}Cell/{{{M}}}Value").text == "a\tb\nc\rd"  # the carriage return is kept
    assert root.find(f".//{{{M}}}Member/{{{M}}}Caption").text == "a\tb\nc\rd"


@pytest.mark.parametrize("name", ['[T"q"].[F]', "[T'q'].[F]", "[T\"'q].[F]", "[T\n\t].[F]", "[T&<>].[F]"])
def test_hierarchy_names_with_quotes_and_markup_keep_the_header_well_formed(name: str) -> None:
    s = spec(pa.array(["a"]), [(pa.array([1.0]), "0")])
    s["axes"][0]["level"]["template"]["hierarchy"] = name
    s["axes"][0]["properties"] = ["MEMBER_CAPTION"]
    s["slicer"] = [{**md(SAMPLE), "hierarchy": name}]
    out = arrow2xmla.cellset_xml(s)
    root = ET.fromstring(out)
    infos = [h.get("name") for h in root.iter(f"{{{M}}}HierarchyInfo")]
    assert infos and all(i == name.replace("\n", " ").replace("\t", " ") or i == name for i in infos)
