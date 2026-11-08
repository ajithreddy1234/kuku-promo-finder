"""Source contracts. No semantic model output is treated as timing authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Issue(Record):
    severity: Literal["warning", "error"]
    code: str
    source_file: str
    source_line: int | None = None
    message: str


class Event(Record):
    event_id: str
    episode_id: str
    scene_id: str | None = None
    event_type: Literal["dialogue", "action", "camera", "sound", "other"]
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    speaker_raw: str | None = None
    text: str = Field(min_length=1)
    source_file: str
    source_line_start: int = Field(ge=1)
    source_line_end: int = Field(ge=1)
    source_order: int = Field(ge=0)
    timing_status: Literal["explicit", "derived", "uncertain"] = "explicit"

    @model_validator(mode="after")
    def valid_interval(self):
        if not self.text.strip():
            raise ValueError("Event content must not be blank")
        if self.end_ms <= self.start_ms:
            raise ValueError("End must be after start")
        if self.source_line_end < self.source_line_start:
            raise ValueError("Invalid source location")
        return self


class Scene(Record):
    scene_id: str
    heading: str | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)


class Episode(Record):
    episode_id: str
    episode_number: int = Field(gt=0)
    source_file: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, str] = Field(default_factory=dict)
    scenes: list[Scene] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def duration_ms(self):
        return max([0] + [e.end_ms for e in self.events] +
                   [s.end_ms for s in self.scenes if s.end_ms is not None])

    def temporal_events(self):
        """Authority for playback/reconstruction; source order breaks equal-time ties."""
        return sorted(self.events, key=lambda e: (e.start_ms, e.source_order))

    def original_events(self):
        """Source provenance order, independent of JSON list ordering."""
        return sorted(self.events, key=lambda e: e.source_order)

    @property
    def source_ready(self) -> bool:
        """Conservative timing gate, not a claim of footage or editorial verification."""
        return (bool(self.events) and not any(i.severity == "error" for i in self.issues)
                and all(e.timing_status == "explicit" for e in self.events))
