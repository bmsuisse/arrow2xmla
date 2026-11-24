"""Tests build specs as plain dicts (`axes`, `columns`); this turns them into the public `Cellset`."""

import arrow2xmla


def to_cellset(spec: dict) -> arrow2xmla.Cellset:
    if isinstance(spec, arrow2xmla.Cellset):
        return spec
    if not isinstance(spec, dict):
        raise TypeError(f"the spec is a dict, not {type(spec).__name__}")
    axes = spec.get("axes", [])
    if not isinstance(axes, (list, tuple)):
        raise TypeError(f"axes is a list of axes, not {type(axes).__name__}")
    d = {k: v for k, v in spec.items() if k not in ("axes", "columns")}
    d["values"] = spec.get("columns", [])
    for k, a in zip(("columns_axis", "rows_axis"), axes[:2]):
        d[k] = a
    if len(axes) > 2:
        raise arrow2xmla.CellsetError("a cellset has at most two axes")
    return arrow2xmla.Cellset.from_dict(d)


def cellset_xml(spec: dict, threads: int = 0) -> bytes:
    return to_cellset(spec).to_xml(threads)
