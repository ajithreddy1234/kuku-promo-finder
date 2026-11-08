"""Conservative cut points supported by events/scenes; camera overlap is valid."""
from .models import Episode
from .validation import validate_episode


def usable_boundaries(episode: Episode) -> list[int]:
    validate_episode(episode)
    if not episode.source_ready:
        return []
    points = {p for e in episode.events for p in (e.start_ms, e.end_ms)}
    points.update(p for s in episode.scenes for p in (s.start_ms, s.end_ms) if p is not None)
    protected = [e for e in episode.events if e.event_type in ("dialogue", "action")]
    return sorted(p for p in points if not any(e.start_ms < p < e.end_ms for e in protected))


def interval_iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union if union else 0.0


def union_duration(intervals: list[tuple[int, int]]) -> int:
    total, previous_end = 0, -1
    for start, end in sorted(intervals):
        total += max(0, end - max(start, previous_end))
        previous_end = max(previous_end, end)
    return total
