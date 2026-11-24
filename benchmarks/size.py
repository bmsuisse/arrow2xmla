"""The size of what ships: the compiled module and the wheel.

    uv run python benchmarks/size.py
"""

import glob
import os
import subprocess
import tempfile

root = os.path.join(os.path.dirname(__file__), "..")
so = glob.glob(os.path.join(root, "python", "arrow2xmla", "_native*"))
with tempfile.TemporaryDirectory() as d:
    subprocess.run(["maturin", "build", "--release", "-o", d, "-q"], cwd=root, check=True, capture_output=True)
    wheel = glob.glob(os.path.join(d, "*.whl"))[0]
    print(f"module  {os.path.getsize(so[0]) / 1024:8.0f} KB  ({os.path.basename(so[0])})")
    print(f"wheel   {os.path.getsize(wheel) / 1024:8.0f} KB  ({os.path.basename(wheel)})")
