"""Arrow in, XMLA out. The Python face of the `arrow2xmla` Rust crate: no engine, no product names, no dependencies.

Arrays are read through the Arrow PyCapsule interface (`__arrow_c_array__`): pyarrow, nanoarrow and every other
library that exports it work, and none is imported here. A chunked array is combined into one first (pyarrow's
`combine_chunks`; an object that only exports `__arrow_c_stream__`, such as a polars Series, goes through pyarrow if it
is installed, else convert it with `.to_arrow()`). The producer of an array is trusted to give buffers as long as it
says: this is not a validator of untrusted Arrow memory.
"""

from arrow2xmla import _native

try:
    from importlib.metadata import version as _version

    __version__ = _version("arrow2xmla")
except Exception:  # not installed as a distribution (a source checkout)
    __version__ = "0+unknown"


def _plain(x):
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
            raise ValueError("an Arrow stream needs pyarrow to be combined into an array: pass an array (.to_arrow())") from None
    return x  # the native side says what it was


def _prepared(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise TypeError(f"the spec is a dict, not {type(spec).__name__}")
    axes = spec.get("axes", [])
    columns = spec.get("columns", [])
    if not isinstance(axes, (list, tuple)):
        raise TypeError(f"axes is a list of axes, not {type(axes).__name__}")
    if not isinstance(columns, (list, tuple)):
        raise TypeError(f"columns is a list of (array, format) pairs, not {type(columns).__name__}")
    for c in columns:
        if not (isinstance(c, (list, tuple)) and len(c) == 2):
            raise TypeError("each column is an (array, number format) pair")
    axes = [
        {**a, "level": {**a["level"], "keys": _plain(a["level"].get("keys"))}} if isinstance(a, dict) and "level" in a else a
        for a in axes
    ]
    return {**spec, "axes": list(axes), "columns": [(_plain(v), f) for v, f in columns]}


def _threads(threads: object) -> int:
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 0:
        raise ValueError(f"threads is a number of threads, 0 or more (0: the machine's cores), not {threads!r}")
    return threads


def cellset_xml(spec: dict, threads: int = 0) -> bytes:
    """The cellset (an XMLA MDDataset) of `spec` as bytes. `threads`: 0 uses the machine's cores (at most 8, and never
    more than are free: many answers written at once share them), 1 stays on the calling thread."""
    return _native.cellset_xml(_prepared(spec), _threads(threads))


def cellset_xml_chunks(spec: dict, threads: int = 0) -> list[bytes]:
    """The same answer as a list of chunks of about 1 MB, to send one by one."""
    return _native.cellset_xml_chunks(_prepared(spec), _threads(threads))


# the request parser is a build feature (`request`, on by default)
parse_request = getattr(_native, "parse_request", None)

__all__ = ["__version__", "cellset_xml", "cellset_xml_chunks", *(["parse_request"] if parse_request else [])]
