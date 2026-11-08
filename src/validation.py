"""Shared source checks for text and canonical JSON imports."""
from .models import Episode, Issue

CHECK_CODES = {"empty_episode", "missing_scenes", "source_order_differs",
               "invalid_scene_interval", "outside_scene", "missing_scene_timing"}


def validate_episode(episode: Episode) -> Episode:
    """Recompute source quality rather than trusting imported JSON issue lists.

    Structural inconsistency rejects an import. Timing ambiguity preserves source
    records with warnings and closes the source_ready gate. No timestamp repair.
    """
    if len({s.scene_id for s in episode.scenes}) != len(episode.scenes):
        raise ValueError("Duplicate scene IDs")
    if len({e.event_id for e in episode.events}) != len(episode.events):
        raise ValueError("Duplicate event IDs")
    if len({e.source_order for e in episode.events}) != len(episode.events):
        raise ValueError("Duplicate source order positions")
    scenes = {s.scene_id for s in episode.scenes}
    if any(e.episode_id != episode.episode_id or
           (e.scene_id is not None and e.scene_id not in scenes) for e in episode.events):
        raise ValueError("Unknown episode/scene reference")
    episode.issues = [i for i in episode.issues if i.code not in CHECK_CODES]

    def issue(code, message, severity="warning", line=None):
        episode.issues.append(Issue(code=code, message=message, severity=severity,
                                    source_file=episode.source_file, source_line=line))

    if not episode.events:
        issue("empty_episode", "No timed events extracted", "error")
    if not episode.scenes:
        issue("missing_scenes", "No explicit scene boundaries found")
    original = episode.original_events()
    if any(b.start_ms < a.start_ms for a,b in zip(original, original[1:])):
        issue("source_order_differs", "Temporal order governs playback; original order is preserved")
    ambiguous = False
    for scene in episode.scenes:
        if scene.start_ms is None or scene.end_ms is None:
            issue("missing_scene_timing", f"{scene.scene_id} has incomplete declared timing")
        if scene.start_ms is not None and scene.end_ms is not None and scene.end_ms <= scene.start_ms:
            issue("invalid_scene_interval", f"{scene.scene_id} has nonpositive duration", "error")
            ambiguous = True
        for event in (e for e in episode.events if e.scene_id == scene.scene_id):
            if ((scene.start_ms is not None and event.start_ms < scene.start_ms) or
                (scene.end_ms is not None and event.end_ms > scene.end_ms)):
                issue("outside_scene", f"{event.event_id} exceeds declared scene range; time base needs inspection",
                      line=event.source_line_start)
                ambiguous = True
    if ambiguous:
        # Conservative until a documented source-review decision resolves it.
        for event in episode.events:
            event.timing_status = "uncertain"
    return episode
