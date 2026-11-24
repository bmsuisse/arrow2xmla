"""A pure-Python reference for the XMLA cellset writer: the plain, slow implementation the Rust crate is held to, byte
for byte (tests/parity). Independent of the crate; it needs only the standard library."""

# ruff: noqa: E501
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
import re
from typing import Any
from xml.sax.saxutils import escape as _escape, quoteattr

XA = "urn:schemas-microsoft-com:xml-analysis"


def escape(text: str) -> str:
    """`saxutils.escape`, and a carriage return as a reference (a parser reads a raw one back as a line feed)."""
    return _escape(text, {"\r": "&#13;"})


def attr_escape(text: str) -> str:
    """Text for a double-quoted attribute value."""
    return escape(text).replace('"', "&quot;").replace("\n", "&#10;").replace("\t", "&#9;")


LANGUAGE = 1033

CELL_PROPERTIES = {
    "VALUE": ("Value", None),
    "FORMAT_STRING": ("FormatString", "xsd:string"),
    "FORMATTED_VALUE": ("FmtValue", "xsd:string"),
    "LANGUAGE": ("Language", "xsd:unsignedInt"),
    "CELL_ORDINAL": ("CellOrdinal", "xsd:unsignedInt"),
    "BACK_COLOR": ("BackColor", "xsd:unsignedInt"),
    "FORE_COLOR": ("ForeColor", "xsd:unsignedInt"),
    "FONT_NAME": ("FontName", "xsd:string"),
    "FONT_SIZE": ("FontSize", "xsd:unsignedShort"),
    "FONT_FLAGS": ("FontFlags", "xsd:int"),
    "UPDATEABLE": ("Updateable", "xsd:unsignedInt"),
    "ACTION_TYPE": ("ActionType", "xsd:unsignedInt"),
}


@dataclass(frozen=True)
class Member:
    hierarchy: str
    unique: str
    caption: str
    level: str
    number: int
    parent: str = ""
    children: int = 0
    kind: int = 1  # 1 a value, 2 the All member, 3 a measure, 4 a calculated member


MEMBER_PROPERTIES: dict[str, Callable[[Member], object]] = {
    "PARENT_UNIQUE_NAME": lambda m: m.parent,
    "HIERARCHY_UNIQUE_NAME": lambda m: m.hierarchy,
    "MEMBER_UNIQUE_NAME": lambda m: m.unique,
    "MEMBER_CAPTION": lambda m: m.caption,
    "LEVEL_UNIQUE_NAME": lambda m: m.level,
    "LEVEL_NUMBER": lambda m: m.number,
    "CHILDREN_CARDINALITY": lambda m: m.children,
    "PARENT_LEVEL": lambda _: 0,
    "PARENT_COUNT": lambda m: 1 if m.parent else 0,
    "MEMBER_TYPE": lambda m: m.kind,
}


@dataclass
class LightAxis:
    sample: Member
    all_member: Member | None
    values: list[object]


@dataclass
class Cellset:
    cube: Any  # anything with a `name`
    axes: list[list[tuple[Member, ...]]]
    cells: dict[int, tuple[object, str]]  # ordinal -> (value, format string)
    slicer: tuple[Member, ...]
    properties: list[tuple[str, ...]] = field(default_factory=list)
    cell_properties: tuple[str, ...] = ("VALUE", "FORMAT_STRING")
    light: dict[int, LightAxis] = field(default_factory=dict)


def member_uname(hierarchy: str, key: object) -> str:
    return f"{hierarchy}.&[{str(key).replace(']', ']]')}]"


_SPECIAL = re.compile("[&<>\r]").search


def _esc(text: str) -> str:
    """`escape` (three passes over the string) only when it has something to escape: most captions do not."""
    return escape(text) if _SPECIAL(text) else text


def _info(h: str, props: tuple[str, ...] = ()) -> str:
    e = attr_escape(h)
    return (
        f'<HierarchyInfo name="{e}"><UName name="{e}.[MEMBER_UNIQUE_NAME]" type="xsd:string"/>'
        f'<Caption name="{e}.[MEMBER_CAPTION]" type="xsd:string"/><LName name="{e}.[LEVEL_UNIQUE_NAME]" type="xsd:string"/>'
        f'<LNum name="{e}.[LEVEL_NUMBER]" type="xsd:int"/><DisplayInfo name="{e}.[DISPLAY_INFO]" type="xsd:unsignedInt"/>'
        + "".join(f'<{p} name="{e}.[{p}]" type="xsd:string"/>' for p in props)
        + "</HierarchyInfo>"
    )


_DRILLED_DOWN, _SAME_PARENT = 0x10000, 0x20000  # DISPLAY_INFO flags; the low 16 bits are the children count


def display_infos(axis: list[tuple[Member, ...]]) -> list[list[int]]:
    """How Excel lays out each member of each tuple: its children count, whether the next member at that position
    holds one of its children (it is drilled down: a subtotal above them), whether the previous one has the same
    parent. "Next" and "previous" skip the tuples that repeat this member (the inner members vary under it). Each
    tuple's common prefix with the one before (`same`, in members) answers "the same outer members" with integers,
    so the axis is read once per position."""
    n = len(axis)
    width = len(axis[0]) if axis else 0
    infos = [[min(m.children, 0xFFFF) for m in t] for t in axis]
    keys = [tuple([x.unique for x in t]) for t in axis]
    same = [0] * n  # same[i]: how many leading members tuple i shares with tuple i - 1
    for i in range(1, n):
        a, b = keys[i - 1], keys[i]
        k = 0
        while k < width and a[k] == b[k]:
            k += 1
        same[i] = k
    for j in range(width):
        start = [0] * n  # the first tuple of the run that repeats members 0..j
        for i in range(1, n):
            start[i] = start[i - 1] if same[i] > j else i
        end = [n] * n  # the first tuple after that run
        for i in range(n - 2, -1, -1):
            end[i] = end[i + 1] if same[i + 1] > j else i + 1
        for i, t in enumerate(axis):
            m = t[j]
            e = end[i]
            if e < n and same[e] >= j and axis[e][j].parent == m.unique:  # the next run: same outer members
                infos[i][j] |= _DRILLED_DOWN
            b = start[i]
            if b and m.parent and same[b] >= j and axis[b - 1][j].parent == m.parent:
                infos[i][j] |= _SAME_PARENT
    return infos


def _member_parts(m: Member, props: tuple[str, ...]) -> tuple[str, str]:
    """A member's XML around its DisplayInfo (the one part that depends on where it stands in the axis)."""
    head = (
        f"<Member Hierarchy={quoteattr(m.hierarchy)}>"
        f"<UName>{escape(m.unique)}</UName><Caption>{escape(m.caption)}</Caption>"
        f"<LName>{escape(m.level)}</LName><LNum>{m.number}</LNum><DisplayInfo>"
    )
    tail = "</DisplayInfo>" + "".join(f"<{p}>{escape(str(MEMBER_PROPERTIES[p](m)))}</{p}>" for p in props)
    return head, tail + "</Member>"


def _member(m: Member, props: tuple[str, ...] = (), info: int = 0) -> str:
    head, tail = _member_parts(m, props)
    return f"{head}{info}{tail}"


_BATCH = 20_000  # tuples or cells per chunk of a streamed answer: a few MB of XML


_KEY, _CAPTION, _INFO = "\x01K", "\x01C", "\x02"  # where a value's key, caption and DisplayInfo go in a template


def _light_chunks(axis: LightAxis, props: tuple[str, ...]) -> Iterator[str]:
    """The tuples of an axis of one hierarchy's members (`LightAxis`), as `tuples` writes them but from the
    values: one template per axis, each value's key and caption put in it (the text `_member_parts` makes for a member
    with those names), and its DisplayInfo from its place: All is drilled down when values follow it, a value has the
    same parent as the one before it unless it is the first."""
    proto = replace(axis.sample, unique=member_uname(axis.sample.hierarchy, _KEY), caption=_CAPTION)
    head, tail = _member_parts(proto, props)
    template = f"<Tuple>{head}{_INFO}{tail}</Tuple>"
    out: list[str] = []
    if axis.all_member is not None:
        info = min(axis.all_member.children, 0xFFFF) | (_DRILLED_DOWN if axis.values else 0)
        all_head, all_tail = _member_parts(axis.all_member, props)
        out.append(f"<Tuple>{all_head}{info}{all_tail}</Tuple>")
    for i, v in enumerate(axis.values):
        key, caption = ("", "(blank)") if v is None else (str(v), str(v))
        out.append(
            template.replace(_INFO, "0" if i == 0 else str(_SAME_PARENT))
            .replace(_KEY, _esc(key.replace("]", "]]")))
            .replace(_CAPTION, _esc(caption))
        )
        if len(out) >= _BATCH:
            yield "".join(out)
            out.clear()
    yield "".join(out)


def cellset_chunks(cs: Cellset) -> Iterator[str]:
    """The cellset's XML in chunks of a few MB, so an answer is sent while it is built and never held whole."""
    props = [cs.properties[n] if n < len(cs.properties) else () for n in range(len(cs.axes))]
    info = "".join(
        f'<AxisInfo name="Axis{n}">{"".join(_info(h, props[n]) for h in dict.fromkeys(m.hierarchy for m in (axis[0] if axis else ())))}</AxisInfo>'
        for n, axis in enumerate(cs.axes)
    )
    slicer_info = "".join(_info(h) for h in dict.fromkeys(m.hierarchy for m in cs.slicer))
    yield (
        f'<root xmlns="{XA}:mddataset" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<OlapInfo><CubeInfo><Cube><CubeName>{escape(cs.cube.name)}</CubeName></Cube></CubeInfo><AxesInfo>{info}"
        f'<AxisInfo name="SlicerAxis">{slicer_info}</AxisInfo></AxesInfo>'
        "<CellInfo>"
        + "".join(
            f'<{CELL_PROPERTIES[p][0]} name="{p}"'
            + (f' type="{t}"' if (t := CELL_PROPERTIES[p][1]) else "")
            + "/>"
            for p in cs.cell_properties
        )
        + "</CellInfo></OlapInfo><Axes>"
    )

    def tuples(n: int, axis: list[tuple[Member, ...]]) -> Iterator[str]:
        infos = display_infos(axis)
        # each member's XML once, however many tuples it is in: by identity (an axis builds each member once and
        # holds it; hashing the frozen dataclass hashes every field, per tuple)
        pieces: dict[tuple[int, int], str] = {}  # (member, its DisplayInfo) -> its XML: few distinct per member
        halves: dict[int, tuple[str, str]] = {}
        out: list[str] = []
        for t, di in zip(axis, infos, strict=True):
            out.append("<Tuple>")
            for m, d in zip(t, di, strict=True):
                if (piece := pieces.get(k := (id(m), d))) is None:
                    if (head_tail := halves.get(id(m))) is None:
                        head_tail = halves[id(m)] = _member_parts(m, props[n])
                    piece = pieces[k] = f"{head_tail[0]}{d}{head_tail[1]}"
                out.append(piece)
            out.append("</Tuple>")
            if len(out) > _BATCH * 4:
                yield "".join(out)
                out.clear()
        yield "".join(out)

    for n, axis in enumerate(cs.axes):
        yield f'<Axis name="Axis{n}"><Tuples>'
        yield from (_light_chunks(cs.light[n], props[n]) if n in cs.light else tuples(n, axis))
        yield "</Tuples></Axis>"
    yield (
        f'<Axis name="SlicerAxis"><Tuples><Tuple>{"".join(_member(m) for m in cs.slicer)}</Tuple></Tuples></Axis>'
        if cs.slicer
        else '<Axis name="SlicerAxis"><Tuples/></Axis>'
    )
    yield "</Axes><CellData>"
    with_value = "VALUE" in cs.cell_properties
    tails: dict[str, str] = {}  # per format string: the cell's other properties (only those asked for, in order)

    def tail(fmt: str) -> str:
        if fmt not in tails:
            extra = {"FORMAT_STRING": escape(fmt), "LANGUAGE": str(LANGUAGE)}
            tails[fmt] = "".join(
                f"<{CELL_PROPERTIES[p][0]}>{extra[p]}</{CELL_PROPERTIES[p][0]}>"
                for p in cs.cell_properties
                if p in extra
            )
        return tails[fmt]

    out = []  # only the properties the statement asked for: an undeclared one makes Excel's provider fail
    for ordinal, (value, fmt) in sorted(cs.cells.items()):
        if not with_value:
            out.append(f'<Cell CellOrdinal="{ordinal}">{tail(fmt)}</Cell>')
        elif isinstance(value, bool) or not isinstance(value, int | float):
            out.append(
                f'<Cell CellOrdinal="{ordinal}"><Value xsi:type="xsd:string">{escape(str(value))}</Value>{tail(fmt)}</Cell>'
            )
        elif isinstance(value, int):  # a count; xsd:int is 32 bits, so a larger value is a long
            kind = "int" if -(2**31) <= value < 2**31 else "long" if -(2**63) <= value < 2**63 else "unsignedLong"
            out.append(f'<Cell CellOrdinal="{ordinal}"><Value xsi:type="xsd:{kind}">{value}</Value>{tail(fmt)}</Cell>')
        else:
            text = "NaN" if value != value else "INF" if value == float("inf") else "-INF" if value == float("-inf") else repr(value)
            out.append(
                f'<Cell CellOrdinal="{ordinal}"><Value xsi:type="xsd:double">{text}</Value>{tail(fmt)}</Cell>'
            )
        if len(out) >= _BATCH:
            yield "".join(out)
            out.clear()
    yield "".join(out)
    yield "</CellData></root>"




def cellset_xml(cs: Cellset) -> str:
    return "".join(cellset_chunks(cs))
