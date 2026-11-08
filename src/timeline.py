"""Source-aligned views; partial records never pretend to have word timing."""
from .models import Episode, Record
from .validation import validate_episode


class ClipEvent(Record):
    event_id: str
    event_type: str
    speaker_raw: str | None
    source_start_ms: int
    source_end_ms: int
    clip_start_ms: int
    clip_end_ms: int
    content_status: str
    text: str | None


def format_time(ms: int) -> str:
    hours, remainder = divmod(ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:03d}"


def reconstruct(episode: Episode, start_ms: int, end_ms: int) -> list[ClipEvent]:
    validate_episode(episode)
    if not 0 <= start_ms < end_ms <= episode.duration_ms:
        raise ValueError("Selection outside episode timeline")
    result = []
    for event in episode.temporal_events():
        if event.start_ms >= end_ms or event.end_ms <= start_ms:
            continue
        full = start_ms <= event.start_ms and event.end_ms <= end_ms
        result.append(ClipEvent(
            event_id=event.event_id, event_type=event.event_type, speaker_raw=event.speaker_raw,
            source_start_ms=event.start_ms, source_end_ms=event.end_ms,
            clip_start_ms=max(start_ms, event.start_ms)-start_ms,
            clip_end_ms=min(end_ms, event.end_ms)-start_ms,
            content_status="full" if full else "partial_unknown",
            text=event.text if full else None))
    return result


def render_timeline(episode: Episode, start_ms: int = 0, end_ms: int | None = None) -> str:
    end_ms = episode.duration_ms if end_ms is None else end_ms
    rows = [f"{episode.episode_id} | {format_time(start_ms)}–{format_time(end_ms)} | screenplay timeline"]
    for event in reconstruct(episode, start_ms, end_ms):
        interval = f"{format_time(event.source_start_ms)}–{format_time(event.source_end_ms)}"
        content = event.text if event.text is not None else "[Partial record: selected content cannot be localized from source timing]"
        rows.append(f"{event.event_id} | {interval} | {event.event_type} | {event.speaker_raw or '-'} | {content}")
    return "\n".join(rows) + "\n"
