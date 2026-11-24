"""An independent client reads our answers: olap4j's XMLA driver (Java), against the mock endpoint.

Skipped unless a JDK and the built driver are found:

    JAVA_HOME=/path/to/jdk tests/olap4j/build.sh .olap4j-build       # once
    OLAP4J_BUILD=.olap4j-build JAVA_HOME=/path/to/jdk pytest tests/e2e/test_olap4j.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pyarrow as pa
import pytest
from xmla_mock import Server, Table

BUILD = Path(os.environ.get("OLAP4J_BUILD", Path(__file__).parents[2] / ".olap4j-build"))


def _java() -> str | None:
    """A `java` that runs (macOS has a /usr/bin/java stub that only says there is no JDK)."""
    path = str(Path(os.environ["JAVA_HOME"]) / "bin" / "java") if "JAVA_HOME" in os.environ else shutil.which("java")
    try:
        ok = path is not None and subprocess.run([path, "-version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return path if ok else None


JAVA = _java()
CHECK = Path(__file__).parents[1] / "olap4j" / "Olap4jCheck.java"

pytestmark = pytest.mark.skipif(
    not (JAVA and (BUILD / "classes" / "org" / "olap4j" / "driver" / "xmla").is_dir()),
    reason="needs a JDK and the olap4j driver (tests/olap4j/build.sh)",
)

MDX = "SELECT {[Measures].[Revenue], [Measures].[Orders]} ON COLUMNS, [Customer].[Name].Members ON ROWS FROM [Sales]"


def read(table: Table, threads: int = 0) -> dict:
    classpath = os.pathsep.join([str(BUILD / "classes"), *map(str, (BUILD / "jars").glob("*.jar"))])
    with Server(table, threads) as url:
        out = subprocess.run([str(JAVA), "-cp", classpath, str(CHECK), url, MDX], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    seen = {"columns": [], "rows": [], "cells": {}, "axes": None}
    for line in out.stdout.splitlines():
        kind, _, rest = line.partition(" ")
        if kind == "AXES":
            seen["axes"] = int(rest)
        elif kind == "COLUMN":
            seen["columns"].append(rest)
        elif kind == "ROW":
            seen["rows"].append(rest)
        elif kind == "CELL":
            r, k, rest = rest.split(" ", 2)
            seen["cells"][(int(r), int(k))] = None if rest == "null" else tuple(rest.split(" ", 1))
    return seen


def test_olap4j_reads_the_pivot() -> None:
    table = Table(
        pa.array(["Acme", "Globex", None, "Initech & Co", "Ünï <cödé> ]x]"]),
        pa.array([2509.75, 1500.5, 920.0, None, 89.25, 0.0]),
        pa.array([49, 30, 12, 1, 4, 2]),
    )
    seen = read(table)
    assert seen["axes"] == 2
    assert seen["columns"] == ["Revenue", "Orders"]
    assert seen["rows"] == ["All", "Acme", "Globex", "(blank)", "Initech & Co", "Ünï <cödé> ]x]"]
    revenue, orders = table.revenue.to_pylist(), table.orders.to_pylist()
    for r in range(6):
        if revenue[r] is None:
            assert seen["cells"][(r, 0)] is None, "a null is a cell with no value"
        else:
            kind, value = seen["cells"][(r, 0)]
            assert kind == "Double" and float(value) == revenue[r]
        kind, value = seen["cells"][(r, 1)]
        assert kind == "Integer" and int(value) == orders[r]


def test_olap4j_reads_a_bigger_answer() -> None:
    """5 000 rows: olap4j builds a Java object per member and gets slow well before our writer does."""
    n = 5_000
    table = Table(pa.array([f"Customer {i} & Co" for i in range(n)]), pa.array([i / 7 for i in range(n + 1)]), pa.array(range(n + 1)))
    seen = read(table)
    assert len(seen["rows"]) == n + 1 and seen["rows"][-1] == f"Customer {n - 1} & Co"
    kind, value = seen["cells"][(1_234, 0)]
    assert kind == "Double" and float(value) == pytest.approx(1_234 / 7)
    assert seen["cells"][(1_234, 1)] == ("Integer", "1234")
