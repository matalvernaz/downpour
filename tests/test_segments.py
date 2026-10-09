from downpour.core import segments as seglib
from downpour.core.segments import Segment


def test_split_takes_back_half_of_largest():
    segs = [Segment(0, 100, 10, active=True), Segment(100, 1000, 100, active=True)]
    tail = seglib.split(segs, min_size=10)
    assert tail == Segment(550, 1000, 550)
    assert segs[1].end == 550
    assert segs == sorted(segs, key=lambda s: s.start)


def test_split_refuses_small_segments():
    segs = [Segment(0, 100, 50, active=True)]
    assert seglib.split(segs, min_size=30) is None


def test_acquire_prefers_idle_segments():
    segs = [Segment(0, 100, 100), Segment(100, 200, 120), Segment(200, 1000, 300, active=True)]
    got = seglib.acquire(segs, min_size=10)
    assert got is segs[1] and got.active


def test_acquire_never_splits_unknown_length():
    segs = [Segment(0, None, 500, active=True)]
    assert seglib.acquire(segs, min_size=1) is None


def test_segments_tile_the_file_after_many_splits():
    segs = [Segment(0, 10_000, 0, active=True)]
    for _ in range(20):
        seglib.acquire(segs, min_size=50)
    assert segs[0].start == 0 and segs[-1].end == 10_000
    for a, b in zip(segs, segs[1:]):
        assert a.end == b.start


def test_compact_merges_finished_neighbours_only():
    segs = [Segment(0, 10, 10), Segment(10, 20, 20), Segment(20, 30, 25), Segment(30, 40, 40)]
    out = seglib.compact(segs)
    assert [s.to_list() for s in out] == [[0, 20, 20], [20, 30, 25], [30, 40, 40]]
    assert seglib.downloaded(out) == 35


def test_round_trip():
    seg = Segment(5, None, 7)
    assert Segment.from_list(seg.to_list()) == seg
