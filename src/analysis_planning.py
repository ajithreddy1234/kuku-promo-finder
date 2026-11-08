"""Lossless token-aware source chunks; original timelines never change."""
import json
from pydantic import Field
from .budget import BudgetPause, estimate_request
from .provider import strict_schema
from .review_models import StoryAnalysis,CompactStoryAnalysis
from .models import Record
from .candidates import StorySignal
from . import prompts


class CombinedAnalysis(Record):
    episode_id: str
    signals: list[StorySignal] = Field(max_length=256)
    limitations: list[str]


def plan_analysis_parts(episode,config):
    role=config['models']['analysis']
    limit=(role.get('context_tokens',8192) if role['provider']=='ollama' else
           config['quota']['models'][role['model']]['tokens_per_minute'])
    lean=config['inference'].get('analysis_view')=='compact_anchors_v2'
    output=role.get('max_output_tokens',config['inference']['max_output_tokens'])
    columns=['id','type','start_ms','end_ms','speaker','text']
    events=episode.temporal_events()
    def part(start,end):
        selected=[e for e in events if e.start_ms<end and e.end_ms>start]
        if lean:
            speakers=list(dict.fromkeys(e.speaker_raw for e in selected if e.speaker_raw))
            anchors={f'A{i:03d}':e.event_id for i,e in enumerate(selected,1)}
            payload={'episode_id':episode.episode_id,'columns':['anchor','type','speaker','text'],
                'speakers':speakers,'records':[[a,e.event_type,speakers.index(e.speaker_raw) if e.speaker_raw else None,e.text]
                    for a,e in zip(anchors,selected)],'entire_episode':start==0 and end==episode.duration_ms}
            return payload,anchors
        return {'episode_id':episode.episode_id,'columns':columns,
                'records':[[e.event_id,e.event_type,e.start_ms,e.end_ms,e.speaker_raw,e.text] for e in selected],
                'analysis_window_ms':[start,end],'entire_episode':start==0 and end==episode.duration_ms,
                'scope_note':'All records intersecting this analysis window are supplied intact. Resolutions outside supplied records are unknown.'},None
    def split(start,end,depth=0):
        payload,anchors=part(start,end)
        messages=[{'role':'system','content':prompts.STORY_LEAN if lean else prompts.STORY},
                  {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
        estimate=estimate_request(messages,strict_schema(CompactStoryAnalysis if lean else StoryAnalysis),output)
        if estimate['reserved_tokens']<=limit:
            return [{'payload':payload,'estimate':estimate,**({'anchor_map':anchors} if lean else {})}]
        if end-start<20000 or depth>=4:
            raise BudgetPause('Source record/density exceeds automatic chunk capacity; no data was dropped')
        middle=(start+end)//2;overlap=min(15000,(end-start)//8)
        return split(start,middle+overlap,depth+1)+split(middle-overlap,end,depth+1)
    return split(0,episode.duration_ms)
