"""Rust against the Python reference on one big light-axis pivot (a field on rows, 3 measures)."""
import os, sys, time, random
os.environ.setdefault("PUBLIC_API_ORIGINS", "[]"); os.environ.setdefault("AI_ASK_PER_USER_PER_HOUR", "60")
sys.path.insert(0, os.path.abspath(os.environ["ONEVIEW_PATH"]))
sys.path.insert(0, os.path.dirname(__file__) + "/parity")
import pyarrow as pa
from types import SimpleNamespace
from arrow2xmla import cellset_xml
from backend.xmla import query, service
from test_cellset_parity import H, all_member, value_member, measure_member, member_dict, python_cells

N = int(sys.argv[1]) if len(sys.argv) > 1 else 60_000
rnd = random.Random(1)
keys = [f"Customer {i} & Co" for i in range(N)]
cols = [(pa.array([rnd.random() * 1e6 for _ in range(N + 1)]), "#,##0.00"), (pa.array([rnd.randint(0, 999) for _ in range(N + 1)]), "0"), (pa.array([rnd.random() for _ in range(N + 1)]), "0.0%")]
labels = ["Sales", "Qty", "Margin"]
cube = SimpleNamespace(name="Sales")
light = query.LightAxis(value_member(H, "x"), all_member(), keys)
t = time.perf_counter()
cells = python_cells(cols)
cs = query.Cellset(cube, [[(measure_member(l),) for l in labels], [(all_member(),)]], cells, (), [(), ()], ("VALUE", "FORMAT_STRING"), {1: light})
py = "".join(service.cellset_chunks(cs)); tp = time.perf_counter() - t
spec = {"cube": "Sales", "axes": [{"tuples": [[member_dict(measure_member(l))] for l in labels]}, {"level": {"all": member_dict(all_member()), "template": member_dict(value_member(H, "x")), "keys": pa.array(keys)}}], "columns": cols}
best = 9
for _ in range(5):
    t = time.perf_counter(); rs = cellset_xml(spec); best = min(best, time.perf_counter() - t)
assert rs.decode() == py
print(f"rows={N:,} bytes={len(rs)/1e6:.1f} MB  python={tp*1000:.0f} ms (incl. building cells)  rust={best*1000:.0f} ms  speedup x{tp/best:.0f}")
