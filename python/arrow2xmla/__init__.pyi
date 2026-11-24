from typing import Any, Literal, NotRequired, Protocol, TypedDict

__version__: str

class _ArrowArray(Protocol):
    def __arrow_c_array__(self, requested_schema: object | None = ...) -> tuple[object, object]: ...

class Member(TypedDict):
    """One member of a hierarchy (a value of a field, its All member, a measure)."""

    hierarchy: str
    level_name: str
    level_number: int  # >= 0
    unique_name: NotRequired[str]  # a level axis makes each value's own
    caption: NotRequired[str]  # likewise
    parent_unique_name: NotRequired[str]  # "" for All and for a measure
    children_cardinality: NotRequired[int]
    member_type: NotRequired[int]  # 1 a value, 2 the All member, 3 a measure, 4 a calculated member

class Level(TypedDict):
    """The values of one hierarchy's level, read from a text array: nothing is built per value."""

    template: Member  # hierarchy, level_name, level_number, parent_unique_name, children_cardinality, member_type
    keys: _ArrowArray  # text; a null is the blank member
    all: NotRequired[Member | None]  # the All member, shown first
    blank_caption: NotRequired[str]  # default "(blank)"

class LevelAxis(TypedDict):
    level: Level
    properties: NotRequired[list[str]]  # DIMENSION PROPERTIES, e.g. "MEMBER_CAPTION"

class TupleAxis(TypedDict):
    """Explicit tuples: member dicts, or `members` listed once and each tuple naming them by index (faster)."""

    tuples: list[list[Member]] | list[list[int]]
    members: NotRequired[list[Member]]
    properties: NotRequired[list[str]]

class Spec(TypedDict):
    cube: str
    axes: list[LevelAxis | TupleAxis]  # two: columns, then rows (one: rows * columns cells)
    columns: list[tuple[_ArrowArray, str]]  # (values, number format); a cell's ordinal is row * len(columns) + column
    slicer: NotRequired[list[Member]]
    cell_properties: NotRequired[list[str]]  # default VALUE, FORMAT_STRING
    language: NotRequired[int]  # default 1033

def cellset_xml(spec: Spec, threads: int = 0) -> bytes: ...
def cellset_xml_chunks(spec: Spec, threads: int = 0) -> list[bytes]: ...

class _Call(TypedDict, total=False):
    kind: Literal["discover", "execute", "other"]
    request_type: str
    restrictions: list[tuple[str, list[str]]]
    statement: str
    properties: list[tuple[str, str]]

class Request(TypedDict):
    session: tuple[Literal["none", "begin", "use", "end"], str | None]
    call: _Call

def parse_request(body: bytes) -> Request: ...
