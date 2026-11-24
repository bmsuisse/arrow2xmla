"""The Rust writer against the pure-Python reference on one big pivot (a field on rows, 3 measures).

    uv run python tests/bench_cellset.py 60000
"""

import random
import sys
import time
from types import SimpleNamespace

import pyarrow as pa
import reference as ref

from arrow2xmla import cellset_xml

H = "[Customer].[Name]"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 60_000
rnd = random.Random(1)
keys = [f"Customer {i} & Co" for i in range(N)]
cols = [
    (pa.array([rnd.random() * 1e6 for _ in range(N + 1)]), "#,##0.00"),
    (pa.array([rnd.randint(0, 999) for _ in range(N + 1)]), "0"),
    (pa.array([rnd.random() for _ in range(N + 1)]), "0.0%"),
]
labels = ["Sales", "Qty", "Margin"]


def member(**kw: object) -> ref.Member:
    return ref.Member(H, f"{H}.[All]", "All", f"{H}.[(All)]", 0, children=1000, kind=2) if kw.get("all") else ref.Member(H, ref.member_uname(H, kw["v"]), str(kw["v"]), f"{H}.[Name]", 1, parent=f"{H}.[All]")


def measure(label: str) -> ref.Member:
    return ref.Member("[Measures]", f"[Measures].[{label}]", label, "[Measures].[MeasuresLevel]", 0, kind=3)


def as_dict(m: ref.Member) -> dict:
    return dict(hierarchy=m.hierarchy, unique_name=m.unique, caption=m.caption, level_name=m.level, level_number=m.number, parent_unique_name=m.parent, children_cardinality=m.children, member_type=m.kind)


all_m, sample = member(all=True), member(v="x")
t = time.perf_counter()
cells = {r * 3 + k: (v, f) for k, (c, f) in enumerate(cols) for r, v in enumerate(c.to_pylist())}
cs = ref.Cellset(SimpleNamespace(name="Sales"), [[(measure(l),) for l in labels], [(all_m,)]], cells, (), [(), ()], ("VALUE", "FORMAT_STRING"), {1: ref.LightAxis(sample, all_m, keys)})
py = ref.cellset_xml(cs)
tp = time.perf_counter() - t
spec = {"cube": "Sales", "axes": [{"tuples": [[as_dict(measure(l))] for l in labels]}, {"level": {"all": as_dict(all_m), "template": as_dict(sample), "keys": pa.array(keys)}}], "columns": cols}
best = 9.0
for _ in range(5):
    t = time.perf_counter()
    out = cellset_xml(spec)
    best = min(best, time.perf_counter() - t)
assert out.decode() == py
print(f"rows={N:,} {len(out) / 1e6:.1f} MB  python={tp * 1000:.0f} ms  rust={best * 1000:.0f} ms  x{tp / best:.0f}")
