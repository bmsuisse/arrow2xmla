"""The cellset model: frozen dataclasses and enums, checked when they are made."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any

from arrow2xmla._native import CellsetError


class MemberType(IntEnum):
    """MDSCHEMA `MEMBER_TYPE`."""

    REGULAR = 1  # a value of a field
    ALL = 2  # the All member
    MEASURE = 3
    FORMULA = 4  # a calculated member


class MemberProperty(str, Enum):
    """A member property a client may ask for (`DIMENSION PROPERTIES`), written after each member of its axis."""

    PARENT_UNIQUE_NAME = "PARENT_UNIQUE_NAME"
    HIERARCHY_UNIQUE_NAME = "HIERARCHY_UNIQUE_NAME"
    MEMBER_UNIQUE_NAME = "MEMBER_UNIQUE_NAME"
    MEMBER_CAPTION = "MEMBER_CAPTION"
    LEVEL_UNIQUE_NAME = "LEVEL_UNIQUE_NAME"
    LEVEL_NUMBER = "LEVEL_NUMBER"
    CHILDREN_CARDINALITY = "CHILDREN_CARDINALITY"
    PARENT_LEVEL = "PARENT_LEVEL"
    PARENT_COUNT = "PARENT_COUNT"
    MEMBER_TYPE = "MEMBER_TYPE"


class CellProperty(str, Enum):
    """What is written in each cell: only the properties this package produces."""

    VALUE = "VALUE"
    FORMAT_STRING = "FORMAT_STRING"
    LANGUAGE = "LANGUAGE"


def _plain(x: Any) -> Any:
    """One Arrow array: as it is, or a chunked array combined."""
    if hasattr(x, "__arrow_c_array__"):
        return x
    if hasattr(x, "combine_chunks"):
        return x.combine_chunks()
    if hasattr(x, "__arrow_c_stream__"):
        try:
            import pyarrow as pa

            return pa.chunked_array(x).combine_chunks()
        except ImportError:
            raise CellsetError("an Arrow stream needs pyarrow to be combined into an array: pass an array (.to_arrow())") from None
    raise TypeError(f"expected an Arrow array (something with __arrow_c_array__), not {type(x).__name__}")


def _enums(kind: type[Enum], items: Any, what: str, one: str) -> tuple[Any, ...]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise TypeError(f"{what} is a sequence of {kind.__name__}, not {type(items).__name__}")
    out = []
    for i in items:
        try:
            out.append(kind(i))
        except ValueError:
            raise CellsetError(f"unknown {one} {i!r} (one of {', '.join(m.value for m in kind)})") from None
    return tuple(out)


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


@dataclass(frozen=True, slots=True)
class Member:
    """One member of a hierarchy: a value of a field, its All member, a measure."""

    hierarchy: str
    level_name: str
    level_number: int
    unique_name: str = ""  # a level axis makes each value's own
    caption: str = ""  # likewise
    parent_unique_name: str = ""  # "" for All and for a measure
    children_cardinality: int = 0
    member_type: MemberType = MemberType.REGULAR

    def __post_init__(self) -> None:
        for name in ("hierarchy", "level_name", "unique_name", "caption", "parent_unique_name"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"Member.{name} is a str, not {type(getattr(self, name)).__name__}")
        for name in ("level_number", "children_cardinality"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int):
                raise TypeError(f"Member.{name} is an int, not {type(v).__name__}")
        if not -(2**31) <= self.level_number < 2**31:
            raise CellsetError("level_number is a signed 32-bit number")
        if not 0 <= self.children_cardinality <= 0xFFFFFFFF:
            raise CellsetError("children_cardinality is an unsigned 32-bit number")
        try:
            _set(self, "member_type", MemberType(self.member_type))
        except ValueError:
            raise CellsetError(f"member_type {self.member_type!r} is not a MemberType (1 value, 2 All, 3 measure, 4 calculated)") from None



@dataclass(frozen=True, slots=True, eq=False)
class LevelAxis:
    """The values of one hierarchy's level, read from a text array: nothing is built per value (the fast form for
    big axes). A value's unique name is `<hierarchy>.&[<key>]` (`]` doubled), its caption the key; a null key is the
    blank member."""

    template: Member  # hierarchy, level_name, level_number, parent_unique_name, children_cardinality, member_type
    keys: Any  # an Arrow text array (string, large_string or string_view)
    all: Member | None = None  # the All member, shown first
    blank_caption: str = "(blank)"
    properties: tuple[MemberProperty, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.template, Member):
            raise TypeError("LevelAxis.template is a Member")
        if self.all is not None and not isinstance(self.all, Member):
            raise TypeError("LevelAxis.all is a Member or None")
        if not isinstance(self.blank_caption, str):
            raise TypeError("LevelAxis.blank_caption is a str")
        _set(self, "keys", _plain(self.keys))
        _set(self, "properties", _enums(MemberProperty, self.properties, "member properties", "member property"))



@dataclass(frozen=True, slots=True, eq=False)
class TupleAxis:
    """Explicit tuples: the distinct `members` once, each tuple as indices into them (all tuples of one width).
    `DISPLAY_INFO` (children, drilled down, same parent) is worked out from them. Build one from tuples of members
    with `TupleAxis.from_tuples`."""

    members: Sequence[Member]
    tuples: Sequence[Sequence[int]]
    properties: tuple[MemberProperty, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "members", tuple(self.members))
        if not all(isinstance(m, Member) for m in self.members):
            raise TypeError("TupleAxis.members are Members")
        tuples = tuple(tuple(t) for t in self.tuples)
        if not all(isinstance(k, int) and not isinstance(k, bool) for t in tuples for k in t):
            raise TypeError("TupleAxis.tuples are sequences of member indices (ints)")
        if not all(0 <= k < len(self.members) for t in tuples for k in t):
            raise CellsetError("a tuple names a member that is not in members")
        _set(self, "tuples", tuples)
        _set(self, "properties", _enums(MemberProperty, self.properties, "member properties", "member property"))

    @classmethod
    def from_tuples(cls, tuples: Sequence[Sequence[Member]], properties: Sequence[MemberProperty | str] = ()) -> TupleAxis:
        """From tuples of members: equal members are stored once."""
        index: dict[Member, int] = {}
        out = [[index.setdefault(m, len(index)) for m in t] for t in tuples]
        return cls(tuple(index), out, tuple(properties))



@dataclass(frozen=True, slots=True, eq=False)
class CellValues:
    """One column of cells: an Arrow array of numbers or text, one per row, and its number format. A null is a cell
    with no value."""

    values: Any
    format: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.format, str):
            raise TypeError("CellValues.format is a str")
        _set(self, "values", _plain(self.values))


@dataclass(frozen=True, slots=True, eq=False)
class Cellset:
    """A whole cellset, the answer to an MDX `Execute`. A cell's ordinal is `row * len(values) + column`.

    `columns_axis` lists the columns, `rows_axis` the rows. With neither the answer has at most one cell; with only
    `columns_axis` it lists `rows * len(values)` tuples, the cells in order; `rows_axis` alone is not valid. With no
    `values` at all there are no cells, so the axes are written as they are (a header-only answer)."""

    cube: str
    values: Sequence[CellValues]
    columns_axis: LevelAxis | TupleAxis | None = None
    rows_axis: LevelAxis | TupleAxis | None = None
    slicer: Sequence[Member] = ()
    cell_properties: Sequence[CellProperty | str] = (CellProperty.VALUE, CellProperty.FORMAT_STRING)
    language: int = 1033  # the LCID written in each cell's Language

    def __post_init__(self) -> None:
        if not isinstance(self.cube, str):
            raise TypeError("Cellset.cube is a str")
        values = tuple(self.values) if isinstance(self.values, Sequence) and not isinstance(self.values, (str, bytes)) else None
        if values is None or not all(isinstance(v, CellValues) for v in values):
            raise TypeError("Cellset.values is a sequence of CellValues")
        _set(self, "values", values)
        for name in ("columns_axis", "rows_axis"):
            a = getattr(self, name)
            if a is not None and not isinstance(a, (LevelAxis, TupleAxis)):
                raise TypeError(f"Cellset.{name} is a LevelAxis, a TupleAxis or None")
        if self.rows_axis is not None and self.columns_axis is None:
            raise CellsetError("rows_axis needs a columns_axis (the first axis is the columns)")
        slicer = tuple(self.slicer)
        if not all(isinstance(m, Member) for m in slicer):
            raise TypeError("Cellset.slicer is a sequence of Members")
        _set(self, "slicer", slicer)
        _set(self, "cell_properties", _enums(CellProperty, self.cell_properties, "cell properties", "cell property"))
        if isinstance(self.language, bool) or not isinstance(self.language, int):
            raise TypeError("Cellset.language is an LCID (an int)")
        if not 0 <= self.language <= 0xFFFFFFFF:
            raise CellsetError("language is an unsigned 32-bit LCID")

    def to_xml(self, threads: int = 0) -> bytes:
        """The answer as bytes. `threads`: 0 uses the machine's cores (at most 8, and never more than are free: many
        answers written at once share them), 1 stays on the calling thread."""
        from arrow2xmla import _native

        if isinstance(threads, bool) or not isinstance(threads, int) or threads < 0:
            raise ValueError(f"threads is a number of threads, 0 or more (0: the machine's cores), not {threads!r}")
        return _native.cellset_xml(self, min(threads, 1 << 16))

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Cellset:
        """From plain data (for a spec that comes from JSON or a config): the keys are the field names, `values` a list
        of `(array, format)`, members and axes dicts of their fields (`{"level": {...}}` or `{"tuples": [...]}`).
        An unknown key is an error."""
        return _cellset_from(d)


def _only(d: Mapping[str, Any], keys: tuple[str, ...], what: str) -> None:
    if not isinstance(d, Mapping):
        raise TypeError(f"{what} is a dict, not {type(d).__name__}")
    for k in d:
        if k not in keys:
            raise CellsetError(f"{what} has no key {k!r} (keys: {', '.join(keys)})")


def _member_from(d: Any) -> Member:
    if isinstance(d, Member):
        return d
    _only(d, ("hierarchy", "unique_name", "caption", "level_name", "level_number", "parent_unique_name", "children_cardinality", "member_type"), "a member")
    for k in ("hierarchy", "level_name", "level_number"):
        if k not in d:
            raise CellsetError(f"a member needs {k!r}")
    return Member(**d)


def _axis_from(d: Any) -> LevelAxis | TupleAxis:
    if isinstance(d, (LevelAxis, TupleAxis)):
        return d
    _only(d, ("level", "tuples", "members", "properties"), "an axis")
    props = tuple(d.get("properties", ()))
    if "level" in d:
        if "tuples" in d or "members" in d:
            raise CellsetError("an axis is a level or tuples, not both")
        lv = d["level"]
        _only(lv, ("template", "keys", "all", "blank_caption"), "a level")
        if "template" not in lv or "keys" not in lv:
            raise CellsetError("a level needs 'template' and 'keys'")
        a = lv.get("all")
        return LevelAxis(_member_from(lv["template"]), lv["keys"], None if a is None else _member_from(a), lv.get("blank_caption", "(blank)"), props)
    if "tuples" not in d:
        raise CellsetError("an axis needs 'level' or 'tuples'")
    if "members" in d:
        return TupleAxis([_member_from(m) for m in d["members"]], d["tuples"], props)
    return TupleAxis.from_tuples([[_member_from(m) for m in t] for t in d["tuples"]], props)


def _cellset_from(d: Mapping[str, Any]) -> Cellset:
    _only(d, ("cube", "values", "columns_axis", "rows_axis", "slicer", "cell_properties", "language"), "a cellset")
    if "cube" not in d:
        raise CellsetError("a cellset needs 'cube'")
    vals = d.get("values", ())
    if not isinstance(vals, Sequence) or isinstance(vals, (str, bytes)):
        raise TypeError("values is a list of (array, number format) pairs")
    cells = []
    for v in vals:
        if isinstance(v, CellValues):
            cells.append(v)
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            cells.append(CellValues(*v))
        else:
            raise TypeError("each of values is an (array, number format) pair")
    kw: dict[str, Any] = {}
    for k in ("columns_axis", "rows_axis"):
        if d.get(k) is not None:
            kw[k] = _axis_from(d[k])
    if "slicer" in d:
        kw["slicer"] = [_member_from(m) for m in d["slicer"]]
    for k in ("cell_properties", "language"):
        if k in d:
            kw[k] = d[k]
    return Cellset(d["cube"], cells, **kw)
