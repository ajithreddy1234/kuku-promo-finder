"""Reference integrity and exposure gates. Human labels are never invented."""
from datetime import datetime
import hashlib
import json
from typing import Literal
from pydantic import Field

from .models import Episode, Record


class ReferenceSelection(Record):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    why_it_works: str


class ReferenceEntry(Record):
    episode_id: str
    source_sha256: str
    decision: Literal["pending", "selected", "none"] = "pending"
    selections: list[ReferenceSelection] = Field(default_factory=list)
    reason: str = ""


class ReferenceBundle(Record):
    input_hash: str
    evaluator: str = ""
    independent_before_system_outputs: bool = False
    recorded_at: datetime | None = None
    entries: list[ReferenceEntry]


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def split_sets(config: dict) -> dict[str,set[int]]:
    result = {role:set(config['evaluation'][f'{role}_episode_numbers'])
              for role in ('development','tuning','held_out')}
    for role in result:
        values=config['evaluation'][f'{role}_episode_numbers']
        if len(values)!=len(result[role]) or any(type(n) is not int or n<=0 for n in values):
            raise ValueError('Invalid episode split')
    if any(result[a]&result[b] for a,b in [('development','tuning'),('development','held_out'),('tuning','held_out')]):
        raise ValueError('Evaluation splits must not overlap')
    return result


def reference_template(episodes: list[Episode], input_hash: str, config: dict) -> ReferenceBundle:
    held=split_sets(config)['held_out']
    if not held:raise ValueError('No evaluation split configured. Choose reference episodes before preparing an independent benchmark.')
    selected=[e for e in episodes if e.episode_number in held]
    if {e.episode_number for e in selected} != held:
        raise ValueError('Reference episodes missing from input pack')
    return ReferenceBundle(input_hash=input_hash,
        entries=[ReferenceEntry(episode_id=e.episode_id,source_sha256=e.source_sha256) for e in selected])


def validate_references(bundle: ReferenceBundle, episodes: list[Episode], input_hash: str, config: dict, required_numbers=None):
    expected=reference_template(episodes,input_hash,config)
    if bundle.input_hash!=input_hash:
        raise ValueError('Reference input hash mismatch')
    if not bundle.evaluator.strip() or not bundle.independent_before_system_outputs or bundle.recorded_at is None:
        raise ValueError('Human evaluator, independence attestation and recorded_at are required')
    if bundle.recorded_at.tzinfo is None:
        raise ValueError('Reference timestamp must include timezone')
    if len(bundle.entries)!=len(expected.entries) or {e.episode_id for e in bundle.entries}!={e.episode_id for e in expected.entries}:
        raise ValueError('Reference episode coverage mismatch')
    originals={e.episode_id:e for e in episodes}
    required=set(required_numbers if required_numbers is not None else
                 config['evaluation'].get('primary_reference_episode_numbers',split_sets(config)['held_out']))
    if not required<=split_sets(config)['held_out']:raise ValueError('Required references outside held-out set')
    for entry in bundle.entries:
        ep=originals[entry.episode_id]
        if entry.source_sha256!=ep.source_sha256:
            raise ValueError('Reference source changed')
        if entry.decision=='pending':
            if ep.episode_number in required:raise ValueError('Required human reference labels still pending')
            continue
        if entry.decision=='none' and (entry.selections or not entry.reason.strip()):
            raise ValueError('No-selection decision needs a reason and no clips')
        if entry.decision=='selected' and not entry.selections:
            raise ValueError('Selected decision needs a clip')
        seen=set()
        for clip in entry.selections:
            if not 20000<=clip.end_ms-clip.start_ms<=60000 or clip.end_ms>ep.duration_ms or not clip.why_it_works.strip():
                raise ValueError('Invalid human reference interval or reason')
            if (clip.start_ms,clip.end_ms) in seen:
                raise ValueError('Duplicate human reference interval')
            seen.add((clip.start_ms,clip.end_ms))


def require_held_out_access(episode: Episode, episodes: list[Episode], input_hash: str,
                            config: dict, bundle: ReferenceBundle | None=None, frozen_hash: str | None=None):
    if episode.episode_number not in split_sets(config)['held_out']:
        return
    if bundle is None:
        raise ValueError('Held-out outputs locked: human references pending')
    required=set(config['evaluation'].get('primary_reference_episode_numbers',[]))|{episode.episode_number}
    validate_references(bundle,episodes,input_hash,config,required)
    if frozen_hash!=config_hash(config):
        raise ValueError('Held-out outputs locked: frozen configuration mismatch')
