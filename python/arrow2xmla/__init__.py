"""Arrow in, XMLA out. The Python face of the `arrow2xmla` Rust crate: no engine, no product names, no dependencies.

    from arrow2xmla import Cellset, CellValues, LevelAxis, Member, TupleAxis, MemberType

    cs = Cellset(
        cube="Sales",
        columns_axis=TupleAxis.from_tuples([[Member("[Measures]", "[Measures].[MeasuresLevel]", 0, "[Measures].[Revenue]", "Revenue", member_type=MemberType.MEASURE)]]),
        rows_axis=LevelAxis(Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1, parent_unique_name="[Customer].[Name].[All]"), customer_names),
        values=[CellValues(revenue, "#,##0.00")],
    )
    xml = cs.to_xml()

Data comes in through the Arrow PyCapsule interface, and no Arrow library is needed or imported here. An array
(`__arrow_c_array__`: pyarrow, nanoarrow, ...) is read in place; a table or stream (`__arrow_c_stream__`: a DuckDB relation,
a polars DataFrame or Series, a pyarrow Table) is read with `columns(table)`, which gives its columns by name. The producer of an array is trusted to give buffers as long as it says: this is not a validator
of untrusted Arrow memory.

Everything this package refuses raises a subclass of `XmlaError` (itself a `ValueError`).
"""

from arrow2xmla import _native
from arrow2xmla._model import CellProperty, ChunkIterator, Cellset, CellValues, LevelAxis, Member, MemberProperty, MemberType, Rowset, RowsetColumn, TupleAxis, columns
from arrow2xmla._soap import Envelope, Method, fault
from arrow2xmla._native import ArrowColumn, CellsetError, DoctypeError, RequestError, RequestTooLarge, UnsupportedTypeError, XmlaError

try:
    from importlib.metadata import version as _version

    __version__ = _version("arrow2xmla")
except Exception:  # not installed as a distribution (a source checkout)
    __version__ = "0+unknown"


__all__ = [
    "__version__", "Cellset", "CellValues", "LevelAxis", "TupleAxis", "Member",
    "MemberType", "MemberProperty", "CellProperty", "ChunkIterator", "Rowset", "RowsetColumn", "columns", "ArrowColumn", "Envelope", "Method", "fault",
    "XmlaError", "CellsetError", "UnsupportedTypeError", "RequestError", "DoctypeError", "RequestTooLarge",
]  # fmt: skip

# the request parser is a build feature (`request`, on by default)
if hasattr(_native, "parse_request"):
    from arrow2xmla._request import Discover, Execute, Other, Request, Session, SessionKind, parse_request

    __all__ += ["parse_request", "Request", "Session", "SessionKind", "Discover", "Execute", "Other"]
