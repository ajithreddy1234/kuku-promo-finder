"""Budgeted subset orchestration, global reviews and explicit all-pack coverage."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path

from .models import Episode
from .candidates import assemble,sliding_windows,select_baseline,Candidate
from .evaluation import config_hash,split_sets,require_held_out_access
from .provider import JsonProvider,ProviderFailure,RequestExhausted
from .budget import BudgetPause
from .story import extract_story
from .analysis_planning import CombinedAnalysis
from .thin_slice import blind_package,context_package,review_decision
from .review_models import BlindReview,ContextReview
from .exports import enrich_review,write_review_exports
from . import prompts


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def validate_context(value,candidate,payload,known):
    if value.candidate_id!=candidate.candidate_id or not set(value.evidence_event_ids)<=known:
        raise ValueError('Wrong context identity/evidence')
    texts={e['id']:e['text'] for e in payload['current_source']['events']}
    if payload['next_episode_opening']:texts.update({e['id']:e['text'] for e in payload['next_episode_opening']['events']})
    texts.update({e['id']:e['text'] for e in payload.get('related_show_source',{}).get('records',[])})
    for q in value.evidence_quotes:
        if q.event_id not in texts or q.quote not in texts[q.event_id]:raise ValueError('Unsupported quote')
    if value.source_supported=='yes' and (not value.evidence_event_ids or not value.evidence_quotes):
        raise ValueError('Missing context support')


def blind_screen(blind,config):
    minimum=config['eligibility']
    if (blind.cliffhanger_type=='none' or blind.ending_resolved=='yes' or blind.clear_to_new_viewer=='no' or blind.hook_within_first_3s=='no'
        or blind.hook_score<minimum['minimum_hook'] or blind.cliffhanger_score<minimum['minimum_cliffhanger']
        or blind.standalone_clarity_score<minimum['minimum_clarity']):return 'blind_rejected'
    if blind.cliffhanger_type=='uncertain' or 'uncertain' in (blind.ending_resolved,blind.clear_to_new_viewer,blind.hook_within_first_3s):return 'editor_review'
    if hasattr(blind,'threat_target_quote') and blind.cliffhanger_type=='threat' and not blind.threat_target_quote:
        return 'editor_review'
    return 'pending_context'


def run_batch(root,episodes,input_hash,config,destination,provider=None,selected_numbers=None,
              reference_bundle=None,frozen_hash=None,method='semantic'):
    if config['batch'].get('pipeline_version')==2 and reference_bundle is None and method=='semantic':
        from .discovery import run_discovery
        return run_discovery(root,episodes,input_hash,config,destination,provider,selected_numbers)
    destination.mkdir(parents=True,exist_ok=True)
    selected=list(selected_numbers if selected_numbers is not None else config['batch']['ai_episode_numbers'])
    held=split_sets(config)['held_out']
    if selected_numbers is None and not selected:
        selected=[e.episode_number for e in episodes if e.source_ready and e.episode_number not in held]
        cap=config['batch'].get('auto_episode_limit')
        if cap and len(selected)>cap:selected=[selected[round(i*(len(selected)-1)/(cap-1))] for i in range(cap)] if cap>1 else selected[:1]
    if not selected:raise ValueError('No source-ready episodes selected; check input timing before AI analysis.')
    evaluating=bool(set(selected)&held)
    if len(set(selected))!=len(selected):raise ValueError('Batch subset must be unique')
    if evaluating and (reference_bundle is None or not set(selected)<=held):
        raise ValueError('Held-out selection needs independent references; do not mix splits')
    if method not in ('semantic','baseline'):raise ValueError('Unknown review method')
    known={e.episode_number:e for e in episodes}
    if not set(selected)<=set(known):raise ValueError('Predeclared subset absent from input')
    if any(not known[n].source_ready for n in selected):raise ValueError('AI subset contains unresolved source timing')
    if evaluating:
        for number in selected:require_held_out_access(known[number],episodes,input_hash,config,reference_bundle,frozen_hash)
    identity={'input_hash':input_hash,'config_hash':config_hash(config),'ai_episode_numbers':selected,
              'scope':config['batch']['scope']}
    if evaluating:identity.update(scope='held_out_evaluation',method=method,context_policy='one_per_episode')
    elif method!='semantic':raise ValueError('Baseline mode is reserved for matched evaluation')
    manifest=destination/'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text())!=identity:
        raise ValueError('Batch resume input/config/subset mismatch')
    save(manifest,identity);save(destination/'effective_config.json',config)
    api=provider or JsonProvider(root,config,destination,purpose='evaluation' if evaluating else 'discovery')
    coverage=[]
    for ep in episodes:
        folder=destination/'episodes'/ep.episode_id
        folder.mkdir(parents=True,exist_ok=True)
        status=('requires_source_review' if not ep.source_ready else
                'ai_selected_pending' if ep.episode_number in selected else
                'held_out_locked' if ep.episode_number in held else 'deterministic_only_unreviewed')
        # Never expose even baseline selections from the held-out set here.
        deterministic=[] if status in ('held_out_locked','requires_source_review') else select_baseline(sliding_windows(ep),2)
        if not (folder/'deterministic_unreviewed.json').exists():
            save(folder/'deterministic_unreviewed.json',[c.model_dump() for c in deterministic])
        coverage.append({'episode_id':ep.episode_id,'episode_number':ep.episode_number,'scope_status':status,
                         'source_ready':ep.source_ready,'deterministic_candidates':len(deterministic),
                         'ai_analysis_complete':False})
    all_candidates=[];blinds={};review_rows=[];context_candidates=[]
    def progress(stage,status='running',reason=None,retry_after=None):
        for item in coverage:
            n=item['episode_number']
            item['ai_analysis_complete']=n in selected and (destination/'episodes'/item['episode_id']/'story_signals.json').exists()
            if item['ai_analysis_complete']:item['scope_status']='ai_analyzed_subset'
        report={**identity,'stage':stage,'status':status,'reason':reason,'retry_after_seconds':retry_after,
            'updated_at':datetime.now(timezone.utc).isoformat(),'total_episodes':len(episodes),
            'ai_analyzed_episodes':sum(c['ai_analysis_complete'] for c in coverage),
            'blind_reviewed':len(list((destination/'reviews/blind').glob('*.json'))),
            'context_reviewed':len(list((destination/'reviews/context').glob('*.json'))),
            'context_budget_not_reviewed':max(0,len(context_candidates)-(len({r[1].episode_id for r in context_candidates}) if evaluating else config['batch']['context_limit'])),
            'failed_reviews':len(list((destination/'review_failures').glob('*.json'))),
            'ai_eligible':sum(r['status']=='ai_eligible' for r in review_rows),
            'human_reviewed':False,'coverage':coverage,
            'note':'Subset AI processing; deterministic/unreviewed and locked outputs are not promos.'}
        save(destination/'batch_report.json',report)
        if review_rows or status=='complete_subset':write_review_exports(destination,review_rows,config['output']['ranked_target'],
            config['output']['post_first_target'],config['output']['near_tie_tolerance'])
        return report
    current='prepare'
    try:
        for number in selected:
            ep=known[number];folder=destination/'episodes'/ep.episode_id
            current=f'analysis:{ep.episode_id}';progress(current)
            path=folder/'story_signals.json'
            if method=='baseline':
                result={'review_queue':select_baseline(sliding_windows(ep),config['candidates']['blind_reviewed_per_episode']),
                        'ledger':[{'status':'baseline_window_selection','method':'no_semantic_discovery'}]}
            else:
                if path.exists():analysis=CombinedAnalysis.model_validate_json(path.read_text())
                else:analysis=extract_story(api.request,ep,config,folder)
                result=assemble(ep,analysis.signals,config['candidates']['raw_per_episode'],
                    config['candidates']['assembled_per_episode'],config['candidates']['blind_reviewed_per_episode'],
                    config['candidates']['near_duplicate_iou'])
            save(folder/'candidate_decisions.json',result['ledger'])
            save(folder/'review_queue.json',[c.model_dump() for c in result['review_queue']])
            all_candidates.extend((ep,c) for c in result['review_queue'])
        for ep,candidate in all_candidates:
            current=f'blind:{candidate.candidate_id}';progress(current)
            package,mapping=blind_package(candidate)
            def validate(value):
                if not set(value.hook_evidence_ids+value.cliffhanger_evidence_ids)<=set(mapping):raise ValueError('Unknown blind evidence')
            path=destination/'reviews/blind'/f'{candidate.candidate_id}.json'
            if path.exists():
                value=BlindReview.model_validate_json(path.read_text());validate(value)
            else:
                try:
                    value=api.request('blind',f'blind:{candidate.candidate_hash}',prompts.BLIND,package,BlindReview,validate)
                except RequestExhausted as exc:
                    save(destination/'review_failures'/f'blind-{candidate.candidate_id}.json',
                         {'candidate_id':candidate.candidate_id,'role':'blind','status':'failed_not_recommended','reason':str(exc)})
                    continue
                save(path,value.model_dump())
            blinds[candidate.candidate_id]=value
            screen=blind_screen(value,config)
            score=.30*value.hook_score+.30*value.cliffhanger_score+.25*value.standalone_clarity_score+.15*value.emotional_stakes_score
            if screen=='pending_context':context_candidates.append((score,ep,candidate,mapping,value))
            save(destination/'blind_decisions'/f'{candidate.candidate_id}.json',
                 {'candidate_id':candidate.candidate_id,'status':screen,'provisional_score':score,'context_reviewed':False})
        # Exact provisional scores only choose a bounded review pool; final order groups near ties.
        context_candidates.sort(key=lambda row:(-row[0],row[1].episode_number,row[2].start_ms))
        if evaluating:
            chosen=[];seen=set()
            for item in context_candidates:
                if item[1].episode_id not in seen:chosen.append(item);seen.add(item[1].episode_id)
        else:chosen=context_candidates[:config['batch']['context_limit']]
        save(destination/'context_queue.json',[{'candidate_id':c.candidate_id,'status':'pending_context'} for _,_,c,_,_ in chosen])
        for _,ep,candidate,mapping,blind in chosen:
            current=f'context:{candidate.candidate_id}';progress(current)
            payload,ids,warnings=context_package(candidate,ep,episodes,config,blind,mapping)
            path=destination/'reviews/context'/f'{candidate.candidate_id}.json'
            if path.exists():
                value=ContextReview.model_validate_json(path.read_text());validate_context(value,candidate,payload,ids)
            else:
                try:
                    value=api.request('context',f'context:{candidate.candidate_hash}',prompts.CONTEXT,payload,
                        ContextReview,lambda v:validate_context(v,candidate,payload,ids))
                except RequestExhausted as exc:
                    save(destination/'review_failures'/f'context-{candidate.candidate_id}.json',
                         {'candidate_id':candidate.candidate_id,'role':'context','status':'failed_not_recommended','reason':str(exc)})
                    continue
                save(path,value.model_dump())
            row=review_decision(candidate,blind,value,mapping,config,warnings)
            review_rows.append(enrich_review(row,candidate,ep,{**identity,'models':config['models'],'complete':False,
                'duration_margin_warning_ms':config['output']['duration_margin_warning_ms']}))
            progress(current)
        for row in review_rows:row['run_complete']=True
        # A completed subset never claims that all 50 episodes received AI review.
        return progress('complete','complete_subset')
    except BudgetPause as exc:
        return progress(current,'paused_budget',exc.reason,exc.retry_after)
    except ProviderFailure as exc:
        return progress(current,'paused_provider',str(exc),30)
