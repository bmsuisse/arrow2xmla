"""The Databricks example, offline: a result rebuilt from Arrow IPC bytes goes through the same code as a live one."""

import os
import pathlib
import subprocess
import sys

import pytest

duckdb = pytest.importorskip("duckdb", reason="the example reshapes in DuckDB")
pytest.importorskip("arrowbricks", reason="the example reads a Databricks result")
from arrowbricks._core import read_ipc_stream  # noqa: E402

import arrow2xmla as ax  # noqa: E402
from arrow2xmla import CellValues, Cellset, LevelAxis, Member, MemberType, TupleAxis  # noqa: E402

ROOT = pathlib.Path(__file__).parents[1]
DATA = (ROOT / "examples" / "data" / "sales.arrows").read_bytes()


def test_the_example_runs_both_paths_offline_and_they_write_the_same_bytes() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}
    out = subprocess.run([sys.executable, str(ROOT / "examples" / "databricks.py")], capture_output=True, text=True, timeout=300, env=env)
    assert out.returncode == 0, out.stderr[-800:]
    assert "mode: OFFLINE" in out.stdout
    assert "ordered result: 27 rows" in out.stdout
    sizes = {}
    for line in out.stdout.splitlines():
        if line.startswith(("straight :", "reshaping:")):
            sizes[line.split(":")[0].strip()] = int(line.split(":")[1].split()[0].replace(",", ""))
    assert set(sizes) == {"straight", "reshaping"}
    assert sizes["straight"] == sizes["reshaping"] > 10_000
    assert "both paths wrote the same bytes" in out.stdout


def cellset(columns: dict, all_row: bool = False) -> bytes:
    member = Member("[Customer].[Name]", "[Customer].[Name].[Name]", 1)
    measure = lambda n: Member("[Measures]", "[Measures].[MeasuresLevel]", 0, f"[Measures].[{n}]", n)  # noqa: E731
    return Cellset(
        cube="Sales",
        columns_axis=TupleAxis.from_tuples([[measure("Revenue")], [measure("Orders")]]),
        rows_axis=LevelAxis(member, columns["customer"], all=Member("[Customer].[Name]", "[Customer].[Name].[(All)]", 0, "[Customer].[Name].[All]", "All", member_type=MemberType.ALL) if all_row else None),
        values=[CellValues(columns["revenue"]), CellValues(columns["orders"])],
    ).to_xml()


def test_an_arrowbricks_table_reads_like_the_pyarrow_table_of_the_same_data() -> None:
    pa = pytest.importorskip("pyarrow", reason="the comparison table comes from pyarrow")
    ipc = pytest.importorskip("pyarrow.ipc")
    reference = ipc.open_stream(DATA).read_all()
    result = read_ipc_stream(DATA)
    assert result.column_names == reference.column_names and result.num_rows == reference.num_rows
    assert reference.column("customer").null_count == 1
    ours, theirs = ax.columns(result), ax.columns(reference)
    assert list(ours) == list(theirs)
    for name in ours:  # every column on its own, then the whole answer
        single = {"customer": ours["customer"], "revenue": ours["revenue"], "orders": ours["orders"]}
        single[name] = ours[name]
        other = {**single, name: theirs[name]}
        assert cellset(single) == cellset(other), name
    assert cellset(ours) == cellset(theirs)
    assert b"Initech &amp; Co" in cellset(ours)


def test_an_arrowbricks_table_copied_into_duckdb_is_read_repeatedly_on_every_duckdb() -> None:
    """DuckDB before 1.5 reads a registered Arrow stream once; the example copies it into a table, which any version rereads."""
    result = read_ipc_stream(DATA)
    con = duckdb.connect()
    con.register("raw_result", result)
    con.execute("create table result as select * from raw_result")
    first = con.sql("select count(*), sum(orders), count(customer) from result").fetchall()
    assert first == con.sql("select count(*), sum(orders), count(customer) from result").fetchall()
    assert first[0][0] == 26 and first[0][2] == 25  # the NULL customer is there
    assert ax.columns(con.sql("select customer from result order by customer nulls last"))["customer"] is not None


def test_a_timestamp_is_refused_with_the_column_named() -> None:
    pa = pytest.importorskip("pyarrow", reason="builds the timestamp result")
    ipc = pytest.importorskip("pyarrow.ipc")
    import io

    table = pa.table({"day": pa.array([1], pa.timestamp("us"))})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    with pytest.raises(ax.UnsupportedTypeError, match="day"):
        ax.columns(read_ipc_stream(sink.getvalue()))


def test_the_straight_path_is_the_two_array_construction() -> None:
    """One result, the All row first: its customer column without that row is the keys, the other columns are the cells."""
    ordered = read_ipc_stream((ROOT / "examples" / "data" / "sales_pivot.arrows").read_bytes())
    cols = ax.columns(ordered)
    one = cellset({**cols, "customer": cols["customer"].slice(1)}, True)
    pa = pytest.importorskip("pyarrow", reason="the two-array construction is built from pyarrow arrays")
    ipc = pytest.importorskip("pyarrow.ipc")
    t = ipc.open_stream((ROOT / "examples" / "data" / "sales_pivot.arrows").read_bytes()).read_all()
    two = cellset({"customer": t["customer"].combine_chunks()[1:], "revenue": t["revenue"].combine_chunks(), "orders": t["orders"].combine_chunks()}, True)
    assert one == two
    assert t["customer"][0].as_py() is None and t["customer"][1].as_py() is not None and t["customer"][-1].as_py() is None  # All row, ..., the blank customer last
