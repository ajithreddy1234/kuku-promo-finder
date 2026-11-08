"""Editor-facing evidence, explicit AI status and reproducibility metadata."""
from copy import deepcopy
import csv
import json
from io import StringIO
import uuid
from .timeline import format_time
from .ranking import rank_candidates
from .editor_policy import apply_editor_policy


def enrich_review(row,candidate,episode,metadata):
    result=deepcopy(row)
    result['legacy_status']=row.get('legacy_status',row['status'])
    status=row['status']
    context=row.get('context',{})
    if status in ('eligible','ai_eligible'):
        status='ai_eligible' if context.get('hook_factually_supported')=='yes' and context.get('cliffhanger_factually_supported')=='yes' else 'editor_review'
    result['status']=status
    result['approval_status']='not_human_reviewed'
    result['score_raw']=row.get('score_raw',row['score'])
    result['score']=round(result['score_raw'],1)
    blind=row.get('blind',{})
    for field in ('hook_score','cliffhanger_score','standalone_clarity_score','emotional_stakes_score',
                  'clear_to_new_viewer','hook_within_first_3s','ending_resolved'):
        result[field]=blind.get(field,'not_recorded')
    for field in ('misleading_cut','essential_context_missing','source_supported'):
        result[field]=context.get(field,'not_recorded')
    result['context_review_status']='complete' if context else 'pending'
    result['cliffhanger_type']=blind.get('cliffhanger_type','not_assessed')
    result['threat_target_quote']=blind.get('threat_target_quote')
    result['source_threat_target']=context.get('source_threat_target')
    result['description_consequences_match_source']=context.get('description_consequences_match_source')
    result['config_hash']=metadata.get('config_hash','not_recorded')
    result['input_hash']=metadata.get('input_hash','not_recorded')
    result['run_complete']=metadata.get('complete',False)
    for role in ('blind','context'):
        model=(result.get('judged_by') or {}).get(role) or metadata.get('models',{}).get(role,{})
        result[f'model_{role}']=model.get('model','not_recorded')
        result[f'prompt_{role}']=model.get('prompt_version','not_recorded')
    source={e.event_id:e for e in episode.events}
    selected={e.event_id:e for e in candidate.selected_content}
    def full_evidence(ids):
        return sorted((source[i] for i in ids if i in source and i in selected
                       and selected[i].content_status=='full'),key=lambda e:(e.start_ms,e.source_order))
    hooks=full_evidence(row.get('hook_evidence_ids',[]))
    early=[e for e in hooks if e.start_ms-candidate.start_ms<3000]
    spoken=[e for e in early if e.event_type=='dialogue']
    hook=(spoken or early or hooks or [None])[0]
    result['hook_quote']=hook.text if hook and hook.event_type=='dialogue' else ''
    result['hook_source_text']=hook.text if hook else ''
    result['hook_time']=format_time(hook.start_ms) if hook else ''
    result['hook_end_time']=format_time(hook.end_ms) if hook else ''
    result['hook_time_in_clip_ms']=hook.start_ms-candidate.start_ms if hook else None
    result['hook_source_type']=hook.event_type if hook else 'partial_or_unlocalized'
    endings=full_evidence(row.get('cliffhanger_evidence_ids',[]))
    spoken_endings=[e for e in endings if e.event_type=='dialogue']
    result['cliffhanger_quote']=spoken_endings[-1].text if spoken_endings else ''
    result['cliffhanger_quote_speaker']=spoken_endings[-1].speaker_raw if spoken_endings else ''
    result['cliffhanger_quote_event_id']=spoken_endings[-1].event_id if spoken_endings else ''
    result['description_speakers_match_source']=context.get('description_speakers_match_source','not_assessed')
    full_dialogue=[source[e.event_id] for e in candidate.selected_content if e.content_status=='full' and e.event_type=='dialogue']
    last=max(full_dialogue,key=lambda e:(e.end_ms,e.source_order),default=None)
    result['final_line']=last.text if last else ''
    result['final_line_time']=format_time(last.start_ms) if last else ''
    result['final_line_end_time']=format_time(last.end_ms) if last else ''
    result['final_line_speaker']=last.speaker_raw if last else ''
    result['seconds_after_final_line']=(candidate.end_ms-last.end_ms)/1000 if last else None
    tail=[e.model_dump() for e in candidate.selected_content if last and
          e.source_end_ms>last.end_ms and e.clip_end_ms>last.end_ms-candidate.start_ms]
    result['content_after_final_line']=tail
    margin=min(candidate.duration_ms-20000,60000-candidate.duration_ms)
    result['duration_margin_ms']=margin
    warnings=[w for w in result.get('warnings',[]) if not w.lower().startswith('none')]
    if margin<metadata.get('duration_margin_warning_ms',500):warnings.append('duration_near_limit')
    if not hooks:warnings.append('hook_evidence_not_fully_localized')
    result['warnings']=sorted(set(warnings))
    result['eligibility_policy_version']=metadata.get('eligibility_policy_version',metadata.get('policy_version','not_recorded'))
    if metadata.get('editor_policy'):
        result=apply_editor_policy(result)
    return result


CSV_FIELDS=['rank','priority_group','posting_rank','post_first_reason','candidate_id','episode_id','start','end','duration_seconds',
    'duration_margin_ms','score','status','approval_status','human_reviewed','hook_quote','hook_time',
    'hook_end_time','hook_source_type','hook_source_text','hook','cliffhanger_quote','cliffhanger_quote_speaker',
    'cliffhanger_quote_event_id','description_speakers_match_source','final_line','final_line_time','final_line_end_time',
    'final_line_speaker','seconds_after_final_line','content_after_final_line',
    'cliffhanger','cliffhanger_type','why_it_works','hook_score','cliffhanger_score','standalone_clarity_score',
    'emotional_stakes_score','clear_to_new_viewer','hook_within_first_3s','ending_resolved',
    'context_review_status','misleading_cut','essential_context_missing','source_supported','threat_target_quote',
    'source_threat_target','description_consequences_match_source','config_hash','judge_config_hash',
    'eligibility_policy_version','derived_from_run',
    'editor_tier','editor_review_reasons','editor_policy_version','judged_by',
    'model_blind','model_context','prompt_blind','prompt_context','run_complete','warnings','rank_note']


def write_review_exports(destination,rows,target=20,post_first=10,tolerance=0.5):
    def atomic(name,text):
        path=destination/name;temporary=path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        temporary.write_text(text);temporary.replace(path)
    copied=deepcopy(rows)
    # Historical rows without enrichment retain their old interpretation only in raw archives.
    for row in copied:
        if row['status']=='eligible':row['status']='ai_eligible'
        row.setdefault('score_raw',row['score'])
        row['score']=round(row['score_raw'],1)
        if row['status']!='ai_eligible':
            for field in ('rank','quality_rank','posting_rank','priority_group','post_first_reason','rank_note'):
                row.pop(field,None)
    ranked,posting=rank_candidates(copied,target,post_first,tolerance)
    by_id={r['candidate_id']:r for r in ranked}
    reviews=[by_id.get(r['candidate_id'],r) for r in copied]
    atomic('candidate_reviews.json',json.dumps(reviews,indent=2)+'\n')
    atomic('promos.json',json.dumps(ranked,indent=2)+'\n')
    atomic('post_first.json',json.dumps(posting,indent=2)+'\n')
    for filename,values in [('candidate_reviews.csv',reviews),('promos.csv',ranked)]:
        with StringIO(newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=CSV_FIELDS);writer.writeheader()
            for row in values:
                writer.writerow({**{k:row.get(k,'') for k in CSV_FIELDS},
                    'start':format_time(row['start_ms']),'end':format_time(row['end_ms']),
                    'content_after_final_line':json.dumps(row.get('content_after_final_line',[]),ensure_ascii=False),
                    'judged_by':json.dumps(row.get('judged_by',{}),ensure_ascii=False),
                    'editor_review_reasons':'; '.join(row.get('editor_review_reasons',[])),
                    'duration_seconds':row['duration_ms']/1000,'warnings':'; '.join(row.get('warnings',[]))})
            atomic(filename,handle.getvalue())
    return ranked
