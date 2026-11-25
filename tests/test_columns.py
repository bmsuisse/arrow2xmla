"""`columns(table)`: the columns of an Arrow table or stream, read without an Arrow library; and hostile streams."""

import subprocess
import sys
import textwrap

import pyarrow as pa
import pytest

import arrow2xmla as ax

M = ax.Member("[H]", "[H].[L]", 1)
MS = [ax.Member("[Measures]", "[Measures].[ML]", 0, f"[Measures].[{c}]", c, member_type=ax.MemberType.MEASURE) for c in "AB"]


def cellset(keys, a, b) -> ax.Cellset:
    return ax.Cellset("C", [ax.CellValues(a, "0.0"), ax.CellValues(b, "0")], ax.TupleAxis.from_tuples([[m] for m in MS]), ax.LevelAxis(M, keys))


def same_bytes(table: pa.Table) -> None:
    """A cellset from the columns read natively is the same bytes as from the pyarrow arrays."""
    cols = ax.columns(table)
    native = cellset(cols["k"], cols["a"], cols["b"]).to_xml()
    arrays = cellset(table["k"].combine_chunks(), table["a"].combine_chunks(), table["b"].combine_chunks()).to_xml()
    assert native == arrays


def table(n: int, **kw) -> pa.Table:
    return pa.table(
        {
            "k": pa.array([f"key {i} & <x>" if i % 7 else None for i in range(n)], kw.get("text", pa.string())),
            "a": pa.array([i / 3 if i % 5 else None for i in range(n)], pa.float64()),
            "b": pa.array(range(n), pa.int64()),
        }
    )


@pytest.mark.parametrize("n", [0, 1, 9, 1000, 70_000])
def test_a_table_gives_the_same_bytes(n: int) -> None:
    same_bytes(table(n))


@pytest.mark.parametrize("text", [pa.string(), pa.large_string(), pa.string_view()])
def test_every_text_type(text: pa.DataType) -> None:
    same_bytes(table(300, text=text))


def test_many_batches_are_combined() -> None:
    parts = [table(n) for n in (1000, 1, 0, 4096, 17)]
    big = pa.concat_tables(parts)
    assert big["k"].num_chunks == 5 or big["k"].num_chunks >= 4
    same_bytes(big)


def test_a_sliced_batch_reads_its_slice_and_not_what_is_before_it() -> None:
    t = table(100)
    sliced = t.slice(30, 40)  # the struct and its children both carry an offset
    cols = ax.columns(sliced)
    assert len(cols["b"]) == 40
    expect = cellset(sliced["k"].combine_chunks(), sliced["a"].combine_chunks(), sliced["b"].combine_chunks()).to_xml()
    assert cellset(cols["k"], cols["a"], cols["b"]).to_xml() == expect


@pytest.mark.parametrize(
    ("typ", "values"),
    [
        (pa.float64(), [1.5, None, -2.25]), (pa.float32(), [1.5, None, -2.25]), (pa.float16(), [1.5, None, -2.25]),
        (pa.int8(), [1, None, -128]), (pa.int16(), [1, None, -1]), (pa.int32(), [1, None, -(2**31)]), (pa.int64(), [1, None, -(2**63)]),
        (pa.uint8(), [1, None, 255]), (pa.uint16(), [1, None, 2**16 - 1]), (pa.uint32(), [1, None, 2**32 - 1]), (pa.uint64(), [1, None, 2**64 - 1]),
    ],
)  # fmt: skip
def test_every_number_type_keeps_its_values_and_nulls(typ: pa.DataType, values: list) -> None:
    t = pa.table({"k": pa.array(["x", "y", "z"]), "v": pa.array(values, typ)})
    col = ax.columns(t)["v"]
    assert len(col) == 3 and col.type == str(typ)
    one = ax.Cellset("C", [ax.CellValues(col, "0")], ax.LevelAxis(M, ax.columns(t)["k"]))
    ref = ax.Cellset("C", [ax.CellValues(t["v"].combine_chunks(), "0")], ax.LevelAxis(M, t["k"].combine_chunks()))
    assert one.to_xml() == ref.to_xml()


def test_a_single_column_stream_is_a_column_without_columns() -> None:
    chunked = pa.chunked_array([pa.array([1.0, 2.0]), pa.array([3.0])])
    cs = ax.Cellset("C", [ax.CellValues(chunked, "0")], ax.LevelAxis(M, pa.array(["a", "b", "c"])))
    assert b'CellOrdinal="2"' in cs.to_xml()


def test_a_table_is_not_a_column_and_a_column_is_not_a_table() -> None:
    with pytest.raises(ax.CellsetError, match="table, not one column"):
        ax.CellValues(table(3), "0")
    with pytest.raises(ax.CellsetError, match="one column, not a table"):
        ax.columns(pa.chunked_array([pa.array([1.0])]))
    with pytest.raises(TypeError):
        ax.columns([1, 2, 3])  # type: ignore[arg-type]
    with pytest.raises(ax.CellsetError, match="two columns of one name"):
        ax.columns(pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["x", "x"]))


@pytest.mark.parametrize(
    "array",
    [pa.array([True]), pa.array([1], pa.timestamp("s")), pa.array([1], pa.date32()), pa.array([1]).dictionary_encode(), pa.array([[1]]), pa.array([1], pa.decimal128(10, 2))],
    ids=["bool", "timestamp", "date", "dictionary", "list", "decimal"],
)  # fmt: skip
def test_what_is_not_written_names_its_column(array: pa.Array) -> None:
    with pytest.raises(ax.UnsupportedTypeError, match='column "weird"'):
        ax.columns(pa.table({"weird": array}))


def test_a_column_is_reusable_and_read_only() -> None:
    col = ax.columns(table(50))["b"]
    cs = ax.Cellset("C", [ax.CellValues(col, "0")], ax.LevelAxis(M, ax.columns(table(50))["k"]))
    assert cs.to_xml() == cs.to_xml()
    assert "int64 x 50" in repr(col)
    with pytest.raises(AttributeError):
        col.type = "x"  # type: ignore[misc]


def test_rowset_takes_columns() -> None:
    t = pa.table({"CUBE_NAME": pa.array(["Sales", "Costs"]), "N": pa.array([1, 2])})
    xml = ax.Rowset.from_columns(ax.columns(t)).to_xml()
    assert b"<CUBE_NAME>Sales</CUBE_NAME><N>1</N>" in xml


duckdb = pytest.importorskip("duckdb", reason="the DuckDB checks need duckdb")


def test_duckdb_relation() -> None:
    con = duckdb.connect()
    rel = con.sql("select 'c' || i::varchar as k, (i * 1.5)::double as a, i::bigint as b from range(2500) t(i)")
    cols = ax.columns(rel)
    assert list(cols) == ["k", "a", "b"] and len(cols["a"]) == 2500
    assert cols["k"].type == "large_string" and cols["a"].type == "double" and cols["b"].type == "int64"
    xml = cellset(cols["k"], cols["a"], cols["b"]).to_xml()
    ref = cellset(pa.array([f"c{i}" for i in range(2500)]), pa.array([i * 1.5 for i in range(2500)]), pa.array(range(2500))).to_xml()
    assert xml == ref


def test_duckdb_nulls_and_a_decimal_that_has_to_be_cast() -> None:
    con = duckdb.connect()
    cols = ax.columns(con.sql("select * from (values ('a', 1.5::double), (NULL, NULL)) v(k, x)"))
    assert "1 null" in repr(cols["k"]) and "1 null" in repr(cols["x"])
    with pytest.raises(ax.UnsupportedTypeError, match="DECIMAL|::DOUBLE"):
        ax.columns(con.sql("select 1.5 as x"))


def test_duckdb_more_than_one_batch() -> None:
    con = duckdb.connect()
    cols = ax.columns(con.sql("select 'k' || i::varchar as k, i::bigint as n from range(2500000) t(i)"))
    assert len(cols["k"]) == len(cols["n"]) == 2_500_000


HOSTILE = textwrap.dedent(
    """
    import ctypes, sys
    import pyarrow as pa
    import arrow2xmla as ax

    class Schema(ctypes.Structure):
        _fields_ = [("format", ctypes.c_void_p), ("name", ctypes.c_void_p), ("metadata", ctypes.c_void_p), ("flags", ctypes.c_int64),
                    ("n_children", ctypes.c_int64), ("children", ctypes.c_void_p), ("dictionary", ctypes.c_void_p),
                    ("release", ctypes.c_void_p), ("private_data", ctypes.c_void_p)]
    class Array(ctypes.Structure):
        _fields_ = [("length", ctypes.c_int64), ("null_count", ctypes.c_int64), ("offset", ctypes.c_int64), ("n_buffers", ctypes.c_int64),
                    ("n_children", ctypes.c_int64), ("buffers", ctypes.c_void_p), ("children", ctypes.c_void_p), ("dictionary", ctypes.c_void_p),
                    ("release", ctypes.c_void_p), ("private_data", ctypes.c_void_p)]
    class Stream(ctypes.Structure):
        pass
    GET_SCHEMA = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(Stream), ctypes.POINTER(Schema))
    GET_NEXT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(Stream), ctypes.POINTER(Array))
    LAST_ERROR = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.POINTER(Stream))
    RELEASE = ctypes.CFUNCTYPE(None, ctypes.POINTER(Stream))
    Stream._fields_ = [("get_schema", GET_SCHEMA), ("get_next", GET_NEXT), ("get_last_error", LAST_ERROR), ("release", RELEASE), ("private_data", ctypes.c_void_p)]

    new = ctypes.pythonapi.PyCapsule_New
    new.restype = ctypes.py_object
    new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    get_ptr = ctypes.pythonapi.PyCapsule_GetPointer
    get_ptr.restype = ctypes.c_void_p
    get_ptr.argtypes = [ctypes.py_object, ctypes.c_char_p]
    NOOP = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(lambda p: None)  # the copies are not the producer's to release

    batch = pa.record_batch({"k": ["a", "b", "c"], "v": [1.0, 2.0, 3.0]})
    keep = batch.__arrow_c_array__()  # the real structs, alive as long as this

    def copy(cls, capsule, name):
        out = cls()
        ctypes.memmove(ctypes.byref(out), get_ptr(capsule, name), ctypes.sizeof(cls))
        out.release = ctypes.cast(NOOP, ctypes.c_void_p).value
        return out

    class Fake:
        def __init__(self, mode):
            self.mode, self.calls, self.schema = mode, 0, copy(Schema, keep[0], b"arrow_schema")
            self.live = []
            def get_schema(s, out):
                if self.mode == "schema_error":
                    return 5
                ctypes.memmove(out, ctypes.byref(self.schema), ctypes.sizeof(Schema))
                if self.mode == "schema_no_format":
                    out.contents.format = None
                return 0
            def get_next(s, out):
                self.calls += 1
                if self.mode == "next_error" and self.calls == 2:
                    return 7
                if self.calls > (0 if self.mode == "empty" else 2):
                    ctypes.memset(out, 0, ctypes.sizeof(Array))  # release == NULL: the end
                    return 0
                a = copy(Array, keep[1], b"arrow_array")
                if self.mode == "children_mismatch": a.n_children = 1
                if self.mode == "negative_length": a.length = -3
                if self.mode == "null_struct_rows": a.null_count = 1
                if self.mode == "huge_offset": a.offset = 2**62
                if self.mode == "null_children": a.children = None
                self.live.append(a)
                ctypes.memmove(out, ctypes.byref(a), ctypes.sizeof(Array))
                return 0
            self.fns = (GET_SCHEMA(get_schema), GET_NEXT(get_next), LAST_ERROR(lambda s: b"the producer says no"), RELEASE(lambda s: None))
            self.stream = Stream(*self.fns, None)
        def __arrow_c_stream__(self, requested_schema=None):
            return new(ctypes.addressof(self.stream), b"arrow_array_stream", None)

    for mode in sys.argv[1:]:
        try:
            cols = ax.columns(Fake(mode))
            print(mode, "ok", {k: len(v) for k, v in cols.items()})
        except ax.XmlaError as e:
            print(mode, "error", str(e)[:70])
    print("done")
    """
)


def test_hostile_streams_are_errors_not_crashes() -> None:
    modes = ["ok", "empty", "schema_error", "schema_no_format", "next_error", "children_mismatch", "negative_length", "null_struct_rows", "huge_offset", "null_children"]
    out = subprocess.run([sys.executable, "-c", HOSTILE, *modes], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"crashed with {out.returncode}: {out.stderr[-600:]}"
    seen = {line.split()[0]: line for line in out.stdout.splitlines() if line.split()[:1] and line.split()[0] in modes}
    assert seen["ok"].startswith("ok ok"), seen["ok"]  # the fake stream is well-formed: two batches of three rows
    assert "'k': 6" in seen["ok"] and "'v': 6" in seen["ok"]
    assert seen["empty"].startswith("empty ok") and "'k': 0" in seen["empty"]
    for mode in modes[2:]:
        assert seen[mode].startswith(f"{mode} error"), seen[mode]
    assert "the producer says no" in seen["schema_error"] and "the producer says no" in seen["next_error"]
    assert out.stdout.strip().endswith("done")


def test_the_duckdb_example_runs_and_says_what_it_should() -> None:
    import pathlib

    example = pathlib.Path(__file__).parents[1] / "examples" / "quickstart.py"
    out = subprocess.run([sys.executable, str(example)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    assert "bytes of XMLA" in out.stdout and "MDX: SELECT FROM [Sales]" in out.stdout
