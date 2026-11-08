"""Meaningful orchestration/accounting checks. Fixtures are NOT quality evidence."""
import json
from pathlib import Path
import threading
import pytest
import yaml

from src.analysis_planning import plan_analysis_parts
from src.batch import run_batch,validate_context
from src.budget import BudgetLedger,BudgetPause
from src.candidates import make_candidate
from src.parser import parse_text
from src.review_models import CompactStoryAnalysis,CompactStorySignal,BlindReview,SourceBlindReview,ContextReview,ShowContextReview,EvidenceQuote
from src.story import extract_story
from src.show_context import build_index
from src.thin_slice import blind_package,context_package

ROOT=Path(__file__).resolve().parents[1]


def setup(count=4):
    c=yaml.safe_load((ROOT/'config.yaml').read_text());c['batch'].update(max_workers=1,stream_preview_limit=0)
    source=(ROOT/'data/examples/synthetic-episode-001.txt').read_text()
    eps=[parse_text(source.replace('Episode 1',f'Episode {n}').encode(),f'episode-{n}.txt') for n in range(1,count+1)]
    return c,eps


class Passing:
    def __init__(self):self.calls=[]
    def request(self,role,stage,system,payload,response_model,validate):
        self.calls.append((role,payload.get('episode_id')))
        if role=='analysis':value=CompactStoryAnalysis(signals=[],limitations=[])
        elif role=='blind':
            full=[e for e in payload['events'] if e['content_status']=='full']
            early=next(e for e in full if e['start_ms']<3000)
            value=SourceBlindReview(clip_id='clip',clear_to_new_viewer='yes',hook_within_first_3s='yes',ending_resolved='no',
                hook_score=8,cliffhanger_score=8,standalone_clarity_score=8,emotional_stakes_score=8,
                hook='Synthetic fixture',cliffhanger='Synthetic threat',why_it_works='Automated fixture, not an editorial judgment',
                hook_evidence_ids=[early['id']],cliffhanger_evidence_ids=[full[-1]['id']],uncertainty_reasons=[],cliffhanger_type='threat',
                threat_target_quote=full[-1]['text'],threat_target_evidence_id=full[-1]['id'])
        else:
            source=payload['current_source']
            e=source['events'][0] if 'events' in source else dict(zip(source['columns'],source['records'][0]))
            value=ShowContextReview(candidate_id=payload['candidate_id'],source_supported='yes',misleading_cut='no',
                essential_context_missing='no',ending_resolved_inside_clip='no',boundary_issue='none',
                evidence_event_ids=[e['id']],reasons=['Automated fixture only'],hook_factually_supported='yes',
                cliffhanger_factually_supported='yes',description_speakers_match_source='yes',
                evidence_quotes=[EvidenceQuote(event_id=e['id'],quote=e['text'])],source_threat_target='Synthetic fixture',
                described_threat_target='Synthetic fixture',description_consequences_match_source='yes')
        validate(value);return value


def test_every_episode_analyzed_but_deep_reviews_are_bounded(tmp_path):
    c,eps=setup(9);c['batch'].update(blind_global_limit=3,context_limit=2)
    api=Passing();r=run_batch(tmp_path,eps,'newshow',c,tmp_path/'batch',api)
    assert r['ai_analyzed_episodes']==9 and r['all_usable_episodes_selected'] and r['full_show_analysis_complete']
    assert r['blind_reviewed']==3 and r['context_reviewed']==2
    reviewed=json.loads((tmp_path/'batch/candidate_reviews.json').read_text())
    assert all(x['status']=='editor_review' and 'partial_camera_record' in x['editor_review_reasons'] for x in reviewed)
    assert r['status']=='complete_subset' and r['provisional']  # budget-limited coverage is explicit
    assert r['coverage_pass']['reviewed_episodes']==3 and not r['whole_show_results']
    pool=json.loads((tmp_path/'batch/global_review_pool.json').read_text())
    assert any(x['status']=='not_reviewed' for x in pool)
    assert len({x['episode_id'] for x in pool if x['status']=='selected_for_review'})==3
    assert all(x['run_complete'] for x in json.loads((tmp_path/'batch/candidate_reviews.json').read_text()))


def test_analysis_pause_still_reviews_finished_work_then_resume_uses_checkpoints(tmp_path):
    c,eps=setup(3);api=Passing();original=api.request;paused=False
    def request(role,stage,system,payload,model,validate):
        nonlocal paused
        if role=='analysis' and payload['episode_id']=='E002' and not paused:
            paused=True;raise BudgetPause('Synthetic daily pause',10000)
        return original(role,stage,system,payload,model,validate)
    api.request=request
    r=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    assert r['status']=='paused_budget' and r['ai_analyzed_episodes']==1
    assert r['blind_reviewed']>0 and r['context_reviewed']>0 and r['provisional']
    before=len([x for x in api.calls if x[0]=='blind'])
    r=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    assert r['full_show_analysis_complete']
    assert api.calls.count(('analysis','E001'))==1
    assert len([x for x in api.calls if x[0]=='blind'])==before+4


def test_audience_preview_overlaps_later_episode_analysis(tmp_path):
    c,eps=setup(2);c['batch'].update(stream_preview_limit=1,coverage_policy='legacy_preview_v1')
    api=Passing();original=api.request;audience_started=threading.Event()
    def request(role,stage,system,payload,model,validate):
        if role=='analysis' and payload['episode_id']=='E002':
            assert audience_started.wait(3),'Later analysis should not block the independent audience preview'
        if role=='blind':audience_started.set()
        return original(role,stage,system,payload,model,validate)
    api.request=request
    assert run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)['status']=='complete_subset'


def test_compact_view_preserves_all_words_speakers_and_source_mapping():
    c,eps=setup(1);ep=eps[0];seen={}
    parts=plan_analysis_parts(ep,c)
    for part in parts:
        for anchor,kind,speaker,text in part['payload']['records']:
            seen[part['anchor_map'][anchor]]=(kind,part['payload']['speakers'][speaker] if speaker is not None else None,text)
    assert seen=={e.event_id:(e.event_type,e.speaker_raw,e.text) for e in ep.temporal_events()}


def test_compact_analysis_rejects_invented_anchor_before_assembly(tmp_path):
    c,eps=setup(1)
    def request(role,stage,system,payload,model,validate):
        result=CompactStoryAnalysis(signals=[CompactStorySignal(kind='hook',source_event_ids=['INVENTED'],
            description='Synthetic invalid source',priority='high',resolution_event_ids=[])],limitations=[])
        validate(result);return result
    with pytest.raises(ValueError,match='Unknown source anchor'):extract_story(request,eps[0],c,tmp_path)


def test_whole_show_retrieval_is_source_linked_and_excludes_heldout():
    c,eps=setup(3);index=build_index(eps,{},excluded_numbers=[3]);candidate=make_candidate(eps[0],0,30000)
    package,mapping=blind_package(candidate);api=Passing()
    blind=api.request('blind','x','',package,BlindReview,lambda v:None)
    payload,known,warnings=context_package(candidate,eps[0],eps,c,blind,mapping,index)
    related=payload['related_show_source']
    assert related['records'] and not related['retrieval_is_exhaustive']
    assert all(r['episode_id']!='E003' for r in related['records'])
    record=related['records'][0]
    value=ContextReview(candidate_id=candidate.candidate_id,source_supported='yes',misleading_cut='no',
        essential_context_missing='no',ending_resolved_inside_clip='no',boundary_issue='none',reasons=[],
        evidence_event_ids=[record['id']],evidence_quotes=[EvidenceQuote(event_id=record['id'],quote=record['text'])])
    validate_context(value,candidate,payload,known)
    value.evidence_quotes[0].quote='NOT IN SOURCE'
    with pytest.raises(ValueError,match='Unsupported quote'):validate_context(value,candidate,payload,known)


def test_cache_credit_releases_reservation_without_resetting_usage(tmp_path):
    from test_budget_exports import Clock,budget_config
    clock=Clock();ledger=BudgetLedger(tmp_path,budget_config(),clock.now,clock.sleep)
    identity=ledger.reserve('A',{'reserved_tokens':90});ledger.settle(identity,80,cached_input_tokens=30)
    assert ledger.snapshot()['models']['A']['rolling_24h_accounted_tokens']==50
    ledger.reserve('A',{'reserved_tokens':50}) # measured headroom now fits
    assert not clock.sleeps


def test_historical_cache_credit_needs_proof_and_cannot_repeat(tmp_path):
    from test_budget_exports import Clock,budget_config
    clock=Clock();ledger=BudgetLedger(tmp_path,budget_config(),clock.now,clock.sleep)
    rid=ledger.reserve('A',{'reserved_tokens':90});ledger.settle(rid,80)
    folder=tmp_path/'proof';folder.mkdir()
    usage={'request_hash':'request','attempt':1,'model':'A','total_tokens':80,'quota_reservation_id':rid}
    raw={'request_hash':'request','attempt':1,'response':{'usage':{'total_tokens':80,'prompt_tokens':60,'prompt_tokens_details':{'cached_tokens':30}}}}
    (folder/'usage.jsonl').write_text(json.dumps(usage)+'\n');(folder/'raw_responses.jsonl').write_text(json.dumps(raw)+'\n')
    assert ledger.reconcile_cached_usage(folder/'usage.jsonl')==30
    assert ledger.reconcile_cached_usage(folder/'usage.jsonl')==0
    assert ledger.snapshot()['models']['A']['rolling_24h_accounted_tokens']==50


def test_consequence_target_mismatch_cannot_pass_even_with_high_scores():
    from src.thin_slice import review_decision
    c,eps=setup(1);candidate=make_candidate(eps[0],0,30000);payload,mapping=blind_package(candidate)
    api=Passing();blind=api.request('blind','x','',payload,BlindReview,lambda v:None)
    package,ids,warnings=context_package(candidate,eps[0],eps,c,blind,mapping)
    context=api.request('context','x','',package,ShowContextReview,lambda v:None)
    context.source_threat_target='Her husband';context.described_threat_target='Her'
    context.description_consequences_match_source='no'
    assert review_decision(candidate,blind,context,mapping,c,[])['status']=='rejected'


def test_non_groq_network_provider_is_disabled(tmp_path):
    from src.provider import JsonProvider,ProviderFailure
    c,_=setup(1);api=JsonProvider(tmp_path,c,tmp_path/'logs',transport=lambda *a:None)
    with pytest.raises(ProviderFailure,match='Groq only'):
        api._network({'provider':'ollama','model':'anything:cloud'},[],{},c['inference'])


def test_relation_threat_paraphrase_is_flagged_even_if_context_judge_agrees():
    from src.thin_slice import review_decision
    c,eps=setup(1);candidate=make_candidate(eps[0],0,30000);payload,mapping=blind_package(candidate)
    api=Passing();blind=api.request('blind','x','',payload,SourceBlindReview,lambda v:None)
    blind.threat_target_quote='your husband';blind.cliffhanger='She must obey or face execution.'
    package,ids,warnings=context_package(candidate,eps[0],eps,c,blind,mapping)
    context=api.request('context','x','',package,ShowContextReview,lambda v:None)
    context.source_threat_target='Alexander (her husband)'
    row=review_decision(candidate,blind,context,mapping,c,[])
    assert row['status']=='editor_review' and 'threat_target_paraphrase_needs_editor_confirmation' in row['warnings']


def test_review_pool_fits_available_quota_without_skipping_episode_analysis(tmp_path):
    c,eps=setup(4);c['batch'].update(fit_review_pool_to_budget=True,blind_global_limit=4)
    model=c['models']['blind']['model'];c['quota']['daily_safety_reserve_tokens']=0
    c['quota']['models'][model]['tokens_per_day']=120
    api=Passing();api.budget=BudgetLedger(tmp_path,c)
    rid=api.budget.reserve(model,{'reserved_tokens':40});api.budget.settle(rid,40)
    api.request_cost=lambda *a:80
    result=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    assert result['ai_analyzed_episodes']==4 and result['blind_reviewed']==1
    assert result['review_budget_allocation']['allocated_pool']==1
    assert result['status']=='complete_subset'


def test_blank_optional_resolution_is_normalized_without_inventing_evidence():
    from src.story import validate_compact_result
    raw={'signals':[{'kind':'hook','source_event_ids':['A001'],'description':'Synthetic fixture',
        'priority':'high','resolution_event_ids':['','A002',' ']}],'limitations':[]}
    value=CompactStoryAnalysis.model_validate(raw)
    assert validate_compact_result(value,{'A001':'source1','A002':'source2'})==2
    assert value.signals[0].resolution_event_ids==['A002'] and raw['signals'][0]['resolution_event_ids']==['','A002',' ']
    value.signals[0].source_event_ids=['']
    with pytest.raises(ValueError):validate_compact_result(value,{'A001':'source1'})


def test_one_oversized_context_does_not_abort_other_candidates(tmp_path,monkeypatch):
    import src.discovery as discovery
    c,eps=setup(1);api=Passing();original=discovery.context_package;calls=0
    def package(*args,**kwargs):
        nonlocal calls
        payload,ids,warnings=original(*args,**kwargs);calls+=1
        if calls==1:
            selected=set(args[0].selected_event_ids)
            next(e for e in payload['current_source']['events'] if e['id'] in selected)['text']='x'*50000
        return payload,ids,warnings
    monkeypatch.setattr(discovery,'context_package',package)
    result=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    assert result['status']=='complete_subset' and result['context_capacity_not_reviewed']==1
    assert result['context_reviewed']==1
    assert json.loads((tmp_path/'batch/candidate_reviews.json').read_text())[0]['status']=='editor_review'


def test_one_failed_analysis_does_not_lock_the_rest_to_preview_budget(tmp_path):
    from src.provider import RequestExhausted
    c,eps=setup(7);c['batch'].update(blind_global_limit=10,context_limit=0)
    api=Passing();original=api.request
    def request(role,stage,system,payload,model,validate):
        if role=='analysis' and payload['episode_id']=='E002':raise RequestExhausted('Synthetic permanent failure')
        return original(role,stage,system,payload,model,validate)
    api.request=request
    result=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    assert result['ai_analyzed_episodes']==6 and result['blind_reviewed']==10
    assert not result['full_show_analysis_complete']


def test_context_wire_compaction_preserves_text_and_source_fields():
    from src.context_view import compact_context
    c,eps=setup(1);candidate=make_candidate(eps[0],0,30000);clip,mapping=blind_package(candidate)
    blind=Passing().request('blind','x','',clip,SourceBlindReview,lambda v:None)
    payload,ids,warnings=context_package(candidate,eps[0],eps,c,blind,mapping,build_index(eps,{}))
    wire=compact_context(payload);source=wire['current_source']
    assert [dict(zip(source['columns'],r)) for r in source['records']]==payload['current_source']['events']
    assert 'events' in payload['current_source'] # validation source never mutated by compaction


def test_withheld_camera_text_does_not_crash_threat_attribution_check():
    from src.thin_slice import review_decision
    c,eps=setup(1);candidate=make_candidate(eps[0],0,30000);clip,mapping=blind_package(candidate)
    api=Passing();blind=api.request('blind','x','',clip,SourceBlindReview,lambda v:None)
    blind.threat_target_quote='your husband';blind.cliffhanger='Alexander faces the threat.'
    package,ids,warnings=context_package(candidate,eps[0],eps,c,blind,mapping)
    context=api.request('context','x','',package,ShowContextReview,lambda v:None)
    context.source_threat_target='Alexander';candidate.selected_content[0].text=None
    row=review_decision(candidate,blind,context,mapping,c,[])
    assert row['status']=='editor_review'


@pytest.mark.parametrize('kind,screen,decision',[('none','blind_rejected','rejected'),('uncertain','editor_review','editor_review')])
def test_cliffhanger_type_is_a_gate_even_when_every_score_is_high(kind,screen,decision):
    from src.batch import blind_screen
    from src.thin_slice import review_decision
    c,eps=setup(1);candidate=make_candidate(eps[0],0,30000);clip,mapping=blind_package(candidate)
    api=Passing();blind=api.request('blind','x','',clip,SourceBlindReview,lambda v:None)
    blind.cliffhanger_type=kind
    package,ids,warnings=context_package(candidate,eps[0],eps,c,blind,mapping)
    context=api.request('context','x','',package,ShowContextReview,lambda v:None)
    assert blind_screen(blind,c)==screen
    assert review_decision(candidate,blind,context,mapping,c,[])['status']==decision
