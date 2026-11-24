"""Hardening beyond the data-type matrix: hostile Arrow structs, malformed text, number types, axis shapes, and the
strictness of the request parser."""

import ctypes
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pyarrow as pa
import pytest

import arrow2xmla

H = "[T].[F]"
MEMBER = dict(hierarchy=H, level_name=f"{H}.[L]", level_number=1)
M = "urn:schemas-microsoft-com:xml-analysis:mddataset"
XA = "urn:schemas-microsoft-com:xml-analysis"


def one_axis(keys: pa.Array, columns: list) -> dict:
    return {"cube": "C", "axes": [{"level": {"template": MEMBER, "keys": keys}}], "columns": columns}


# ---- number types and lexical forms ------------------------------------------------------------------------------


def values_of(out: bytes) -> list[tuple[str, str]]:
    """(xsd type, text) of each cell's value."""
    root = ET.fromstring(out)
    cells = [c.find(f"{{{M}}}Value") for c in root.iter(f"{{{M}}}Cell")]
    return [(v.get("{http://www.w3.org/2001/XMLSchema-instance}type"), v.text) for v in cells if v is not None]


def test_integers_get_the_xsd_type_that_holds_them() -> None:
    vals = [0, -(2**31), 2**31 - 1, 2**31, -(2**31) - 1, 2**63 - 1]
    got = values_of(arrow2xmla.cellset_xml(one_axis(pa.array([str(v) for v in vals]), [(pa.array(vals, pa.int64()), "0")])))
    assert [t for t, _ in got] == ["xsd:int", "xsd:int", "xsd:int", "xsd:long", "xsd:long", "xsd:long"]
    got = values_of(arrow2xmla.cellset_xml(one_axis(pa.array(["a", "b"]), [(pa.array([2**63, 2**64 - 1], pa.uint64()), "0")])))
    assert got == [("xsd:unsignedLong", "9223372036854775808"), ("xsd:unsignedLong", "18446744073709551615")]
    got = values_of(arrow2xmla.cellset_xml(one_axis(pa.array(["a", "b"]), [(pa.array([2**31, 5], pa.uint32()), "0")])))
    assert [t for t, _ in got] == ["xsd:long", "xsd:int"]


def test_nan_and_infinity_use_the_xsd_double_forms() -> None:
    vals = [float("nan"), float("inf"), float("-inf")]
    got = values_of(arrow2xmla.cellset_xml(one_axis(pa.array(["a", "b", "c"]), [(pa.array(vals), "0")])))
    assert [v for _, v in got] == ["NaN", "INF", "-INF"]


def test_half_floats_are_widened() -> None:
    vals = [1.0, 0.5, -2.0, 65504.0, 6.103515625e-05, 5.960464477539063e-08, float("inf"), None]
    got = values_of(arrow2xmla.cellset_xml(one_axis(pa.array([str(i) for i in range(8)]), [(pa.array(vals, pa.float16()), "0")])))
    assert [v for _, v in got] == ["1.0", "0.5", "-2.0", "65504.0", "6.103515625e-05", "5.960464477539063e-08", "INF"]


# ---- text that is not text -----------------------------------------------------------------------------------------


def string_array(data: bytes, offsets: list[int]) -> pa.Array:
    import struct

    return pa.Array.from_buffers(pa.string(), len(offsets) - 1, [None, pa.py_buffer(struct.pack(f"<{len(offsets)}i", *offsets)), pa.py_buffer(data)])


@pytest.mark.parametrize("data", [b"\xff\xfe", b"ab\xc3", b"\xed\xa0\x80", b"\xc0\x80", b"\xf4\x90\x80\x80"], ids=["bad", "truncated", "surrogate", "overlong", "too-big"])
def test_text_that_is_not_utf8_is_refused(data: bytes) -> None:
    arr = string_array(data, [0, len(data)])
    with pytest.raises(ValueError, match="UTF-8"):
        arrow2xmla.cellset_xml(one_axis(arr, [(pa.array([1.0]), "0")]))
    with pytest.raises(ValueError, match="UTF-8"):
        arrow2xmla.cellset_xml(one_axis(pa.array(["a"]), [(arr, "@")]))


def test_a_string_view_outside_its_buffer_is_refused() -> None:
    import struct

    views = struct.pack("<I4sII", 50, b"abcd", 0, 1000)  # 50 bytes at offset 1000 of a 3-byte buffer
    keys = pa.Array.from_buffers(pa.string_view(), 1, [None, pa.py_buffer(views), pa.py_buffer(b"abc")])
    with pytest.raises(ValueError, match="view"):
        arrow2xmla.cellset_xml(one_axis(keys, [(pa.array([1.0]), "0")]))


# ---- the axes must list the cells the columns hold -------------------------------------------------------------------


def test_orphan_cells_are_refused() -> None:
    with pytest.raises(ValueError, match="axes list"):
        arrow2xmla.cellset_xml(one_axis(pa.array(["a", "b"]), [(pa.array([1, 2, 3, 4, 5]), "0")]))
    with pytest.raises(ValueError, match="axes list"):  # two columns but only one axis of two members
        arrow2xmla.cellset_xml(one_axis(pa.array(["a", "b"]), [(pa.array([1, 2]), "0"), (pa.array([3, 4]), "0")]))
    spec = {"cube": "C", "axes": [{"tuples": [[MEMBER]]}, {"level": {"template": MEMBER, "keys": pa.array(["a", "b", "c"])}}], "columns": [(pa.array([1, 2]), "0")]}
    with pytest.raises(ValueError, match="axes list"):
        arrow2xmla.cellset_xml(spec)


def test_nothing_is_written_when_the_input_is_wrong() -> None:
    """An error comes before the first byte, so a caller that streams never sends half an answer."""
    n = 40_000
    bad = one_axis(pa.array([f"k{i}" for i in range(n)]), [(pa.array(range(n)), "0"), (pa.array(range(n - 1)), "0")])
    with pytest.raises(ValueError):
        arrow2xmla.cellset_xml_chunks(bad)


@pytest.mark.parametrize("axes", [{}, "axes", 3, None])
def test_axes_must_be_a_list(axes: object) -> None:
    with pytest.raises(TypeError):
        arrow2xmla.cellset_xml({"cube": "C", "axes": axes, "columns": []})


def test_spec_checks() -> None:
    base = one_axis(pa.array(["a"]), [(pa.array([1.0]), "0")])
    for bad_spec, match in [
        ({**base, "cell_properties": ["VALUE", "VALUE"]}, "twice"),
        ({**base, "language": True}, "bool"),
        ({**base, "columns": [(pa.array([1.0]), "0", 1)]}, "pair"),
        ({**base, "columns": {}}, "list"),
    ]:
        with pytest.raises((ValueError, TypeError), match=match):
            arrow2xmla.cellset_xml(bad_spec)
    s = one_axis(pa.array(["a"]), [(pa.array([1.0]), "0")])
    s["axes"][0]["level"]["template"] = {**MEMBER, "level_number": -1}
    with pytest.raises(ValueError, match="negative"):
        arrow2xmla.cellset_xml(s)
    s["axes"][0]["level"]["template"] = {**MEMBER, "bogus": 1}
    with pytest.raises(ValueError, match="bogus"):
        arrow2xmla.cellset_xml(s)
    s["axes"][0]["level"]["template"] = MEMBER
    s["axes"][0]["properties"] = ["MEMBER_CAPTION", "MEMBER_CAPTION"]
    with pytest.raises(ValueError, match="twice"):
        arrow2xmla.cellset_xml(s)


def test_a_non_array_says_what_it_got() -> None:
    with pytest.raises(ValueError, match="not a list"):
        arrow2xmla.cellset_xml(one_axis(["a"], [(pa.array([1.0]), "0")]))


def test_threads_is_bounded() -> None:
    n = 30_000
    s = one_axis(pa.array([f"k{i}" for i in range(n)]), [(pa.array(range(n)), "0")])
    assert arrow2xmla.cellset_xml(s, threads=1_000_000) == arrow2xmla.cellset_xml(s, threads=1)


# ---- hostile C data interface structs: an error, never a crash ---------------------------------------------------

FFI = textwrap.dedent(
    """
    import ctypes, sys
    import pyarrow as pa
    import arrow2xmla

    class Schema(ctypes.Structure):
        _fields_ = [("format", ctypes.c_void_p), ("name", ctypes.c_void_p), ("metadata", ctypes.c_void_p), ("flags", ctypes.c_int64),
                    ("n_children", ctypes.c_int64), ("children", ctypes.c_void_p), ("dictionary", ctypes.c_void_p),
                    ("release", ctypes.c_void_p), ("private_data", ctypes.c_void_p)]

    class Array(ctypes.Structure):
        _fields_ = [("length", ctypes.c_int64), ("null_count", ctypes.c_int64), ("offset", ctypes.c_int64), ("n_buffers", ctypes.c_int64),
                    ("n_children", ctypes.c_int64), ("buffers", ctypes.c_void_p), ("children", ctypes.c_void_p),
                    ("dictionary", ctypes.c_void_p), ("release", ctypes.c_void_p), ("private_data", ctypes.c_void_p)]

    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]

    ctypes.pythonapi.PyCapsule_New.restype = ctypes.py_object
    ctypes.pythonapi.PyCapsule_New.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]

    class Hostile:
        # An array whose structs are copies with something wrong in them (the real ones are left alone: their
        # producer reads them again when it frees the array).
        def __init__(self, arr, mutate):
            self.arr, self.mutate = arr, mutate

        def __arrow_c_array__(self, requested_schema=None):
            schema_c, array_c = self.arr.__arrow_c_array__()
            s0 = Schema.from_address(ctypes.pythonapi.PyCapsule_GetPointer(schema_c, b"arrow_schema"))
            a0 = Array.from_address(ctypes.pythonapi.PyCapsule_GetPointer(array_c, b"arrow_array"))
            s, a = Schema(), Array()
            ctypes.memmove(ctypes.byref(s), ctypes.byref(s0), ctypes.sizeof(Schema))
            ctypes.memmove(ctypes.byref(a), ctypes.byref(a0), ctypes.sizeof(Array))
            self.mutate(s, a)
            self.keep = (schema_c, array_c, s, a)  # the real capsules free the real arrays when this object goes
            return (
                ctypes.pythonapi.PyCapsule_New(ctypes.addressof(s), b"arrow_schema", None),
                ctypes.pythonapi.PyCapsule_New(ctypes.addressof(a), b"arrow_array", None),
            )

    MEMBER = dict(hierarchy="[T].[F]", level_name="[T].[F].[L]", level_number=1)
    def spec(col):
        return {"cube": "C", "axes": [{"level": {"template": MEMBER, "keys": pa.array(["a", "b"])}}], "columns": [(col, "0")]}

    cases = {
        "length-1": lambda s, a: setattr(a, "length", -1),
        "offset-1": lambda s, a: setattr(a, "offset", -1),
        "n_buffers-1": lambda s, a: setattr(a, "n_buffers", -1),
        "n_buffers0": lambda s, a: setattr(a, "n_buffers", 0),
        "n_buffers1": lambda s, a: setattr(a, "n_buffers", 1),
        "buffers_null": lambda s, a: setattr(a, "buffers", None),
        "released_array": lambda s, a: setattr(a, "release", None),
        "released_schema": lambda s, a: setattr(s, "release", None),
        "format_null": lambda s, a: setattr(s, "format", None),
        "null_count-5": lambda s, a: setattr(a, "null_count", -5),
        "dictionary": lambda s, a: setattr(a, "dictionary", 1),
        "children": lambda s, a: setattr(a, "n_children", 1),
        "length_max": lambda s, a: setattr(a, "length", 2**63 - 1),
    }
    kinds = {"float": pa.array([1.0, 2.0]), "text": pa.array(["x", "y"]), "int": pa.array([1, 2])}
    ok = True
    for kind, arr in kinds.items():
        for name, mutate in cases.items():
            try:
                arrow2xmla.cellset_xml(spec(Hostile(arr, mutate)))
                print("accepted", kind, name)
                ok = False
            except ValueError:
                pass
    print("done" if ok else "some accepted")
    """
)


def test_hostile_c_structs_are_refused_without_a_crash() -> None:
    r = subprocess.run([sys.executable, "-c", FFI], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"crashed with {r.returncode}: {r.stderr[-500:]}"
    assert r.stdout.strip() == "done", r.stdout


# ---- the request parser is as strict as a strict parser ---------------------------------------------------------------

ENV = '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">{head}<s:Body>{body}</s:Body></s:Envelope>'
EXEC = f'<Execute xmlns="{XA}"><Command><Statement>SELECT 1</Statement></Command></Execute>'


def req(body: str = EXEC, head: str = "") -> bytes:
    return ENV.format(head=head, body=body).encode()


@pytest.mark.parametrize(
    "bad",
    [
        req() + b"<junk/>",  # a second root
        req() + b"junk",  # text after the root
        b"junk" + req(),  # text before the root
        req() + req(),
        ENV.format(head="", body="<x:Execute><x:Command/></x:Execute>").encode(),  # an undeclared prefix
        ENV.format(head="", body=f'<Execute xmlns="{XA}"><Command><Statement>a\x01b</Statement></Command></Execute>').encode(),
        ENV.format(head="", body=f'<Execute xmlns="{XA}"><Command><Statement>a&#1;b</Statement></Command></Execute>').encode(),
        ENV.format(head="", body=f'<Execute xmlns="{XA}"><Command><Statement>a￾b</Statement></Command></Execute>').encode(),
        ENV.format(head=f'<Session xmlns="{XA}" SessionId="a<b"/>', body=EXEC).encode(),  # `<` in an attribute
        b"<a>" + b"<b/>" * 100_001 + b"</a>",  # too many elements
    ],
    ids=["second-root", "text-after", "text-before", "two-envelopes", "unbound-prefix", "ctrl-char", "ctrl-ref", "noncharacter", "lt-in-attr", "too-many-elements"],
)
def test_the_parser_refuses_what_a_strict_one_does(bad: bytes) -> None:
    with pytest.raises(ValueError):
        arrow2xmla.parse_request(bad)


def test_a_prefixed_attribute_is_not_the_plain_one() -> None:
    head = f'<x:Session xmlns:x="{XA}" x:SessionId="abc"/>'
    assert arrow2xmla.parse_request(req(head=head))["session"] == ("none", None)


def test_an_empty_session_id_is_no_session() -> None:
    head = f'<Session xmlns="{XA}" SessionId=""/>'
    assert arrow2xmla.parse_request(req(head=head))["session"] == ("none", None)


def test_the_first_discover_or_execute_of_any_body_is_the_call() -> None:
    two = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/><s:Body>{EXEC}</s:Body></s:Envelope>'.encode()
    assert arrow2xmla.parse_request(two)["call"]["kind"] == "execute"


def test_whitespace_around_the_root_is_fine() -> None:
    assert arrow2xmla.parse_request(b"\n  " + req() + b"\n\n")["call"]["kind"] == "execute"
