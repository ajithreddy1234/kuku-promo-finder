"""Whole-show discovery with bounded parallel calls and explicit review budgets.

Legacy runs retain their pinned orchestration. This version scans the selected
source episodes, overlaps a small audience preview with analysis on a different
model, and allocates the remaining review budget across the show.
"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from datetime import datetime,timezone
from copy import deepcopy
import json

from .analysis_planning import CombinedAnalysis
from .batch import save,blind_screen,validate_context
from .budget import BudgetPause,estimate_request
from .candidates import Candidate,assemble,sliding_windows,select_baseline
from .evaluation import config_hash,split_sets
from .exports import enrich_review,write_review_exports
from .provider import JsonProvider,ProviderFailure,RequestExhausted,strict_schema
from .review_models import SourceBlindReview,ShowContextReview
from .show_context import build_index
from .story import extract_story
from .thin_slice import blind_package,context_package,review_decision
from . import prompts
from .context_view import compact_context
from .coverage import allocate,candidate_key,excluded_numbers,source_reserved_numbers
from .editor_policy import apply_editor_policy


def choose_pool(items,cap,already=()):
    """Stratify by show position, prioritize semantic signals within each band.

    This is discovery allocation, not a claim of promo quality. Existing preview
    reviews count against the SAME cap. Unselected candidates are unreviewed.
    """
    ordered=sorted(items,key=lambda x:(x[0].episode_number,x[1].start_ms,x[1].end_ms))
    selected=[x for cid in already for x in ordered if x[1].candidate_id==cid]
    selected=selected[:cap];seen={c.candidate_id for _,c in selected}
    episode_order=sorted({e.episode_number for e,_ in ordered})
    position={n:i for i,n in enumerate(episode_order)}
    bands=[[] for _ in range(min(5,len(episode_order)))]
    for ep,c in ordered:
        if c.candidate_id not in seen:
            band=min(len(bands)-1,position[ep.episode_number]*len(bands)//len(episode_order))
            bands[band].append((ep,c))
    for band in bands:band.sort(key=lambda x:(-x[1].discovery_priority,x[1].source!='semantic',x[0].episode_number,x[1].start_ms))
    # Prefer different episodes before a second candidate from the same one.
    while len(selected)<cap and any(bands):
        for band in bands:
            if not band or len(selected)>=cap:continue
            represented={e.episode_number for e,_ in selected}
            index=next((i for i,(e,_) in enumerate(band) if e.episode_number not in represented),0)
            chosen=band.pop(index);selected.append(chosen)
    return selected


def run_discovery(root,episodes,input_hash,config,destination,provider=None,selected_numbers=None):
    destination.mkdir(parents=True,exist_ok=True)
    held=split_sets(config)['held_out'];known={e.episode_number:e for e in episodes}
    excluded=excluded_numbers(config)|source_reserved_numbers(root,episodes,config['batch'].get('respect_evaluation_reservations',True))
    coverage_first=config['batch'].get('coverage_policy')=='coverage_first_v2'
    selected=list(selected_numbers if selected_numbers is not None else config['batch']['ai_episode_numbers'])
    if not selected:selected=[e.episode_number for e in episodes if e.source_ready and e.episode_number not in excluded]
    if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(known):
        raise ValueError('Choose valid, unique episodes before analysis.')
    if set(selected)&held:raise ValueError('Held-out episodes require the separate independent evaluation workflow.')
    if set(selected)&excluded:raise ValueError('Reserved/exposed episodes are excluded from the ordinary coverage pass.')
    if any(not known[n].source_ready for n in selected):raise ValueError('Selected episode timing needs source review.')
    identity={'input_hash':input_hash,'config_hash':config_hash(config),'ai_episode_numbers':selected,'scope':config['batch']['scope']}
    if coverage_first and excluded-excluded_numbers(config):identity['source_reserved_episode_numbers']=sorted(excluded)
    manifest=destination/'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text())!=identity:raise ValueError('Batch resume input/config/subset mismatch')
    save(manifest,identity);save(destination/'effective_config.json',config)
    api=provider or JsonProvider(root,config,destination)
    settings=config['batch'];workers=max(1,min(4,settings.get('max_workers',2)))
    local=any(config['models'][r]['provider']=='ollama' for r in ('analysis','blind','context'))
    if local:workers=1
    blind_cap=settings.get('blind_global_limit',40);context_cap=settings.get('context_limit',30)
    analyses={};items=[];rows=[];pauses=[];failures=[];current='prepare'
    coverage=[]
    for ep in episodes:
        folder=destination/'episodes'/ep.episode_id;folder.mkdir(parents=True,exist_ok=True)
        status='requires_source_review' if not ep.source_ready else 'held_out_locked' if ep.episode_number in held else 'coverage_reserved' if ep.episode_number in excluded else 'ai_selected_pending' if ep.episode_number in selected else 'not_selected'
        baseline_path=folder/'deterministic_unreviewed.json'
        if not baseline_path.exists():save(baseline_path,[c.model_dump() for c in select_baseline(sliding_windows(ep),2)] if status=='not_selected' else [])
        coverage.append({'episode_id':ep.episode_id,'episode_number':ep.episode_number,'source_ready':ep.source_ready,
            'scope_status':status,'ai_analysis_complete':False})
    def progress(stage,status='running',reason=None,retry_after=None):
        for entry in coverage:
            done=entry['episode_number'] in selected and (destination/'episodes'/entry['episode_id']/'story_signals.json').exists()
            entry['ai_analysis_complete']=done
            if done:entry['scope_status']='ai_analyzed'
        ready={e.episode_number for e in episodes if e.source_ready and e.episode_number not in excluded}
        complete=all(c['ai_analysis_complete'] for c in coverage if c['episode_number'] in selected)
        report={**identity,'pipeline_version':2,'stage':stage,'status':status,'reason':reason,'retry_after_seconds':retry_after,
            'updated_at':datetime.now(timezone.utc).isoformat(),'total_episodes':len(episodes),'selected_episodes':len(selected),
            'ai_analyzed_episodes':sum(c['ai_analysis_complete'] for c in coverage),'analysis_complete':complete,
            'all_usable_episodes_selected':set(selected)==ready,'full_show_analysis_complete':complete and set(selected)==ready,
            'blind_reviewed':len(list((destination/'reviews/blind').glob('*.json'))),
            'context_reviewed':len(list((destination/'reviews/context').glob('*.json'))),
            'failed_reviews':len(list((destination/'review_failures').glob('*.json'))),
            'ai_eligible':sum(r['status']=='ai_eligible' for r in rows),'human_reviewed':False,'coverage':coverage,
            'editor_review':sum(r['status']=='editor_review' for r in rows),
            'review_budgets':{'blind_max':blind_cap,'context_max':context_cap},'provisional':not complete or status!='complete_subset',
            'note':'Every selected usable episode is scanned. Bounded deeper reviews do not exhaust all possible clips. Unreviewed means not assessed, not rejected.'}
        allocation_path=destination/'review_budget_allocation.json'
        if allocation_path.exists():report['review_budget_allocation']=json.loads(allocation_path.read_text())
        report['context_budget_not_reviewed']=len(list((destination/'context_budget_unreviewed').glob('*.json')))
        report['context_capacity_not_reviewed']=len(list((destination/'context_capacity_unreviewed').glob('*.json')))
        if coverage_first:
            primary=allocate(items,len({e.episode_number for e,_ in items}),excluded)
            pool_path=destination/'global_review_pool.json'
            allocated=json.loads(pool_path.read_text()) if pool_path.exists() else []
            allocated_ids={r['candidate_id'] for r in allocated if r['status']=='selected_for_review'}
            completed={p.stem for p in (destination/'reviews/blind').glob('*.json')}
            failed={p.stem.removeprefix('blind-') for p in (destination/'review_failures').glob('blind-*.json')}
            primary_ids={c.candidate_id for _,c in primary}
            report['coverage_pass']={'policy':'coverage_first_v2','source_ready_episodes':sum(e.source_ready for e in episodes),
                'eligible_episodes':len(ready),'selected_episodes':len(selected),'episodes_with_candidates':len(primary),
                'reviewed_episodes':len(primary_ids&completed),'failed_primary_reviews':len(primary_ids&failed),
                'not_reviewed_episodes':len(primary_ids-completed),
                'allocated_candidates':len(allocated_ids),'declared_pass_complete':bool(allocated_ids) and allocated_ids<=completed|failed,
                'all_primary_candidates_reviewed':bool(primary_ids) and primary_ids<=completed and complete,
                'excluded_episode_numbers':sorted(excluded&set(known)),
                'source_exclusions':[e.episode_number for e in episodes if not e.source_ready]}
            report['whole_show_results']=complete and set(selected)==ready and bool(primary_ids) and primary_ids<=completed
            report['provisional']=report['provisional'] or not report['whole_show_results']
        save(destination/'batch_report.json',report)
        write_review_exports(destination,rows,config['output']['ranked_target'],config['output']['post_first_target'],config['output']['near_tie_tolerance'])
        return report
    def failure(role,key,exc):
        save(destination/'review_failures'/f'{role}-{key}.json',{'role':role,'candidate_id':key,'status':'failed_not_recommended','reason':str(exc)})
    def analyze(ep):
        folder=destination/'episodes'/ep.episode_id;path=folder/'story_signals.json'
        analysis=CombinedAnalysis.model_validate_json(path.read_text()) if path.exists() else extract_story(api.request,ep,config,folder)
        result=assemble(ep,analysis.signals,config['candidates']['raw_per_episode'],config['candidates']['assembled_per_episode'],
                        config['candidates']['blind_reviewed_per_episode'],config['candidates']['near_duplicate_iou'])
        queue=result['review_queue']
        if coverage_first:
            queue=sorted(result['retained'],key=candidate_key)[:config['candidates']['blind_reviewed_per_episode']]
        save(folder/'candidate_decisions.json',result['ledger']);save(folder/'review_queue.json',[c.model_dump() for c in queue])
        save(folder/'deterministic_unreviewed.json',[c.model_dump() for c in result['baseline_queue']])
        return ep,analysis,[(ep,c) for c in queue]
    def audience(item):
        ep,c=item;payload,mapping=blind_package(c);path=destination/'reviews/blind'/f'{c.candidate_id}.json'
        provenance=destination/'review_provenance/blind'/f'{c.candidate_id}.json'
        judged={'provider':config['models']['blind']['provider'],'model':config['models']['blind']['model'],
                'prompt_version':config['models']['blind']['prompt_version'],'fallback_used':False}
        def validate(v):
            if not set(v.hook_evidence_ids+v.cliffhanger_evidence_ids)<=set(mapping):raise ValueError('Unknown blind evidence')
            quote=v.threat_target_quote;event=v.threat_target_evidence_id
            if (quote is None)!=(event is None):raise ValueError('Threat target needs both quote and evidence')
            if quote:
                source=next((e for e in payload['events'] if e['id']==event and e['content_status']=='full'),None)
                if not source or quote not in source['text']:raise ValueError('Threat target quote is not exact selected source')
        if path.exists():
            value=SourceBlindReview.model_validate_json(path.read_text());validate(value)
            if provenance.exists():judged=json.loads(provenance.read_text())
        else:
            try:value=api.request('blind',f'blind:{c.candidate_hash}',prompts.BLIND_SOURCE,payload,SourceBlindReview,validate)
            except RequestExhausted as exc:
                backup=config.get('review',{}).get('blind_fallback_role')
                if not backup or backup not in config['models'] or config['models'][backup]['model']==judged['model']:
                    failure('blind',c.candidate_id,exc);return None
                role=config['models'][backup]
                if role['prompt_version']!=judged['prompt_version']:raise ValueError('Backup must use the frozen audience prompt version')
                try:value=api.request(backup,f'blind:{c.candidate_hash}',prompts.BLIND_SOURCE,payload,SourceBlindReview,validate)
                except RequestExhausted as backup_exc:failure('blind',c.candidate_id,backup_exc);return None
                judged={'provider':role['provider'],'model':role['model'],'prompt_version':role['prompt_version'],
                        'fallback_used':True,'primary_model':config['models']['blind']['model'],
                        'reason':'Primary response exhausted bounded attempts; one backup attempt. Not part of homogeneous primary ranking.'}
            save(provenance,judged)
            save(path,value.model_dump())
        score=.30*value.hook_score+.30*value.cliffhanger_score+.25*value.standalone_clarity_score+.15*value.emotional_stakes_score
        screen=blind_screen(value,config)
        if judged['fallback_used'] and screen=='pending_context':screen='editor_review'
        save(destination/'blind_decisions'/f'{c.candidate_id}.json',{'candidate_id':c.candidate_id,'status':screen,
            'provisional_score':score,'context_reviewed':False,'judged_by':{'blind':judged},
            'editor_review_reasons':['backup_judge_requires_editor'] if judged['fallback_used'] else []})
        return score,ep,c,mapping,value
    def record_error(exc):
        if isinstance(exc,(BudgetPause,ProviderFailure)):pauses.append(exc)
        else:raise exc
    def parallel(tasks,fn,label):
        """At most workers active; stop scheduling at the first quota pause."""
        nonlocal current
        result=[];iterator=iter(tasks);stopped=False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending={}
            def submit():
                try:item=next(iterator)
                except StopIteration:return False
                pending[pool.submit(fn,item)]=item;return True
            for _ in range(workers):submit()
            while pending:
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    item=pending.pop(future)
                    try:
                        value=future.result()
                        if value is not None:result.append(value)
                    except RequestExhausted as exc:
                        key=getattr(item,'episode_id',str(item));failure(label,key,exc);failures.append(exc)
                    except (BudgetPause,ProviderFailure) as exc:record_error(exc);stopped=True
                current=label;progress(current)
                if not stopped:
                    while len(pending)<workers and submit():pass
        return result
    # Preview audience requests can run while another model analyses subsequent
    # episodes. They count against the global review cap, not an extra budget.
    overlap=config['models']['analysis']['model']!=config['models']['blind']['model']
    preview_limit=min(blind_cap,settings.get('stream_preview_limit',8)) if overlap and not coverage_first else 0
    preview_ids=[];preview_futures=[]
    with ThreadPoolExecutor(max_workers=1) as preview:
        def analyze_and_preview(ep):
            return analyze(ep)
        # Main thread owns preview scheduling and report writes.
        iterator=iter([known[n] for n in selected]);stopped=False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending={}
            def submit_analysis():
                for ep in iterator:
                    if (destination/'review_failures'/f'analysis-{ep.episode_id}.json').exists():continue
                    pending[pool.submit(analyze_and_preview,ep)]=ep;return True
                return False
            for _ in range(workers):submit_analysis()
            while pending:
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    ep=pending.pop(future)
                    try:
                        ep,analysis,candidates=future.result();analyses[ep.episode_id]=analysis;items.extend(candidates)
                        if candidates and len(preview_ids)<preview_limit:
                            candidate=candidates[0];preview_ids.append(candidate[1].candidate_id)
                            preview_futures.append(preview.submit(audience,candidate))
                    except RequestExhausted as exc:failure('analysis',ep.episode_id,exc);failures.append(exc)
                    except (BudgetPause,ProviderFailure) as exc:record_error(exc);stopped=True
                current='analysis';progress(current)
                if not stopped:
                    while len(pending)<workers and submit_analysis():pass
        for future in preview_futures:
            try:future.result()
            except (BudgetPause,ProviderFailure) as exc:record_error(exc)
    # A pause in one part must not hide already checkpointed later episodes.
    for number in selected:
        ep=known[number];folder=destination/'episodes'/ep.episode_id
        if ep.episode_id not in analyses and (folder/'story_signals.json').exists():
            ep,analysis,candidates=analyze(ep);analyses[ep.episode_id]=analysis;items.extend(candidates)
    show_index=build_index(episodes,analyses,excluded);save(destination/'show_index.json',show_index)
    all_analyzed=len(analyses)==len(selected)
    permanently_failed={p.stem.removeprefix('analysis-') for p in (destination/'review_failures').glob('analysis-*.json')}
    analysis_stage_finished=all_analyzed or all(known[n].episode_id in analyses or known[n].episode_id in permanently_failed for n in selected)
    existing=[p.stem for p in (destination/'reviews/blind').glob('*.json')]
    existing+= [p.stem.removeprefix('blind-') for p in (destination/'review_failures').glob('blind-*.json')]
    pool_cap=blind_cap if analysis_stage_finished else min(blind_cap,max(8,len(existing)))
    done_items=[(e,c) for e,c in items if c.candidate_id in existing]
    pool=(done_items+allocate(items,max(0,pool_cap-len(done_items)),excluded,existing)) if coverage_first else choose_pool(items,pool_cap,sorted(set(existing)))
    requested_pool_size=len(pool)
    allocation=None
    if settings.get('fit_review_pool_to_budget') and getattr(api,'budget',None) and config['models']['blind']['provider']=='groq':
        model=config['models']['blind']['model'];limits=api.budget.limits(model)
        headroom=max(0,limits['tokens_per_day']-config['quota'].get('daily_safety_reserve_tokens',10000)
                     -config['quota'].get('evaluation_reserve_by_model',{}).get(model,0)
                     -api.budget.snapshot()['models'][model]['rolling_24h_accounted_tokens'])
        initial=headroom;fitting=[];already=set(existing)
        if coverage_first:
            # Re-stratify after reducing the cap, so a tiny budget still spans
            # the whole eligible show rather than taking the first few bands.
            costs={}
            for ep,c in pool:
                payload,_=blind_package(c)
                costs[c.candidate_id]=0 if c.candidate_id in already else api.request_cost('blind',f'blind:{c.candidate_hash}',prompts.BLIND_SOURCE,payload,SourceBlindReview)
            for cap in range(max(0,pool_cap-len(done_items)),-1,-1):
                trial=done_items+allocate(items,cap,excluded,existing)
                for ep,c in trial:
                    if c.candidate_id not in costs:
                        payload,_=blind_package(c)
                        costs[c.candidate_id]=0 if c.candidate_id in already else api.request_cost('blind',f'blind:{c.candidate_hash}',prompts.BLIND_SOURCE,payload,SourceBlindReview)
                cost=sum(costs[c.candidate_id] for _,c in trial)
                if cost<=headroom:
                    pool=trial;break
            fitting=pool;headroom-=cost
        for ep,c in pool:
            if coverage_first:break
            payload,_=blind_package(c)
            cost=0 if c.candidate_id in already else api.request_cost('blind',f'blind:{c.candidate_hash}',prompts.BLIND_SOURCE,payload,SourceBlindReview)
            if cost<=headroom:fitting.append((ep,c));headroom-=cost
        allocation={'policy':'conservative_daily_headroom_v1','model':model,'headroom_before':initial,
                    'estimated_new_reservations':initial-headroom,'requested_pool':requested_pool_size,'allocated_pool':len(fitting),
                    'note':'Selection budget, not a quality rejection. Reported cache savings may permit a larger future run.'}
        pool=fitting;save(destination/'review_budget_allocation.json',allocation)
    selected_ids={c.candidate_id for _,c in pool}
    primary_ids={c.candidate_id for _,c in allocate(items,len({ep.episode_number for ep,_ in items}),excluded)}
    order={c.candidate_id:i for i,(_,c) in enumerate(pool,1)}
    save(destination/'global_review_pool.json',[{'candidate_id':c.candidate_id,'episode_id':ep.episode_id,
        'status':'selected_for_review' if c.candidate_id in selected_ids else 'not_reviewed',
        'allocation_pass':1 if c.candidate_id in primary_ids else 2,'selection_order':order.get(c.candidate_id),
        'discovery_priority':c.discovery_priority,'boundary_warning_count':len(c.warnings),
        'duration_margin_ms':min(c.duration_ms-20000,60000-c.duration_ms),
        'policy':'coverage_first_v2' if coverage_first else 'chronological_bands_semantic_priority_episode_diversity_v1'}
        for ep,c in sorted(items,key=lambda x:(x[0].episode_number,x[1].start_ms,x[1].end_ms))])
    blind_results=parallel([item for item in pool if not (destination/'review_failures'/f'blind-{item[1].candidate_id}.json').exists()],audience,'blind')
    def audience_provenance(c):
        p=destination/'review_provenance/blind'/f'{c.candidate_id}.json'
        return json.loads(p.read_text()) if p.exists() else {'model':config['models']['blind']['model'],'fallback_used':False}
    finalists=sorted([r for r in blind_results if blind_screen(r[4],config)=='pending_context' and not audience_provenance(r[2]).get('fallback_used')],key=lambda r:(-r[0],r[1].episode_number,r[2].start_ms))
    if coverage_first:
        for score,ep,c,mapping,blind in blind_results:
            judged=audience_provenance(c);screen=blind_screen(blind,config)
            if screen!='pending_context' or judged.get('fallback_used'):
                row={**c.model_dump(),'status':'rejected' if screen=='blind_rejected' else 'editor_review','score':score,
                     'blind':blind.model_dump(),'context':{},'hook':blind.hook,'cliffhanger':blind.cliffhanger,
                     'why_it_works':blind.why_it_works,'human_reviewed':False,'judged_by':{'blind':judged},
                     'hook_evidence_ids':[mapping[i] for i in blind.hook_evidence_ids],
                     'cliffhanger_evidence_ids':[mapping[i] for i in blind.cliffhanger_evidence_ids]}
                rows.append(enrich_review(row,c,ep,{**identity,'models':config['models'],'editor_policy':True}))
    current_context=list((destination/'reviews/context').glob('*.json'))
    active_context_cap=context_cap if analysis_stage_finished else min(context_cap,max(3,len(current_context)))
    def contextual(item):
        _,ep,c,mapping,blind=item
        context_config=deepcopy(config)
        context_config['batch']['coverage_excluded_episode_numbers']=sorted(excluded)
        payload,ids,warnings=context_package(c,ep,episodes,context_config,blind,mapping,show_index)
        role=config['models']['context'];schema=strict_schema(ShowContextReview)
        limit=role.get('context_tokens',8192) if role['provider']=='ollama' else config['quota']['models'][role['model']]['tokens_per_minute']
        retrieved=payload['related_show_source']['records'];dropped=[]
        extra_warnings=[]
        use_compact=config.get('review',{}).get('context_view')=='compact_rows_v1'
        while True:
            wire=compact_context(payload) if use_compact else payload
            messages=[{'role':'system','content':prompts.CONTEXT_SHOW},
                      {'role':'user','content':json.dumps(wire,ensure_ascii=False,separators=(',',':'))}]
            if estimate_request(messages,schema,role['max_output_tokens'])['reserved_tokens']<=limit:break
            if retrieved:dropped.append(retrieved.pop()['id']);continue
            optional=[e for e in payload['current_source']['events'] if e['id'] not in c.selected_event_ids]
            if optional:
                farthest=max(optional,key=lambda e:max(c.start_ms-e['end_ms'],e['start_ms']-c.end_ms))
                payload['current_source']['events'].remove(farthest);dropped.append(farthest['id'])
                extra_warnings.append('adjacent_source_context_reduced_to_fit_capacity');continue
            future=payload.get('next_episode_opening')
            if future and future['events']:
                dropped.append(future['events'].pop()['id'])
                extra_warnings.append('next_episode_context_reduced_to_fit_capacity');continue
            save(destination/'context_capacity_unreviewed'/f'{c.candidate_id}.json',
                {'candidate_id':c.candidate_id,'status':'not_verified_capacity',
                 'reason':'Full selected source does not fit this model budget. No selected text was truncated; other candidates continue.'})
            return None
        if dropped:
            ids-=set(dropped);warnings.append('related_context_reduced_to_fit_capacity')
        warnings.extend(sorted(set(extra_warnings)))
        path=destination/'reviews/context'/f'{c.candidate_id}.json'
        if path.exists():
            value=ShowContextReview.model_validate_json(path.read_text());validate_context(value,c,payload,ids)
        else:
            try:value=api.request('context',f'context:{c.candidate_hash}',prompts.CONTEXT_SHOW,wire,ShowContextReview,
                                  lambda v:validate_context(v,c,payload,ids))
            except BudgetPause as exc:
                if settings.get('fit_review_pool_to_budget') and 'daily' in str(exc).lower():
                    save(destination/'context_budget_unreviewed'/f'{c.candidate_id}.json',
                         {'candidate_id':c.candidate_id,'status':'not_verified_quota','reason':str(exc)})
                    return None
                raise
            except RequestExhausted as exc:failure('context',c.candidate_id,exc);return None
            save(path,value.model_dump())
        decision=review_decision(c,blind,value,mapping,config,warnings)
        decision['judged_by']={'blind':audience_provenance(c),'context':{
            'provider':role['provider'],'model':role['model'],'prompt_version':role['prompt_version'],'fallback_used':False}}
        return enrich_review(decision,c,ep,{**identity,'models':config['models'],'complete':False,
            'editor_policy':config.get('review',{}).get('editor_policy'),
            'eligibility_policy_version':config['eligibility'].get('policy_version'),
            'duration_margin_warning_ms':config['output']['duration_margin_warning_ms']})
    # Restore finished rows before spending anything, including provisional ones.
    indexed={item[2].candidate_id:item for item in blind_results}
    for path in current_context:
        if path.stem in indexed:
            row=contextual(indexed[path.stem])
            if row:rows.append(row)
    attempted=len(current_context)+len(list((destination/'review_failures').glob('context-*.json')))
    remaining=[r for r in finalists if not (destination/'reviews/context'/f'{r[2].candidate_id}.json').exists()
               and not (destination/'review_failures'/f'context-{r[2].candidate_id}.json').exists()
               and not (destination/'context_capacity_unreviewed'/f'{r[2].candidate_id}.json').exists()]
    target=config['output']['ranked_target']
    chosen=[]
    while remaining and attempted<active_context_cap and sum(r['status']=='ai_eligible' for r in rows)<target:
        # Small waves permit rejected candidates to be replenished without
        # spending context quota on every candidate after the target is reached.
        wave=remaining[:min(workers,active_context_cap-attempted)];remaining=remaining[len(wave):]
        chosen.extend(wave);before=len(pauses)
        rows.extend(parallel(wave,contextual,'context'));attempted+=len(wave)
        if len(pauses)>before:break
    save(destination/'context_queue.json',[{'candidate_id':r[2].candidate_id,'status':'selected_for_context'} for r in chosen])
    unfinished_blind=any(not (destination/'reviews/blind'/f'{c.candidate_id}.json').exists()
        and not (destination/'review_failures'/f'blind-{c.candidate_id}.json').exists() for _,c in pool)
    unfinished_context=any(not (destination/'reviews/context'/f'{r[2].candidate_id}.json').exists()
        and not (destination/'review_failures'/f'context-{r[2].candidate_id}.json').exists() for r in finalists)
    real_context_attempts=len(list((destination/'reviews/context').glob('*.json')))+len(list((destination/'review_failures').glob('context-*.json')))
    if all_analyzed and not unfinished_blind and (not unfinished_context or real_context_attempts>=context_cap or sum(r['status']=='ai_eligible' for r in rows)>=target):
        pauses.clear() # a transient preview pause may have been resolved later
    if pauses:
        # Prefer the earliest actionable retry, not a long pause from a model
        # that still has no work to do in the next invocation.
        pause=min(pauses,key=lambda e:getattr(e,'retry_after',None) or 30)
        status=('paused_input' if pause.retry_after is None else 'paused_budget') if isinstance(pause,BudgetPause) else 'paused_provider'
        return progress(current,status,str(pause),pause.retry_after if isinstance(pause,BudgetPause) else 30)
    for row in rows:row['run_complete']=all_analyzed
    return progress('complete','complete_subset',
        'Some analyses failed; inspect coverage and raw failure logs.' if not all_analyzed else None)
