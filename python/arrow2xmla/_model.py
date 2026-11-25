"""The cellset model: frozen dataclasses and enums, checked when they are made."""

from __future__ import annotations

import atexit
import queue
import threading
import weakref
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
    """The cell properties of `CellInfo`, in the order they are listed (a client asked for them in that order).
    `VALUE`, `FORMAT_STRING` and `LANGUAGE` are written in each cell. The others are declared only: `CellInfo` names
    them (with their XSD type) and no cell carries them, so a client that asked for them gets the declaration and the
    defaults. A name in neither group is refused."""

    VALUE = "VALUE"
    FORMAT_STRING = "FORMAT_STRING"
    LANGUAGE = "LANGUAGE"
    BACK_COLOR = "BACK_COLOR"  # declared only: BackColor, xsd:unsignedInt
    FORE_COLOR = "FORE_COLOR"  # declared only: ForeColor, xsd:unsignedInt
    FONT_FLAGS = "FONT_FLAGS"  # declared only: FontFlags, xsd:int
    FORMATTED_VALUE = "FORMATTED_VALUE"  # declared only: FmtValue, xsd:string
    FONT_NAME = "FONT_NAME"  # declared only: FontName, xsd:string
    FONT_SIZE = "FONT_SIZE"  # declared only: FontSize, xsd:unsignedShort
    UPDATEABLE = "UPDATEABLE"  # declared only: Updateable, xsd:unsignedInt
    ACTION_TYPE = "ACTION_TYPE"  # declared only: ActionType, xsd:unsignedInt
    CELL_ORDINAL = "CELL_ORDINAL"  # declared only: CellOrdinal, xsd:unsignedInt


def _plain(x: Any) -> Any:
    """One Arrow array: as it is, a column read from a stream, or a chunked array combined."""
    from arrow2xmla import _native

    if isinstance(x, _native.ArrowColumn) or hasattr(x, "__arrow_c_array__"):
        return x
    if hasattr(x, "combine_chunks"):  # a pyarrow ChunkedArray (a Table has the method too, and combines to a Table)
        combined = x.combine_chunks()
        if hasattr(combined, "__arrow_c_array__"):
            return combined
    if hasattr(x, "__arrow_c_stream__"):  # a polars Series, a one-column stream: read here
        columns = _native.read_stream(x)
        if len(columns) != 1 or columns[0][0] is not None:
            raise CellsetError("this is a table, not one column: take a column of it with `columns(table)[name]`")
        return columns[0][1]
    raise TypeError(f"expected an Arrow array (something with __arrow_c_array__ or __arrow_c_stream__), not {type(x).__name__}")


def columns(table: Any) -> dict[str, Any]:
    """The columns of an Arrow table or stream by name, ready for `CellValues`, `LevelAxis` and `Rowset`.
    Anything that exports `__arrow_c_stream__` works (a DuckDB relation, a polars DataFrame, a pyarrow Table).

        columns(duckdb.sql("select name, sum(x) as total from t group by name"))["total"]

    The batches are copied once into one array per column. Types: floats, integers, text, and decimals (DuckDB's `DECIMAL` and
    `HUGEINT`): a decimal is read as an int64 when its scale is 0 and every value fits, else as a double. Anything else (dates,
    booleans, 256-bit decimals, ...) raises `UnsupportedTypeError` naming the column: cast it in the query.
    A column's `slice(offset, length=None)` is a copy of some of its rows, e.g. `columns(t)["name"].slice(1)` for the keys of a result whose first row is the All row."""
    from arrow2xmla import _native

    if not hasattr(table, "__arrow_c_stream__"):
        raise TypeError(f"expected an Arrow table or stream (something with __arrow_c_stream__), not {type(table).__name__}")
    read = _native.read_stream(table)
    if any(name is None for name, _ in read):
        raise CellsetError("this is one column, not a table: use it as it is")
    names = [name for name, _ in read]
    if len(set(names)) != len(names):
        raise CellsetError("the table has two columns of one name: name them apart")
    return {name: column for name, column in read}


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


def _threads(threads: object) -> int:
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 0:
        raise ValueError(f"threads is a number of threads, 0 or more (0: the machine's cores), not {threads!r}")
    return min(threads, 1 << 16)


class _Stopped(Exception):
    """The consumer of `iter_xml` stopped before the end."""


class _Sink:
    def __init__(self, write: Any) -> None:
        self.write = write


_live: weakref.WeakSet[ChunkIterator] = weakref.WeakSet()


def _stop_all() -> None:
    for it in list(_live):
        it.close()


atexit.register(_stop_all)


class ChunkIterator:
    """The chunks of an answer, written ahead on a worker thread (`Cellset.iter_xml`). Closing it, using it up or dropping
    it stops the worker; it is also a context manager."""

    def __init__(self, cellset: Cellset, threads: int, buffer: int) -> None:
        self._chunks: queue.Queue[Any] = queue.Queue(maxsize=buffer)
        self._stop = threading.Event()
        self._done = False
        chunks, stop = self._chunks, self._stop  # the worker holds these, not the iterator: dropping it is noticed
        finished = object()
        self._finished = finished

        def put(chunk: Any) -> None:
            while not stop.is_set():
                try:
                    chunks.put(chunk, timeout=0.1)
                    return
                except queue.Full:
                    continue
            raise _Stopped

        def run() -> None:
            try:
                cellset.write_to(_Sink(put), threads)
                put(finished)
            except _Stopped:
                pass
            except BaseException as e:  # noqa: BLE001: handed to the consumer
                try:
                    put(e)
                except _Stopped:
                    pass

        self._worker = threading.Thread(target=run, daemon=True, name="arrow2xmla-writer")
        _live.add(self)
        self._worker.start()

    def __iter__(self) -> ChunkIterator:
        return self

    def __next__(self) -> bytes:
        if self._done:
            raise StopIteration
        item = self._chunks.get()
        if item is self._finished:
            self.close()
            raise StopIteration
        if isinstance(item, BaseException):
            self.close()
            raise item
        return item

    def close(self) -> None:
        """Stops the writer (the chunks not yet read are dropped)."""
        self._done = True
        self._stop.set()
        if self._worker is not threading.current_thread():
            self._worker.join()
        _live.discard(self)

    def __enter__(self) -> ChunkIterator:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.set()


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
    blank member.

    `captions`: an optional second text array of the same length, the caption of each member. Then the keys are written
    exactly as given (only XML-escaped, `]` doubled): an empty key is `&[]` and `~tilde` stays `~tilde`, so a caller that
    wants its own escaping (`~empty`, `~~tilde`) passes the keys already escaped, and the captions (`(empty)`) itself.
    A null caption is `blank_caption`; a null key is still the key `""`."""

    template: Member  # hierarchy, level_name, level_number, parent_unique_name, children_cardinality, member_type
    keys: Any  # an Arrow text array (string, large_string or string_view)
    all: Member | None = None  # the All member, shown first
    blank_caption: str = "(blank)"
    properties: tuple[MemberProperty, ...] = ()
    captions: Any = None  # an Arrow text array as long as `keys`: each member's caption; None: the key is the caption

    def __post_init__(self) -> None:
        if not isinstance(self.template, Member):
            raise TypeError("LevelAxis.template is a Member")
        if self.all is not None and not isinstance(self.all, Member):
            raise TypeError("LevelAxis.all is a Member or None")
        if not isinstance(self.blank_caption, str):
            raise TypeError("LevelAxis.blank_caption is a str")
        _set(self, "keys", _plain(self.keys))
        if self.captions is not None:
            _set(self, "captions", _plain(self.captions))
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
        answers written at once share them), 1 stays on the calling thread. The whole answer is in memory (about
        twice its size at the peak): for a big one, `write_to` or `iter_xml` hold about a megabyte."""
        from arrow2xmla import _native

        return _native.cellset_xml(self, _threads(threads))

    def write_to(self, fp: Any, threads: int = 0) -> None:
        """Writes the answer to `fp.write(bytes)` in chunks of about 1 MB as they are made, so it never exists whole in
        memory. Whatever is wrong with the cellset is raised before the first chunk; an error of `fp.write` is raised
        as it is."""
        from arrow2xmla import _native

        _native.cellset_write(self, fp.write, _threads(threads))

    def iter_xml(self, threads: int = 0, buffer: int = 4) -> ChunkIterator:
        """The answer as an iterator of chunks of about 1 MB, written ahead on another thread (at most `buffer` chunks
        ahead). For a streaming response: it is lazy, so pass it on without `list()` or `*`. Whatever is wrong with the
        cellset is raised by the first `next()`. The writer stops when the iterator is closed, used up or dropped."""
        _threads(threads)
        if isinstance(buffer, bool) or not isinstance(buffer, int) or buffer < 1:
            raise ValueError(f"buffer is a number of chunks, 1 or more, not {buffer!r}")
        return ChunkIterator(self, threads, buffer)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Cellset:
        """From plain data (for a spec that comes from JSON or a config): the keys are the field names, `values` a list
        of `(array, format)`, members and axes dicts of their fields (`{"level": {...}}` or `{"tuples": [...]}`).
        An unknown key is an error."""
        return _cellset_from(d)


@dataclass(frozen=True, slots=True, eq=False)
class RowsetColumn:
    """One column of a rowset: its name (an XML name, e.g. `CUBE_NAME`) and an Arrow array, one value per row; a null is
    left out of its row. The type declared to the client follows the array (text `xsd:string`, integers `xsd:int` /
    `xsd:long`, floats `xsd:double`); `xsd_type` overrides it (`"xsd:boolean"` for text holding `true` / `false`). A
    boolean array (pyarrow) is written as `true` / `false` and declared `xsd:boolean`."""

    name: str
    values: Any
    xsd_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("RowsetColumn.name is a str")
        if self.xsd_type is not None and not isinstance(self.xsd_type, str):
            raise TypeError("RowsetColumn.xsd_type is a str or None")
        values = _plain(self.values)
        if str(getattr(values, "type", None)) == "bool":  # the XMLA way: text `true` / `false`, declared `xsd:boolean`
            import pyarrow as pa
            import pyarrow.compute as pc

            values = pc.if_else(values, pa.scalar("true"), pa.scalar("false"))
            if self.xsd_type is None:
                _set(self, "xsd_type", "xsd:boolean")
        _set(self, "values", values)


@dataclass(frozen=True, slots=True, eq=False)
class Rowset:
    """The answer to a `Discover` (cubes, dimensions, properties, ...): columns of Arrow arrays of one length."""

    columns: Sequence[RowsetColumn]

    def __post_init__(self) -> None:
        columns = tuple(self.columns) if isinstance(self.columns, Sequence) and not isinstance(self.columns, (str, bytes)) else None
        if columns is None or not all(isinstance(c, RowsetColumn) for c in columns):
            raise TypeError("Rowset.columns is a sequence of RowsetColumn")
        _set(self, "columns", columns)

    @classmethod
    def from_columns(cls, columns: Mapping[str, Any]) -> Rowset:
        """From `{name: array}`, typed after the arrays."""
        return cls([RowsetColumn(name, values) for name, values in columns.items()])

    def to_xml(self) -> bytes:
        """The rowset (`<root>` with its schema block and rows) as bytes; for `Envelope(Method.DISCOVER).wrap(...)`."""
        from arrow2xmla import _native

        return _native.rowset_xml(self)


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
        _only(lv, ("template", "keys", "all", "blank_caption", "captions"), "a level")
        if "template" not in lv or "keys" not in lv:
            raise CellsetError("a level needs 'template' and 'keys'")
        a = lv.get("all")
        return LevelAxis(_member_from(lv["template"]), lv["keys"], None if a is None else _member_from(a), lv.get("blank_caption", "(blank)"), props, lv.get("captions"))
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
