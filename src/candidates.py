"""Deterministic discovery mechanics, not evaluated promo recommendations."""
import hashlib
import json
from typing import Literal
from pydantic import Field, model_validator

from .models import Episode, Record
from .timeline import ClipEvent, reconstruct
from .boundaries import usable_boundaries, interval_iou, union_duration


class StorySignal(Record):
    signal_id: str
    episode_id: str
    kind: Literal["hook", "conflict", "escalation", "reversal", "reveal", "open_question", "resolution", "cliffhanger", "context_dependency"]
    source_event_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1)
    priority: Literal["high", "medium", "low"] = "medium"
    resolution_event_ids: list[str] = Field(default_factory=list)
    origin: Literal["synthetic", "human", "model"]


class Candidate(Record):
    candidate_id: str
    candidate_version: int = Field(default=1, ge=1)
    candidate_hash: str
    episode_id: str
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    duration_ms: int
    source: Literal["semantic", "wildcard", "baseline", "editor"]
    discovery_priority: int
    signal_ids: list[str]
    selected_event_ids: list[str]
    selected_content: list[ClipEvent]
    warnings: list[str]
    statistics: dict[str, int | float]
    status: Literal["unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def valid_duration(self):
        if self.end_ms - self.start_ms != self.duration_ms or not 20000 <= self.duration_ms <= 60000:
            raise ValueError("Candidate must be 20–60 seconds with consistent duration")
        return self


def make_candidate(episode: Episode, start: int, end: int, source="semantic", signals=None,
                   priority=0, version=1, candidate_id=None) -> Candidate:
    points = usable_boundaries(episode)
    if not points:
        raise ValueError("Episode requires source review")
    if start not in points or end not in points or not 20000 <= end-start <= 60000:
        raise ValueError("Unsafe/unsupported boundary or invalid promo duration")
    content = reconstruct(episode, start, end)
    if not content:
        raise ValueError("Empty clip")
    payload = {"schema": "candidate-v1", "source": episode.model_dump(mode="json"),
               "start": start, "end": end, "version": version}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    dialogue = [(max(start,e.start_ms), min(end,e.end_ms)) for e in episode.events
                if e.event_type == "dialogue" and e.start_ms < end and e.end_ms > start]
    covered = union_duration(dialogue)
    warnings = sorted({f"partial_{e.event_type}_record" for e in content if e.content_status != "full"})
    if any(i.code=='tolerated_scene_audio_overlap' and i.message.split(':',1)[0] in {e.event_id for e in content} for i in episode.issues):
        warnings.append('scene_audio_bridge_requires_editor')
    return Candidate(candidate_id=candidate_id or f"{episode.episode_id}_C{digest[:12]}",
                     candidate_version=version, candidate_hash=digest, episode_id=episode.episode_id,
                     start_ms=start, end_ms=end, duration_ms=end-start, source=source,
                     discovery_priority=priority, signal_ids=sorted(signals or []),
                     selected_event_ids=[e.event_id for e in content], selected_content=content,
                     warnings=warnings, statistics={"dialogue_coverage_ms": covered,
                         "event_types": len({e.event_type for e in content}),
                         "narrative_events": sum(e.event_type in ("dialogue","action") for e in content)})


def revise_candidate(episode: Episode, candidate: Candidate, start: int, end: int) -> Candidate:
    if episode.episode_id != candidate.episode_id:
        raise ValueError("Cannot revise against a different episode")
    return make_candidate(episode,start,end,candidate.source,candidate.signal_ids,
                          candidate.discovery_priority,candidate.candidate_version+1,candidate.candidate_id)


def validate_signals(episode: Episode, signals: list[StorySignal]):
    known = {e.event_id for e in episode.events}
    if len({s.signal_id for s in signals}) != len(signals):
        raise ValueError("Duplicate signal IDs")
    for signal in signals:
        if signal.episode_id != episode.episode_id or not set(signal.source_event_ids + signal.resolution_event_ids) <= known:
            raise ValueError("Signal references unknown source evidence")


def sliding_windows(episode: Episode, source="baseline") -> list[Candidate]:
    points = usable_boundaries(episode)
    result, seen = [], set()
    for start in points:
        ends = [p for p in points if 20000 <= p-start <= 60000]
        for target in (20000,30000,45000,60000):
            if not ends:
                break
            end = min(ends,key=lambda p:(abs(p-start-target),p))
            if (start,end) not in seen:
                seen.add((start,end))
                result.append(make_candidate(episode,start,end,source))
    return result


def semantic_candidates(episode: Episode, signals: list[StorySignal]) -> list[Candidate]:
    validate_signals(episode,signals)
    points = usable_boundaries(episode)
    events = {e.event_id:e for e in episode.events}
    result = []
    starters = [s for s in signals if s.kind in ("hook","conflict","reveal","reversal","open_question")]
    endings = [s for s in signals if s.kind in ("cliffhanger","open_question","resolution","reveal","reversal")]
    for opening in starters:
        first = min(events[e].start_ms for e in opening.source_event_ids)
        starts = [p for p in points if 0 <= first-p <= 3000]
        if not starts:
            continue
        start = max(starts)
        proposals = [(min(events[e].start_ms for e in closing.source_event_ids) if closing.kind == "resolution"
                      else max(events[e].end_ms for e in closing.source_event_ids), closing.signal_id)
                     for closing in endings]
        proposals += [(events[e].start_ms,opening.signal_id) for e in opening.resolution_event_ids]
        priority = {"high":3,"medium":2,"low":1}[opening.priority]
        resolution_limit = min((events[e].start_ms for e in opening.resolution_event_ids),default=episode.duration_ms)
        for end_hint, signal_id in proposals:
            options = [p for p in points if abs(p-end_hint) <= 1500 and 20000 <= p-start <= 60000
                       and p <= resolution_limit]
            if options:
                end = min(options,key=lambda p:(abs(p-end_hint),p))
                result.append(make_candidate(episode,start,end,"semantic",[opening.signal_id,signal_id],priority))
    return result


def stable_key(c):
    return (-c.discovery_priority,c.start_ms,c.end_ms,c.candidate_id)


def distinct_from(c, chosen, threshold):
    return all(interval_iou(c.start_ms,c.end_ms,p.start_ms,p.end_ms) < threshold for p in chosen)


def prune(candidates: list[Candidate], cap: int, overlap=0.8) -> list[Candidate]:
    """Semantic priority, a distinct opening, and a wildcard; no creative score."""
    if cap <= 0:
        return []
    pool = sorted(candidates,key=stable_key)
    selected = []
    semantic = [c for c in pool if c.source == "semantic"]
    if semantic:
        selected.append(semantic[0])
    if selected and len(selected) < cap:
        alternatives = [c for c in semantic if c.start_ms != selected[0].start_ms and distinct_from(c,selected,overlap)]
        if alternatives:
            selected.append(min(alternatives,key=lambda c:(-abs(c.start_ms-selected[0].start_ms),*stable_key(c))))
    wildcards = [c for c in pool if c.source in ("wildcard","baseline") and distinct_from(c,selected,overlap)]
    if wildcards and len(selected) < cap:
        selected.append(min(wildcards,key=lambda c:(-c.statistics['event_types'], -c.statistics['narrative_events'],c.start_ms,c.end_ms)))
    while len(selected) < cap:
        choices = [c for c in pool if c not in selected and distinct_from(c,selected,overlap)]
        if not choices:
            break
        # Maximize opening separation; priority and stable timestamps break ties.
        selected.append(min(choices,key=lambda c:(-min((abs(c.start_ms-p.start_ms) for p in selected),default=0),*stable_key(c))))
    return selected


def select_baseline(candidates: list[Candidate], cap: int, overlap=0.8) -> list[Candidate]:
    """Fixed temporal-coverage selection; no semantic signals or statistics score."""
    pool = sorted(candidates,key=lambda c:(c.start_ms,abs(c.duration_ms-30000),c.end_ms))
    selected = []
    while pool and len(selected)<cap:
        chosen = min(pool,key=lambda c:(-min((abs(c.start_ms-p.start_ms) for p in selected),default=0),
                                        c.start_ms,abs(c.duration_ms-30000),c.end_ms))
        selected.append(chosen)
        pool=[c for c in pool if distinct_from(c,selected,overlap)]
    return selected


def assemble(episode: Episode, signals: list[StorySignal], raw_cap=12, retained_cap=5, review_cap=3, overlap=0.8):
    if not 0 < review_cap <= retained_cap <= raw_cap:
        raise ValueError("Budgets must satisfy 0 < review <= retained <= raw")
    if not 0 < overlap <= 1:
        raise ValueError("Overlap threshold must be in (0,1]")
    # Validate even quarantined episodes so bad evidence is not silently accepted.
    validate_signals(episode,signals)
    baseline = sliding_windows(episode)
    semantic = semantic_candidates(episode,signals)
    wildcards = [c.model_copy(update={"source":"wildcard"}) for c in baseline]
    # Remove exact interval duplicates, preferring source-supported semantic proposals.
    unique = {}
    ledger = []
    def decision(c, status, reason, **extra):
        return {**c.model_dump(exclude={'selected_content'}), 'status':status,'reason':reason,**extra}
    for c in sorted(semantic+wildcards,key=stable_key):
        key=(c.start_ms,c.end_ms)
        if key in unique:
            ledger.append(decision(c,"duplicate","same_interval",duplicate_of=unique[key].candidate_id))
        else:
            unique[key]=c
    raw = prune(list(unique.values()),raw_cap,overlap=1.0)
    retained = prune(raw,retained_cap,overlap)
    review = prune(retained,review_cap,overlap)
    raw_ids={c.candidate_id for c in raw}
    kept={c.candidate_id for c in retained}
    reviewed={c.candidate_id for c in review}
    for c in unique.values():
        status = "queued_for_blind_review" if c.candidate_id in reviewed else "retained_reserve" if c.candidate_id in kept else "not_reviewed"
        ledger.append(decision(c,status,"review_budget" if c.candidate_id in reviewed else
                                "retained_budget" if c.candidate_id in kept else
                                "retention_or_overlap" if c.candidate_id in raw_ids else "raw_budget"))
    return {"episode_id":episode.episode_id,"source_ready":episode.source_ready,
            "mode":"deterministic_unreviewed", "semantic_signal_count":len(signals),
            "enumerated":len(semantic)+len(wildcards),"raw":raw,"retained":retained,"review_queue":review,
            "baseline_queue":select_baseline(baseline,review_cap,overlap),"ledger":ledger}
