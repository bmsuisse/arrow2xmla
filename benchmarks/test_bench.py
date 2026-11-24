"""Speed benchmarks (pytest-benchmark). Run:

    uv run maturin develop -r && uv run pytest benchmarks --benchmark-only
    uv run pytest benchmarks --benchmark-only --benchmark-save=before     # keep a run
    uv run pytest benchmarks --benchmark-only --benchmark-compare=0001    # compare with it

Each cellset benchmark writes a whole answer from Arrow arrays; the extra info is the size of the answer.
"""

import glob
import os
import random

import pyarrow as pa
import pytest

import arrow2xmla
import compat

H = "[Customer].[Name]"
ALL = dict(hierarchy=H, unique_name=f"{H}.[All]", caption="All", level_name=f"{H}.[(All)]", level_number=0, children_cardinality=1000, member_type=2)
VALUE = dict(hierarchy=H, level_name=f"{H}.[Name]", level_number=1, parent_unique_name=f"{H}.[All]")
MEASURES = [dict(hierarchy="[Measures]", unique_name=f"[Measures].[{n}]", caption=n, level_name="[Measures].[MeasuresLevel]", level_number=0, member_type=3) for n in ("Sales", "Qty", "Margin")]


def keys(n: int, special: bool = False) -> pa.Array:
    return pa.array([f"Customer {i} & <Co> ]x" if special else f"Customer {i} Co" for i in range(n)])


def floats(n: int, seed: int) -> pa.Array:
    rnd = random.Random(seed)
    return pa.array([rnd.random() * 10 ** rnd.randint(0, 7) for _ in range(n)])


def ints(n: int, seed: int) -> pa.Array:
    rnd = random.Random(seed)
    return pa.array([rnd.randint(0, 10**6) for _ in range(n)])


def pivot(n: int, measures: int, special: bool = False) -> dict:
    cols = [(floats(n + 1, 1), "#,##0.00"), (ints(n + 1, 2), "0"), (floats(n + 1, 3), "0.0%")][:measures]
    return {
        "cube": "Sales",
        "axes": [
            {"tuples": [[m] for m in MEASURES[:measures]]},
            {"properties": ["MEMBER_CAPTION"], "level": {"all": ALL, "template": VALUE, "keys": keys(n, special)}},
        ],
        "columns": cols,
    }


CASES = {
    "60k rows x 1 float": pivot(60_000, 1),
    "60k rows x 3 mixed": pivot(60_000, 3),
    "60k rows x 3, escapes": pivot(60_000, 3, special=True),
    "490k rows x 3 mixed": pivot(490_000, 3),
}


@pytest.mark.parametrize("case", CASES)
def test_cellset(benchmark, case: str) -> None:
    spec = CASES[case]
    out = benchmark(compat.to_cellset(spec).to_xml)
    benchmark.extra_info["answer_MB"] = round(len(out) / 1e6, 1)



def _nested(n_outer: int = 4000, inner: int = 4):
    """Two hierarchies nested on rows: each outer value with its All, then its inner values."""
    h1, h2 = "[T].[A]", "[T].[B]"

    def m(h: str, v: str | None) -> dict:
        if v is None:
            return dict(hierarchy=h, unique_name=f"{h}.[All]", caption="All", level_name=f"{h}.[(All)]", level_number=0, children_cardinality=1000, member_type=2)
        return dict(hierarchy=h, unique_name=f"{h}.&[{v}]", caption=v, level_name=f"{h}.[L]", level_number=1, parent_unique_name=f"{h}.[All]")

    members, index, tuples = [], {}, []

    def at(h: str, v: str | None) -> int:
        if (h, v) not in index:
            index[(h, v)] = len(members)
            members.append(m(h, v))
        return index[(h, v)]

    for i in range(n_outer):
        a = at(h1, f"a{i}")
        tuples.append([a, at(h2, None)])
        tuples += [[a, at(h2, f"b{j}")] for j in range(inner)]
    return members, tuples


def test_nested_tuples(benchmark) -> None:
    """Two hierarchies nested on rows (explicit tuples, DisplayInfo worked out), 4 000 x 5 tuples, as member dicts."""
    members, tuples = _nested()
    spec = {"cube": "C", "axes": [{"properties": ["MEMBER_CAPTION"], "tuples": [[members[k] for k in t] for t in tuples]}], "columns": [(floats(len(tuples), 4), "0")]}
    benchmark(compat.to_cellset(spec).to_xml)


def test_nested_tuples_indexed(benchmark) -> None:
    """The same, the members listed once and each tuple naming them by index."""
    members, tuples = _nested()
    spec = {"cube": "C", "axes": [{"properties": ["MEMBER_CAPTION"], "members": members, "tuples": tuples}], "columns": [(floats(len(tuples), 4), "0")]}
    benchmark(compat.to_cellset(spec).to_xml)


FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "excel", "*request*.xml")))
REQUESTS = [open(f, "rb").read() for f in FIXTURES]


def test_parse_excel_requests(benchmark) -> None:
    """All the recorded Excel requests, one after the other."""

    def run() -> None:
        for body in REQUESTS:
            arrow2xmla.parse_request(body)

    benchmark(run)
    benchmark.extra_info["requests"] = len(REQUESTS)


def test_parse_big_statement(benchmark) -> None:
    members = ",".join(f"[Customer].[Name].&amp;[Customer {i}]" for i in range(5000))
    body = f'<E xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body><Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement>SELECT {{{members}}} ON 0 FROM [C]</Statement></Command></Execute></Body></E>'.encode()
    benchmark(arrow2xmla.parse_request, body)
