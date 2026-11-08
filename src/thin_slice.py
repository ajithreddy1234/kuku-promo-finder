"""Live development slice: real signals -> both reviews -> policy -> exports."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from .candidates import assemble, validate_signals
from .evaluation import config_hash, split_sets, require_held_out_access
from .provider import JsonProvider, ProviderFailure
from .review_models import StoryAnalysis, BlindReview, ContextReview
from . import prompts
from .timeline import format_time
from .exports import enrich_review, write_review_exports
from .analysis_planning import plan_analysis_parts, CombinedAnalysis


def episode_package(episode, events=None):
    return {'episode_id':episode.episode_id,'events':[
        {'id':e.event_id,'type':e.event_type,'start_ms':e.start_ms,'end_ms':e.end_ms,
         'speaker':e.speaker_raw,'text':e.text}
        for e in (events if events is not None else episode.temporal_events())]}


def analysis_package(episode,view='full'):
    package=episode_package(episode)
    if view=='full':return package
    if view!='compact_rows_v1':raise ValueError('Unknown analysis view')
    columns=['id','type','start_ms','end_ms','speaker','text']
    return {'episode_id':episode.episode_id,'columns':columns,
            'records':[[event[k] for k in columns] for event in package['events']],
            'compaction':'lossless: all event text, source IDs, channels and timing preserved'}


def blind_package(candidate):
    mapping={}
    speakers={}
    rows=[]
    for index,event in enumerate(candidate.selected_content,1):
        local=f'R{index:03d}'
        mapping[local]=event.event_id
        speaker=None
        if event.speaker_raw:
            base=re.sub(r'\s*\([^)]*\)','',event.speaker_raw).strip().upper()
            speaker=speakers.setdefault(base,f'S{len(speakers)+1}')
        rows.append({'id':local,'type':event.event_type,'speaker':speaker,
                     'start_ms':event.clip_start_ms,'end_ms':event.clip_end_ms,
                     'content_status':event.content_status,'text':event.text})
    return {'clip_id':'clip','duration_ms':candidate.duration_ms,'events':rows},mapping


def context_package(candidate,episode,episodes,config,blind,mapping,show_index=None):
    current=[e for e in episode.temporal_events() if
             e.end_ms>max(0,candidate.start_ms-15000) and e.start_ms<candidate.end_ms+15000]
    future=None
    warnings=[]
    if episode.duration_ms-candidate.end_ms<=10000:
        adjacent=next((e for e in episodes if e.episode_number==episode.episode_number+1),None)
        if adjacent:
            from .coverage import excluded_numbers
            if adjacent.episode_number in excluded_numbers(config) or not adjacent.source_ready:
                warnings.append('next_episode_context_withheld_heldout')
            else:
                future=episode_package(adjacent,[e for e in adjacent.temporal_events() if e.start_ms<12000])
    package={'candidate_id':candidate.candidate_id,'start_ms':candidate.start_ms,'end_ms':candidate.end_ms,
             'selected_event_ids':candidate.selected_event_ids,
             'partially_selected_ids':[e.event_id for e in candidate.selected_content if e.content_status!='full'],
             'current_source':episode_package(episode,current),'next_episode_opening':future,
             'hypotheses':{'hook':blind.hook,'cliffhanger':blind.cliffhanger,
                           'hook_evidence':[mapping[i] for i in blind.hook_evidence_ids],
                           'cliffhanger_evidence':[mapping[i] for i in blind.cliffhanger_evidence_ids]},
             'cut_safety':{kind:[e.event_id for e in episode.events if e.event_type==kind and
                       any(e.start_ms<t<e.end_ms for t in (candidate.start_ms,candidate.end_ms))]
                       for kind in ('dialogue','action')},
             'warnings':candidate.warnings+warnings}
    known={e.event_id for e in current}|set(candidate.selected_event_ids)
    if future:known.update(e['id'] for e in future['events'])
    if show_index is not None:
        from .show_context import retrieve
        package['related_show_source']=retrieve(show_index,candidate,blind)
        known.update(e['id'] for e in package['related_show_source']['records'])
        package['hypotheses']['threat_target_quote']=getattr(blind,'threat_target_quote',None)
        package['hypotheses']['threat_target_source_id']=mapping.get(getattr(blind,'threat_target_evidence_id',None))
    return package,known,warnings


def review_decision(candidate,blind,context,mapping,config,extra_warnings):
    extra_warnings=list(extra_warnings)
    evidence=blind.hook_evidence_ids+blind.cliffhanger_evidence_ids
    if not set(evidence)<=set(mapping):raise ValueError('Unknown blind evidence')
    thresholds=config['eligibility']
    reject=(blind.cliffhanger_type=='none' or blind.ending_resolved=='yes' or blind.clear_to_new_viewer=='no'
            or context.source_supported=='no' or context.misleading_cut=='yes'
            or context.essential_context_missing=='yes' or context.ending_resolved_inside_clip=='yes'
            or context.boundary_issue in ('dialogue','action')
            or context.hook_factually_supported=='no' or context.cliffhanger_factually_supported=='no'
            or context.description_speakers_match_source=='no'
            or blind.hook_score<thresholds['minimum_hook']
            or blind.cliffhanger_score<thresholds['minimum_cliffhanger']
            or blind.standalone_clarity_score<thresholds['minimum_clarity'])
    uncertain=(blind.cliffhanger_type=='uncertain' or blind.clear_to_new_viewer=='uncertain' or blind.hook_within_first_3s=='uncertain'
               or blind.ending_resolved=='uncertain' or context.source_supported=='uncertain'
               or context.misleading_cut=='uncertain' or context.essential_context_missing=='uncertain'
               or context.ending_resolved_inside_clip=='uncertain' or context.boundary_issue=='uncertain')
    uncertain=uncertain or context.hook_factually_supported=='uncertain' or context.cliffhanger_factually_supported=='uncertain'
    uncertain=uncertain or context.description_speakers_match_source=='uncertain'
    if blind.hook_within_first_3s=='no':reject=True
    # Check rather than trust a claim that a hook arrives before 3s.
    timed={f'R{i:03d}':e for i,e in enumerate(candidate.selected_content,1)}
    if not blind.hook_evidence_ids or not blind.cliffhanger_evidence_ids:
        uncertain=True
    if blind.hook_evidence_ids and min(timed[i].clip_start_ms for i in blind.hook_evidence_ids)>=3000:
        reject=True
    if blind.hook_evidence_ids and not any(timed[i].content_status=='full' and timed[i].clip_start_ms<3000 for i in blind.hook_evidence_ids):
        uncertain=True
    status='rejected' if reject else 'editor_review' if uncertain else 'eligible'
    if hasattr(context,'description_consequences_match_source'):
        if context.description_consequences_match_source=='no':status='rejected'
        elif context.description_consequences_match_source=='uncertain' and status=='eligible':status='editor_review'
    target_quote=getattr(blind,'threat_target_quote',None)
    if target_quote:
        relations=re.findall(r'\b(husband|wife|son|daughter|brother|sister|father|mother|child|children)\b',target_quote.lower())
        target_name=getattr(context,'source_threat_target','').split('(')[0].strip()
        name_supported=bool(target_name and target_name.lower() in blind.cliffhanger.lower()
                            and any(target_name in (e.text or '') for e in candidate.selected_content))
        if relations and not any(re.search(r'\b'+word+r'\b',blind.cliffhanger.lower()) for word in relations) and not name_supported:
            extra_warnings.append('threat_target_paraphrase_needs_editor_confirmation')
            if status=='eligible':status='editor_review'
    score=round(.30*blind.hook_score+.30*blind.cliffhanger_score+.25*blind.standalone_clarity_score+.15*blind.emotional_stakes_score,2)
    return {'candidate_id':candidate.candidate_id,'candidate_hash':candidate.candidate_hash,
            'candidate_version':candidate.candidate_version,'episode_id':candidate.episode_id,
            'start_ms':candidate.start_ms,'end_ms':candidate.end_ms,'duration_ms':candidate.duration_ms,
            'status':status,'score':score,'hook':blind.hook,'cliffhanger':blind.cliffhanger,
            'why_it_works':blind.why_it_works,'hook_evidence_ids':[mapping[i] for i in blind.hook_evidence_ids],
            'cliffhanger_evidence_ids':[mapping[i] for i in blind.cliffhanger_evidence_ids],
            'warnings':sorted(set(candidate.warnings+extra_warnings+blind.uncertainty_reasons)),
            'human_reviewed':False,'blind':blind.model_dump(),'context':context.model_dump()}


def write_exports(destination,rows):
    return write_review_exports(destination,rows)


def save_progress(destination,identity,rows,start,complete,stage,error_type=None):
    def count_jsonl(name):
        path=destination/name
        return len(path.read_text().splitlines()) if path.exists() else 0
    signals=destination/'story_signals.json'
    usage_path=destination/'usage.jsonl'
    usage=[json.loads(x) for x in usage_path.read_text().splitlines()] if usage_path.exists() else []
    calls=[x for x in usage if not x.get('cache_hit')]
    summary={**identity,'complete':complete,'stage':stage,'error_type':error_type,
             'signals':len(json.loads(signals.read_text())['signals']) if signals.exists() else 0,
             'raw':count_jsonl('raw.jsonl'),'retained':count_jsonl('retained.jsonl'),
             'queued':count_jsonl('review_queue.jsonl'),'reviewed':len(rows),
             'pending':max(0,count_jsonl('review_queue.jsonl')-len(rows)),
             'eligible':sum(r['status'] in ('eligible','ai_eligible') for r in rows),
             'editor_review':sum(r['status']=='editor_review' for r in rows),
             'rejected':sum(r['status']=='rejected' for r in rows),'human_reviewed':False,
             'api_attempts':sum(x.get('api_calls',0) for x in calls),
             'cache_hits':sum(x.get('cache_hit',False) for x in usage),
             'tokens_reported':{k:sum(x.get(k) or 0 for x in calls) for k in ('prompt_tokens','completion_tokens','total_tokens')},
             'attempts_missing_token_usage':sum(x.get('total_tokens') is None for x in calls),
             'seconds_this_invocation':round(time.perf_counter()-start,2),
             'overall_assignment_elapsed':'unknown; assignment start time was not supplied',
             'scope':'development only; partial eligible counts are not a finalized ranked shortlist'}
    (destination/'thin_slice_report.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


def run_thin_slice(root,episodes,input_hash,config,destination,episode_number=1,provider=None):
    start=time.perf_counter()
    target=next((e for e in episodes if e.episode_number==episode_number),None)
    if target is None:raise ValueError('Development episode not found')
    require_held_out_access(target,episodes,input_hash,config)
    allowed=split_sets(config)['development']|split_sets(config)['tuning']
    if not allowed:allowed={e.episode_number for e in episodes if e.episode_number not in split_sets(config)['held_out']}
    if episode_number not in allowed:raise ValueError('Thin slice is restricted to development/tuning episodes')
    if not target.source_ready:raise ValueError('Episode timing requires source review')
    destination.mkdir(parents=True,exist_ok=True)
    identity={'input_hash':input_hash,'config_hash':config_hash(config),'episode_id':target.episode_id}
    manifest=destination/'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text())!=identity:
        raise ValueError('Resume input/config differs from run')
    manifest.write_text(json.dumps(identity,indent=2)+'\n')
    (destination/'effective_config.json').write_text(json.dumps(config,indent=2)+'\n')
    api=provider or JsonProvider(root,config,destination)
    rows=[]
    def request(*args):
        try:
            return api.request(*args)
        except Exception as exc:
            write_exports(destination,rows)
            save_progress(destination,identity,rows,start,False,args[1],type(exc).__name__)
            raise
    from .story import extract_story
    analysis=extract_story(request,target,config,destination)
    assembly=assemble(target,analysis.signals,config['candidates']['raw_per_episode'],
        config['candidates']['assembled_per_episode'],config['candidates']['blind_reviewed_per_episode'],
        config['candidates']['near_duplicate_iou'])
    for key in ('raw','retained','review_queue'):
        (destination/f'{key}.jsonl').write_text(''.join(c.model_dump_json()+'\n' for c in assembly[key]))
    (destination/'candidate_decisions.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in assembly['ledger']))
    for candidate in assembly['review_queue']:
        package,mapping=blind_package(candidate)
        def check_blind(value):
            if not set(value.hook_evidence_ids+value.cliffhanger_evidence_ids)<=set(mapping):
                raise ValueError('Unknown blind evidence')
        blind=request('blind',f'blind:{candidate.candidate_hash}',prompts.BLIND,package,BlindReview,check_blind)
        context_payload,known,warnings=context_package(candidate,target,episodes,config,blind,mapping)
        def check_context(value):
            if value.candidate_id!=candidate.candidate_id or not set(value.evidence_event_ids)<=known:
                raise ValueError('Wrong contextual identity or evidence')
            if value.source_supported=='yes' and not value.evidence_event_ids:
                raise ValueError('Context support needs evidence')
            source_texts={e['id']:e['text'] for e in context_payload['current_source']['events']}
            if context_payload['next_episode_opening']:
                source_texts.update({e['id']:e['text'] for e in context_payload['next_episode_opening']['events']})
            for evidence in value.evidence_quotes:
                if evidence.event_id not in source_texts or evidence.quote not in source_texts[evidence.event_id]:
                    raise ValueError('Evidence quote not in referenced source')
            if value.hook_factually_supported=='yes' and value.cliffhanger_factually_supported=='yes' and not value.evidence_quotes:
                raise ValueError('Factual support requires exact source quotes')
        context=request('context',f'context:{candidate.candidate_hash}',prompts.CONTEXT,
                            context_payload,ContextReview,check_context)
        decision=review_decision(candidate,blind,context,mapping,config,warnings)
        rows.append(enrich_review(decision,candidate,target,{**identity,'models':config['models'],
                            'duration_margin_warning_ms':config['output'].get('duration_margin_warning_ms',500)}))
        write_exports(destination,rows) # useful partial outputs survive later failures
        save_progress(destination,identity,rows,start,False,'reviewing')
    for row in rows:row['run_complete']=True
    ranked=write_exports(destination,rows)
    return save_progress(destination,identity,rows,start,True,'complete')
