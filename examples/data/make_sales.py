"""Writes the two small results examples/databricks.py rebuilds when there is no warehouse.

Run it once (`uv run --with pyarrow python examples/data/make_sales.py`) and commit the files. Both are Arrow IPC streams of
the same data: customer (text, one NULL), revenue (double), orders (bigint).
  sales.arrows        one row per customer, in no particular order (the raw result)
  sales_pivot.arrows  the All row first (customer NULL, the totals), then the customers sorted by name, NULL last: what a
                      query ordered that way returns
Revenues are multiples of 0.25, so a sum is exact in any order and both files give the same totals to the last digit.
"""

import pathlib
import random

import pyarrow as pa
import pyarrow.ipc as ipc

random.seed(7)
names = ["Acme", "Globex", "Initech & Co", "Hooli", "Umbrella", "Stark <Industries>", "Wayne", "Wonka", "Soylent", "Cyberdyne",
         "Tyrell", "Oscorp", "Vandelay", "Pied Piper", "Dunder Mifflin", "Gringotts", "Aperture", "Massive Dynamic", "Nakatomi",
         "Monsters Inc", "Bluth", "Prestige Worldwide", "Sterling Cooper", "Los Pollos", "Duff"]
customers = names[:12] + [None] + names[12:]
random.shuffle(customers)
table = pa.table({
    "customer": pa.array(customers, pa.string()),
    "revenue": pa.array([random.randint(400, 360000) / 4 for _ in customers], pa.float64()),
    "orders": pa.array([random.randint(1, 400) for _ in customers], pa.int64()),
})
rows = table.to_pylist()
ordered = sorted(rows, key=lambda r: (r["customer"] is None, r["customer"] or ""))
pivot = [{"customer": None, "revenue": sum(r["revenue"] for r in rows), "orders": sum(r["orders"] for r in rows)}] + ordered
for name, data in (("sales.arrows", table), ("sales_pivot.arrows", pa.Table.from_pylist(pivot, schema=table.schema))):
    with ipc.new_stream(pathlib.Path(__file__).with_name(name), table.schema) as w:
        w.write_table(data)
print(table.num_rows, "rows")
