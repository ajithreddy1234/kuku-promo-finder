"""Synthetic allocation/decision tests, not human quality measurements."""
from copy import deepcopy
import json
import pytest
from test_optimized import setup,Passing
from src.batch import run_batch
from src.candidates import make_candidate
from src.coverage import allocate, candidate_key, excluded_numbers
from src.editor_policy import apply_editor_policy
from src.provider import JsonProvider, RequestExhausted
from src.review_models import SourceBlindReview
from src.thin_slice import blind_package


def items(count=8):
    config,eps=setup(count)
    return config,eps,[(e,make_candidate(e,0,30000,priority=2)) for e in eps]


def test_reference_exposed_and_quarantined_sources_never_allocated():
    c,eps,values=items()
    c['evaluation'].update(held_out_episode_numbers=[2],primary_reference_episode_numbers=[2])
    c['batch']['coverage_excluded_episode_numbers']=[3]
    eps[3].events[0].timing_status='uncertain'
    pool=allocate(values,20,excluded_numbers(c))
    assert {e.episode_number for e,_ in pool}=={1,5,6,7,8}


def test_direct_runner_cannot_bypass_source_reservation(tmp_path):
    c,eps=setup(2);c['batch']['respect_evaluation_reservations']=True
    private=tmp_path/'evaluation/private';private.mkdir(parents=True)
    (private/'coverage_reservations.json').write_text(json.dumps({'entries':[{'source_sha256':eps[1].source_sha256}]}))
    api=Passing()
    with pytest.raises(ValueError,match='Reserved'):
        run_batch(tmp_path,eps,'x',c,tmp_path/'explicit',api,selected_numbers=[2])
    assert api.calls==[]
    report=run_batch(tmp_path,eps,'x',c,tmp_path/'automatic',api)
    assert report['selected_episodes']==1
    index=json.loads((tmp_path/'automatic/show_index.json').read_text())
    assert index['source_episode_ids']==['E001']


def test_first_pass_one_per_episode_before_expansion_and_stable_order():
    c,eps,values=items()
    values.extend((e,make_candidate(e,10000,40000,priority=1)) for e in eps)
    first=allocate(values,8)
    assert len({e.episode_number for e,_ in first})==8
    assert [(e.episode_id,x.candidate_id) for e,x in allocate(values,12)]==[(e.episode_id,x.candidate_id) for e,x in allocate(list(reversed(values)),12)]


def test_priority_clean_boundaries_then_safe_duration_margin():
    c,eps,values=items(1);ep=eps[0];base=values[0][1]
    dirty=base.model_copy(update={'candidate_id':'dirty','warnings':['partial_camera_record']})
    near=base.model_copy(update={'candidate_id':'near','end_ms':20001,'duration_ms':20001})
    higher=dirty.model_copy(update={'candidate_id':'high','discovery_priority':3})
    assert allocate([(ep,dirty),(ep,near),(ep,base)],1)[0][1].candidate_id==base.candidate_id
    assert allocate([(ep,base),(ep,higher)],1)[0][1].candidate_id=='high'


@pytest.mark.parametrize('change,reason',[
    ({'warnings':['partial_camera_record']},'partial_camera_record'),
    ({'duration_ms':20500},'duration_margin_under_1s'),
    ({'cliffhanger_type':'question'},'question_ending_requires_editor'),
    ({'blind':{'clear_to_new_viewer':'uncertain'}},'blind_clear_to_new_viewer_uncertain'),
    ({'judged_by':{'blind':{'fallback_used':True}}},'backup_judge_requires_editor'),
])
def test_fixed_editor_tier_rules(change,reason):
    row={'status':'ai_eligible','duration_ms':30000,'warnings':[],'blind':{},'context':{},'cliffhanger_type':'threat',**change}
    result=apply_editor_policy(row)
    assert result['status']=='editor_review' and reason in result['editor_review_reasons']


def test_gate_cannot_be_overridden_by_editor_flags():
    result=apply_editor_policy({'status':'ai_eligible','duration_ms':30000,'warnings':['partial_camera_record'],'cliffhanger_type':'none'})
    assert result['status']=='rejected' and result['editor_tier']=='rejected'


def test_coverage_resume_does_not_repeat_successful_reviews(tmp_path):
    c,eps=setup(3);c['batch'].update(coverage_policy='coverage_first_v2',blind_global_limit=3,stream_preview_limit=0)
    api=Passing();first=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    before=deepcopy(api.calls)
    again=run_batch(tmp_path,eps,'x',c,tmp_path/'batch',api)
    assert api.calls==before
    assert first['coverage_pass']['reviewed_episodes']==again['coverage_pass']['reviewed_episodes']==3


def test_larger_budget_keeps_old_reviews_and_finishes_first_pass(tmp_path):
    c,eps=setup(6);c['batch'].update(blind_global_limit=6,stream_preview_limit=0)
    c['quota']['daily_safety_reserve_tokens']=0
    class Budget:
        used=50
        def limits(self,_):return {'tokens_per_day':60}
        def snapshot(self):return {'models':{c['models']['blind']['model']:{'rolling_24h_accounted_tokens':self.used}}}
    api=Passing();api.budget=Budget();api.request_cost=lambda *args:10
    dest=tmp_path/'batch'
    first=run_batch(tmp_path,eps,'x',c,dest,api)
    assert first['coverage_pass']['reviewed_episodes']==1
    api.budget.used=0
    final=run_batch(tmp_path,eps,'x',c,dest,api)
    assert final['coverage_pass']['reviewed_episodes']==6 and final['whole_show_results']
    assert len([r for r in api.calls if r[0]=='blind'])==6
    assert len(json.loads((dest/'candidate_reviews.json').read_text()))==6


def test_backup_review_once_and_checkpoint_survives_resume(tmp_path):
    c,eps=setup(1);c['batch'].update(coverage_policy='coverage_first_v2',blind_global_limit=1)
    c['review']['blind_fallback_role']='blind_fallback'
    c['models']['blind_fallback']={**c['models']['analysis'],'prompt_version':c['models']['blind']['prompt_version']}
    calls=[];fixture=Passing()
    def transport(model,messages,schema,settings):
        calls.append(model['model'])
        if model['model']==c['models']['blind']['model']:raise ValueError('Synthetic malformed response')
        payload=json.loads(messages[1]['content'])
        result=fixture.request('blind','x','',payload,SourceBlindReview,lambda _:None)
        return {'choices':[{'finish_reason':'stop','message':{'content':result.model_dump_json()}}],'usage':{'total_tokens':100}},{}
    dest=tmp_path/'batch';api=JsonProvider(tmp_path,c,dest,transport=transport)
    # Cached analysis is synthesized independently of network calls.
    from src.analysis_planning import CombinedAnalysis
    path=dest/'episodes/E001/story_signals.json';path.parent.mkdir(parents=True)
    path.write_text(CombinedAnalysis(episode_id='E001',signals=[],limitations=[]).model_dump_json())
    first=run_batch(tmp_path,eps,'x',c,dest,api)
    assert len(calls)==3  # two primary attempts, one backup; no context for backup-only verdict
    evidence=json.loads(next((dest/'review_provenance/blind').glob('*.json')).read_text())
    assert evidence['fallback_used'] and evidence['model']==c['models']['blind_fallback']['model']
    assert first['ai_eligible']==0
    run_batch(tmp_path,eps,'x',c,dest,api)
    assert len(calls)==3
