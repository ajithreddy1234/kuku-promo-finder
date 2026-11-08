"""Offline ranking with explicit near ties; no ad-performance prediction."""
from copy import deepcopy
from .boundaries import interval_iou


def rank_candidates(rows, target=20, post_first=10, tolerance=0.5, duplicate_iou=0.8):
    records=deepcopy([r for r in rows if r.get('status') in ('eligible','ai_eligible')])
    hashes={r.get('config_hash') for r in records if r.get('config_hash')}
    if len(hashes)>1:
        raise ValueError('Do not pool different model/prompt/config runs into one ranking')
    records.sort(key=lambda r:(-r.get('score_raw',r['score']),r['episode_id'],r['start_ms']))
    unique=[]
    for row in records:
        if any(row['episode_id']==p['episode_id'] and interval_iou(row['start_ms'],row['end_ms'],p['start_ms'],p['end_ms'])>=duplicate_iou for p in unique):
            continue
        unique.append(row)
    groups=[]
    for row in unique:
        score=row.get('score_raw',row['score'])
        if not groups or groups[-1][0].get('score_raw',groups[-1][0]['score'])-score>tolerance:
            groups.append([])
        groups[-1].append(row)
    quality=[]
    for group_number,group in enumerate(groups,1):
        group.sort(key=lambda r:(-r.get('standalone_clarity_score',0),-r.get('hook_score',0),len(r.get('warnings',[])),
                                -r.get('duration_margin_ms',0),r['episode_id'],r['start_ms']))
        for row in group:
            row['priority_group']=group_number
            row['rank_note']='Near tie: clarity, then hook, fewer warnings, duration margin, then stable episode/time; not score precision.'
            quality.append(row)
    quality=quality[:target]
    for index,row in enumerate(quality,1):row['quality_rank']=index;row['rank']=index
    posting=[]
    counts={}
    remaining=list(quality)
    while remaining and len(posting)<post_first:
        chosen=min(remaining,key=lambda r:(r['priority_group'],counts.get(r['episode_id'],0),r['quality_rank']))
        posting.append(chosen);remaining.remove(chosen)
        counts[chosen['episode_id']]=counts.get(chosen['episode_id'],0)+1
        chosen['posting_rank']=len(posting)
        alternatives=[r for r in remaining if r['priority_group']==chosen['priority_group']]
        other=min(alternatives,key=lambda r:r['quality_rank']) if alternatives else None
        comparison=(f"Clearer standalone scene ({chosen.get('standalone_clarity_score')} vs {other.get('standalone_clarity_score')} for the next same-group alternative). "
                    if other and chosen.get('standalone_clarity_score',0)>other.get('standalone_clarity_score',0) else '')
        chosen['post_first_reason']='AI-reviewed within priority group '+str(chosen['priority_group'])+'; '+(
            'starts the posting shortlist.' if len(posting)==1 else
            'adds another episode to reduce repetition.' if counts[chosen['episode_id']]==1 else
            'retains a distinct, nonduplicate moment; no equally grouped new-episode alternative was preferred.')
        chosen['post_first_reason']=comparison+chosen['post_first_reason']
        rationale=chosen.get('why_it_works','').strip()
        if rationale:chosen['post_first_reason']=rationale+' '+chosen['post_first_reason']
        chosen['post_first_reason']+=f" Rubric: hook {chosen.get('hook_score','?')}, cliffhanger {chosen.get('cliffhanger_score','?')}, clarity {chosen.get('standalone_clarity_score','?')}."
    return quality,posting
