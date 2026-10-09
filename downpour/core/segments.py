"""Byte-range bookkeeping for multi-connection downloads.

A download is a list of segments that tile the file in order. Each segment is
``[start, end)`` with ``pos`` marking the next byte still to fetch. Splitting
is IDM-style dynamic segmentation: a connection that runs out of work takes
the back half of whichever segment has the most bytes left, so a slow
connection never ends up holding the last big piece of the file on its own.

Everything here is pure data manipulation. Callers serialise access with
their own lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Segment:
    start: int
    end: int | None  # exclusive; None while the total size is unknown
    pos: int
    active: bool = field(default=False, compare=False)  # a worker owns it

    @property
    def remaining(self) -> int | None:
        return None if self.end is None else max(0, self.end - self.pos)

    @property
    def done(self) -> bool:
        return self.end is not None and self.pos >= self.end

    def to_list(self) -> list:
        return [self.start, self.end, self.pos]

    @classmethod
    def from_list(cls, raw: list) -> "Segment":
        start, end, pos = raw
        return cls(int(start), None if end is None else int(end), int(pos))


def downloaded(segments: list[Segment]) -> int:
    return sum(s.pos - s.start for s in segments)


def all_done(segments: list[Segment]) -> bool:
    return bool(segments) and all(s.done for s in segments)


def compact(segments: list[Segment]) -> list[Segment]:
    """Merge neighbouring finished segments so saved state stays small."""
    out: list[Segment] = []
    for seg in segments:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.done
            and seg.done
            and not prev.active
            and not seg.active
            and prev.end == seg.start
        ):
            prev.end = seg.end
            prev.pos = seg.pos
        else:
            out.append(seg)
    return out


def split(segments: list[Segment], min_size: int) -> Segment | None:
    """Cut the segment with the most bytes left in half.

    Returns the new back half (not yet active), or None when no segment has
    at least ``2 * min_size`` bytes left - below that, a new connection costs
    more than it saves.
    """
    best: Segment | None = None
    for seg in segments:
        if seg.end is None or seg.done:
            continue
        if best is None or seg.remaining > best.remaining:  # type: ignore[operator]
            best = seg
    if best is None or best.remaining < 2 * min_size:  # type: ignore[operator]
        return None
    mid = best.pos + best.remaining // 2  # type: ignore[operator]
    tail = Segment(mid, best.end, mid)
    best.end = mid
    segments.insert(segments.index(best) + 1, tail)
    return tail


def acquire(segments: list[Segment], min_size: int) -> Segment | None:
    """Find work for an idle connection and mark it active.

    Unowned unfinished segments come first (they exist after a resume or a
    failed connection); otherwise the largest active segment is split.
    """
    idle = [s for s in segments if not s.active and not s.done]
    if idle:
        seg = max(idle, key=lambda s: float("inf") if s.end is None else s.remaining)
        seg.active = True
        return seg
    tail = split(segments, min_size)
    if tail is not None:
        tail.active = True
    return tail
