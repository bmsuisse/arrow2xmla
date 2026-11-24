"""Arrow in, XMLA out. The Python face of the `arrow2xmla` Rust crate: no engine, no product names.

Arrays are read through the Arrow PyCapsule interface (`__arrow_c_array__`): pyarrow, polars, nanoarrow and others work,
and none is imported here. A chunked array is combined into one first.
"""

from arrow2xmla import _native


def _plain(x):
    return x.combine_chunks() if hasattr(x, "combine_chunks") else x


def _prepared(spec: dict) -> dict:
    axes = [
        {**a, "level": {**a["level"], "keys": _plain(a["level"]["keys"])}} if "level" in a else a
        for a in spec["axes"]
    ]
    return {**spec, "axes": axes, "columns": [(_plain(v), f) for v, f in spec.get("columns", [])]}


def cellset_xml(spec: dict, threads: int = 0) -> bytes:
    """The cellset (an XMLA MDDataset) of `spec` as bytes. `threads`: 0 uses the machine's cores (at most 8), 1 stays
    on the calling thread."""
    return _native.cellset_xml(_prepared(spec), threads)


def cellset_xml_chunks(spec: dict, threads: int = 0) -> list[bytes]:
    """The same answer as a list of chunks of about 1 MB, to send one by one."""
    return _native.cellset_xml_chunks(_prepared(spec), threads)


parse_request = _native.parse_request

__all__ = ["cellset_xml", "cellset_xml_chunks", "parse_request"]
