"""Structured semantic responses; code still determines final eligibility."""
from typing import Literal
from pydantic import Field
from .models import Record
from .candidates import StorySignal

Answer = Literal['yes','no','uncertain']


class StoryAnalysis(Record):
    episode_id: str
    signals: list[StorySignal] = Field(max_length=16)
    limitations: list[str]


class CompactStorySignal(Record):
    kind: Literal['hook','conflict','escalation','reversal','reveal','open_question','resolution','cliffhanger','context_dependency']
    source_event_ids: list[str] = Field(min_length=1,max_length=8)
    description: str = Field(min_length=1)
    priority: Literal['high','medium','low']
    resolution_event_ids: list[str] = Field(max_length=8)


class CompactStoryAnalysis(Record):
    signals: list[CompactStorySignal] = Field(max_length=12)
    limitations: list[str]


class BlindReview(Record):
    clip_id: Literal['clip']
    clear_to_new_viewer: Answer
    hook_within_first_3s: Answer
    ending_resolved: Answer
    hook_score: int = Field(ge=0,le=10)
    cliffhanger_score: int = Field(ge=0,le=10)
    standalone_clarity_score: int = Field(ge=0,le=10)
    emotional_stakes_score: int = Field(ge=0,le=10)
    hook: str
    cliffhanger: str
    why_it_works: str
    hook_evidence_ids: list[str]
    cliffhanger_evidence_ids: list[str]
    uncertainty_reasons: list[str]
    cliffhanger_type: Literal['threat','reveal','decision','action','question','none','uncertain'] = 'uncertain'


class ContextReview(Record):
    candidate_id: str
    source_supported: Answer
    misleading_cut: Answer
    essential_context_missing: Answer
    ending_resolved_inside_clip: Answer
    boundary_issue: Literal['none','dialogue','action','uncertain']
    evidence_event_ids: list[str]
    reasons: list[str]
    hook_factually_supported: Answer = 'uncertain'
    cliffhanger_factually_supported: Answer = 'uncertain'
    evidence_quotes: list['EvidenceQuote'] = Field(default_factory=list)
    description_speakers_match_source: Answer = 'uncertain'


class EvidenceQuote(Record):
    event_id: str
    quote: str = Field(min_length=1)


class SourceBlindReview(BlindReview):
    threat_target_quote: str | None
    threat_target_evidence_id: str | None


ContextReview.model_rebuild()


class ShowContextReview(ContextReview):
    source_threat_target: str
    described_threat_target: str
    description_consequences_match_source: Answer
