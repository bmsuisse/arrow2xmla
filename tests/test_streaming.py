"""Streaming: the answer in chunks as it is made, never whole in memory."""

import io
import subprocess
import sys
import textwrap
import threading
import time

import pyarrow as pa
import pytest

import arrow2xmla as ax

M = ax.Member("[H]", "[H].[L]", 1)
MEASURE_A = ax.Member("[Measures]", "[Measures].[ML]", 0, "[Measures].[A]", "A", member_type=ax.MemberType.MEASURE)
MEASURE_B = ax.Member("[Measures]", "[Measures].[ML]", 0, "[Measures].[B]", "B", member_type=ax.MemberType.MEASURE)


def cellset(n: int) -> ax.Cellset:
    keys = pa.array([f"key {i} & <x>" for i in range(n)], pa.string())
    return ax.Cellset(
        "C",
        [ax.CellValues(pa.array([i / 3 for i in range(n)], pa.float64()), "0.00"), ax.CellValues(pa.array(range(n), pa.int64()), "0")],
        ax.TupleAxis.from_tuples([[MEASURE_A], [MEASURE_B]]),
        ax.LevelAxis(M, keys),
    )


class Chunks:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, b: bytes) -> int:
        self.parts.append(bytes(b))
        return len(b)


@pytest.mark.parametrize("n", [0, 1, 7, 50_000, 300_000])
def test_write_to_is_the_same_bytes_as_to_xml(n: int) -> None:
    cs = cellset(n)
    sink = Chunks()
    cs.write_to(sink)
    assert b"".join(sink.parts) == cs.to_xml()
    if n >= 300_000:
        assert len(sink.parts) > 5, "a big answer arrives in several chunks"
        assert max(map(len, sink.parts)) < 4 << 20


def test_write_to_a_file_object() -> None:
    buf = io.BytesIO()
    cellset(1000).write_to(buf, threads=1)
    assert buf.getvalue() == cellset(1000).to_xml(threads=1)


@pytest.mark.parametrize("threads", [0, 1, 3])
def test_iter_xml_is_the_same_bytes(threads: int) -> None:
    cs = cellset(200_000)
    assert b"".join(cs.iter_xml(threads)) == cs.to_xml(threads)


def test_a_bad_cellset_raises_before_the_first_chunk() -> None:
    bad = ax.Cellset("C", [ax.CellValues(pa.array([1.0, 2.0]), "0"), ax.CellValues(pa.array([1.0]), "0")], ax.TupleAxis.from_tuples([[MEASURE_A], [MEASURE_B]]), ax.LevelAxis(M, pa.array(["a", "b"])))
    sink = Chunks()
    with pytest.raises(ax.CellsetError):
        bad.write_to(sink)
    assert sink.parts == []
    with pytest.raises(ax.CellsetError):
        next(bad.iter_xml())


def test_an_error_of_the_writer_is_raised_as_it_is() -> None:
    class Boom(Exception):
        pass

    class Failing:
        def __init__(self) -> None:
            self.calls = 0

        def write(self, b: bytes) -> int:
            self.calls += 1
            if self.calls == 2:
                raise Boom("disk full")
            return len(b)

    f = Failing()
    with pytest.raises(Boom, match="disk full"):
        cellset(400_000).write_to(f)
    assert f.calls == 2, "nothing is written after the error"


def test_stopping_early_stops_the_writer() -> None:
    before = threading.active_count()
    it = cellset(400_000).iter_xml(buffer=1)
    next(it)
    it.close()
    deadline = time.time() + 5
    while threading.active_count() > before and time.time() < deadline:
        time.sleep(0.05)
    assert threading.active_count() == before, "the writer thread ended"


def test_bad_arguments() -> None:
    for bad in (-1, True, "1"):
        with pytest.raises(ValueError):
            cellset(1).write_to(Chunks(), bad)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        cellset(1).write_to(object())


MEMORY = textwrap.dedent(
    """
    import resource, sys
    import pyarrow as pa
    import arrow2xmla as ax

    class Null:
        n = 0
        def write(self, b):
            self.n += len(b)

    n = 600_000
    m = ax.Member("[H]", "[H].[L]", 1)
    ms = [ax.Member("[Measures]", "[Measures].[ML]", 0, f"[Measures].[{c}]", c, member_type=ax.MemberType.MEASURE) for c in "ABC"]
    cs = ax.Cellset("C", [ax.CellValues(pa.array([i / 3 for i in range(n)]), "0.00"), ax.CellValues(pa.array(range(n)), "0"), ax.CellValues(pa.array([i * 2 for i in range(n)]), "0")],
                    ax.TupleAxis.from_tuples([[x] for x in ms]), ax.LevelAxis(m, pa.array([f"customer number {i} & co" for i in range(n)])))
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.argv[1] == "stream":
        sink = Null(); cs.write_to(sink); size = sink.n
    else:
        size = len(cs.to_xml())
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    print(size // 1_000_000, (peak - base) * scale // 1_000_000)
    """
)


def test_streaming_holds_a_fraction_of_the_answer_in_memory() -> None:
    def run(mode: str) -> tuple[int, int]:
        out = subprocess.run([sys.executable, "-c", MEMORY, mode], capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, out.stderr
        size, grown = map(int, out.stdout.split())
        return size, grown

    size, streamed = run("stream")
    _, whole = run("whole")
    assert size > 200, f"a big answer ({size} MB)"
    assert streamed < size / 4, f"streaming grew by {streamed} MB for a {size} MB answer"
    assert whole > size, f"the whole answer takes at least its size ({whole} MB)"


def test_a_writer_that_takes_part_of_a_chunk_is_offered_the_rest() -> None:
    class Trickle:
        def __init__(self) -> None:
            self.got = bytearray()

        def write(self, b: bytes) -> int:
            n = min(len(b), 10_007)
            self.got += b[:n]
            return n

    cs = cellset(20_000)
    sink = Trickle()
    cs.write_to(sink)
    assert bytes(sink.got) == cs.to_xml()


def test_a_writer_that_takes_nothing_is_an_error() -> None:
    class Stuck:
        def write(self, b: bytes) -> int:
            return 0

    with pytest.raises(ax.CellsetError, match="took no bytes"):
        cellset(10).write_to(Stuck())


def test_a_writer_that_returns_nothing_is_taken_to_have_written_it_all() -> None:
    class Silent:
        def __init__(self) -> None:
            self.got = bytearray()

        def write(self, b: bytes) -> None:
            self.got += b

    sink = Silent()
    cellset(1000).write_to(sink)
    assert bytes(sink.got) == cellset(1000).to_xml()


def test_a_dropped_iterator_stops_its_writer() -> None:
    import gc

    before = threading.active_count()
    it = cellset(400_000).iter_xml(buffer=1)
    next(it)
    del it
    gc.collect()
    deadline = time.time() + 5
    while threading.active_count() > before and time.time() < deadline:
        time.sleep(0.05)
    assert threading.active_count() == before


def test_the_iterator_is_a_context_manager_and_closes() -> None:
    with cellset(400_000).iter_xml(buffer=1) as it:
        assert next(it)[:5] == b"<root"
    assert list(it) == []
    assert isinstance(it, ax.ChunkIterator)


def test_iter_xml_checks_its_arguments_at_once() -> None:
    for kwargs in ({"threads": -1}, {"buffer": 0}, {"buffer": None}, {"buffer": True}, {"buffer": 1.5}):
        with pytest.raises(ValueError):
            cellset(1).iter_xml(**kwargs)  # type: ignore[arg-type]


def test_a_stalled_stream_does_not_hold_threads_from_other_answers() -> None:
    """The writer's threads are leased while it renders a wave, not while its sink is busy."""
    big = cellset(300_000)
    stalled = big.iter_xml(buffer=1)
    next(stalled)  # the writer now waits for this consumer
    t0 = time.time()
    other = cellset(300_000).to_xml(threads=0)
    assert time.time() - t0 < 10 and other.startswith(b"<root")
    stalled.close()


def test_the_first_chunk_of_an_invalid_cellset_is_an_error() -> None:
    bad = ax.Cellset("C", [ax.CellValues(pa.array([1.0]), "0")], ax.TupleAxis.from_tuples([[MEASURE_A], [MEASURE_B]]))
    with pytest.raises(ax.CellsetError):
        list(bad.iter_xml())
