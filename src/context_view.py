"""Lossless wire compaction. Canonical source remains available for validation."""
from copy import deepcopy


def compact_context(payload):
    result=deepcopy(payload)
    columns=['id','type','start_ms','end_ms','speaker','text']
    for field in ('current_source','next_episode_opening'):
        source=result.get(field)
        if source:
            source['columns']=columns
            source['records']=[[e.get(k) for k in columns] for e in source.pop('events')]
    related=result.get('related_show_source')
    if related:
        related['source_episode_count']=len(related.pop('coverage_episode_ids',[]))
        related['ai_analyzed_episode_count']=len(related.pop('ai_analyzed_episode_ids',[]))
        related_columns=['episode_id',*columns]
        related['columns']=related_columns
        related['records']=[[e.get(k) for k in related_columns] for e in related['records']]
    return result
