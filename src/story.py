"""Shared source-part analysis for the thin slice and batch worker."""
import json
from .analysis_planning import plan_analysis_parts,CombinedAnalysis
from .review_models import StoryAnalysis,CompactStoryAnalysis
from .candidates import validate_signals,StorySignal
from . import prompts


def validate_compact_result(result,anchors):
    """Normalize only blank optional resolution placeholders, never evidence.

    An empty resolution means unknown/not found in supplied source. Raw model
    responses remain unchanged in logs/cache; no factual ID is invented.
    """
    normalized=0
    for signal in result.signals:
        cleaned=[ref for ref in signal.resolution_event_ids if ref.strip()]
        normalized+=len(signal.resolution_event_ids)-len(cleaned)
        signal.resolution_event_ids=cleaned
        if not set(signal.source_event_ids+signal.resolution_event_ids)<=set(anchors):
            raise ValueError('Unknown source anchor in compact analysis')
    return normalized


def extract_story(request,episode,config,destination):
    plan=plan_analysis_parts(episode,config)
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'analysis_plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    signals=[];limitations=[]
    for index,part in enumerate(plan,1):
        if 'anchor_map' in part:
            anchors=part['anchor_map']
            def validate_compact(result):
                normalized=validate_compact_result(result,anchors)
                if normalized:limitations.append(f'Normalized {normalized} blank optional resolution placeholders; no resolving event was inferred.')
            result=request('analysis',f'story:{index}',prompts.STORY_LEAN,part['payload'],CompactStoryAnalysis,validate_compact)
            signals.extend(StorySignal(signal_id=f'{episode.episode_id}_P{index}_S{i}',episode_id=episode.episode_id,
                kind=s.kind,source_event_ids=[anchors[a] for a in s.source_event_ids],description=s.description,
                priority=s.priority,resolution_event_ids=[anchors[a] for a in s.resolution_event_ids],origin='model')
                for i,s in enumerate(result.signals,1))
            limitations.extend(result.limitations)
            continue
        supplied={row[0] for row in part['payload']['records']}
        def validate(result):
            if result.episode_id!=episode.episode_id or any(s.origin!='model' for s in result.signals):
                raise ValueError('Wrong analysis episode or origin')
            validate_signals(episode,result.signals)
            if any(not set(s.source_event_ids+s.resolution_event_ids)<=supplied for s in result.signals):
                raise ValueError('Signal cites events not supplied to its analysis part')
        result=request('analysis',f'story:{index}',prompts.STORY,part['payload'],StoryAnalysis,validate)
        signals.extend(s.model_copy(update={'signal_id':f'{episode.episode_id}_P{index}_{s.signal_id}'}) for s in result.signals)
        limitations.extend(result.limitations)
    if len(plan)>1:limitations.append('Resolutions outside a source part may be unknown; this is not global absence.')
    result=CombinedAnalysis(episode_id=episode.episode_id,signals=signals,limitations=limitations)
    (destination/'story_signals.json').write_text(result.model_dump_json(indent=2)+'\n')
    return result
