import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml

from src.provider import JsonProvider, ProviderFailure, strict_schema
from src.review_models import StoryAnalysis, BlindReview, ContextReview
from src.candidates import StorySignal, semantic_candidates, make_candidate
from src.parser import parse_text
from src.thin_slice import blind_package, context_package, review_decision, run_thin_slice

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    c=yaml.safe_load((ROOT/'config.yaml').read_text())
    c['inference']['retry_limit']=1
    c['inference']['analysis_view']='compact_rows_v1' # historical thin-slice fixtures
    c['evaluation'].update(development_episode_numbers=[1,6,12],tuning_episode_numbers=[3,25,50],held_out_episode_numbers=[2,9,18,27,36,45],primary_reference_episode_numbers=[2,18,36])
    return c


@pytest.fixture
def ep():
    p=ROOT/'data/examples/synthetic-episode-001.txt'
    return parse_text(p.read_bytes(),p.name)


def blind_response(ids):
    return BlindReview(clip_id='clip',clear_to_new_viewer='yes',hook_within_first_3s='yes',
        ending_resolved='no',hook_score=8,cliffhanger_score=8,standalone_clarity_score=8,
        emotional_stakes_score=8,hook='Synthetic hook',cliffhanger='Synthetic question',
        why_it_works='Synthetic test only',hook_evidence_ids=[ids[0]],cliffhanger_evidence_ids=[ids[-1]],uncertainty_reasons=[])


def envelope(content,finish='stop'):
    return {'choices':[{'finish_reason':finish,'message':{'content':content}}],
            'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}}


def test_strict_schema_requires_defaulted_fields():
    schema=strict_schema(StoryAnalysis)
    signal=schema['$defs']['StorySignal']
    assert set(signal['required'])==set(signal['properties'])
    assert signal['additionalProperties'] is False
    assert 'default' not in signal['properties']['priority']


def test_actual_cache_identity_includes_model_prompt_and_payload(tmp_path,config):
    calls=[]
    response=StoryAnalysis(episode_id='E001',signals=[],limitations=[])
    def fake(*args):calls.append(args);return envelope(response.model_dump_json()),{}
    api=JsonProvider(tmp_path,config,tmp_path/'run',transport=fake)
    for _ in range(2):api.request('analysis','story','task',{'x':1},StoryAnalysis)
    assert len(calls)==1
    api.request('analysis','story','changed task',{'x':1},StoryAnalysis)
    config['models']['analysis']['model']='different'
    api.request('analysis','story','task',{'x':1},StoryAnalysis)
    assert len(calls)==3
    assert len((tmp_path/'run/raw_responses.jsonl').read_text().splitlines())==4


@pytest.mark.parametrize('content,finish',[('', 'stop'),('{broken', 'stop'),('{}','length')])
def test_invalid_empty_partial_output_not_cached(tmp_path,config,monkeypatch,content,finish):
    monkeypatch.setattr('src.provider.time.sleep',lambda _:None)
    api=JsonProvider(tmp_path,config,tmp_path/'run',transport=lambda *a:(envelope(content,finish),{}))
    with pytest.raises(ProviderFailure):api.request('analysis','story','task',{},StoryAnalysis)
    assert not list((tmp_path/'runs/_cache').glob('*.json'))
    assert len((tmp_path/'run/failures.jsonl').read_text().splitlines())==2


def test_rate_limit_checkpoints_without_long_wait(tmp_path,config):
    class Limited(Exception):
        status_code=429
        response=SimpleNamespace(headers={'retry-after':'100'})
        body={'error':{'code':'rate_limit_exceeded','message':'quota'}}
    def fake(*args):raise Limited()
    api=JsonProvider(tmp_path,config,tmp_path/'run',transport=fake)
    with pytest.raises(ProviderFailure):api.request('analysis','story','task',{},StoryAnalysis)
    assert len((tmp_path/'run/failures.jsonl').read_text().splitlines())==1


def test_invalid_response_attempt_cap_survives_resume(tmp_path,config,monkeypatch):
    from src.provider import RequestExhausted
    monkeypatch.setattr('src.provider.time.sleep',lambda _:None)
    calls=[]
    def bad(*args):
        calls.append(1)
        return envelope('{broken'),{}
    for _ in range(2):
        api=JsonProvider(tmp_path,config,tmp_path/'run',transport=bad)
        with pytest.raises(RequestExhausted):api.request('analysis','story','task',{},StoryAnalysis)
    assert len(calls)==2


def test_semantic_validation_rejects_valid_json(tmp_path,config,monkeypatch):
    monkeypatch.setattr('src.provider.time.sleep',lambda _:None)
    value=StoryAnalysis(episode_id='WRONG',signals=[],limitations=[])
    api=JsonProvider(tmp_path,config,tmp_path/'run',transport=lambda *a:(envelope(value.model_dump_json()),{}))
    def validate(result):
        if result.episode_id!='E001':raise ValueError('Unknown episode')
    with pytest.raises(ProviderFailure):api.request('analysis','story','task',{},StoryAnalysis,validate)
    assert not list((tmp_path/'runs/_cache').glob('*.json'))


def test_first_resolution_is_a_hard_upper_bound(ep):
    signals=[StorySignal(signal_id='H',episode_id=ep.episode_id,kind='hook',
                source_event_ids=['E001_V0002'],description='Synthetic',priority='high',
                resolution_event_ids=['E001_V0008','E001_V0012'],origin='synthetic'),
             StorySignal(signal_id='C',episode_id=ep.episode_id,kind='cliffhanger',
                source_event_ids=['E001_V0016'],description='Synthetic later ending',origin='synthetic')]
    candidates=semantic_candidates(ep,signals)
    assert candidates and all(c.end_ms<=25000 for c in candidates)


def test_resolution_before_minimum_length_does_not_force_longer_clip(ep):
    signals=[StorySignal(signal_id='H',episode_id=ep.episode_id,kind='hook',
                source_event_ids=['E001_V0002'],description='Synthetic',
                resolution_event_ids=['E001_V0004'],origin='synthetic')]
    assert semantic_candidates(ep,signals)==[]


def test_blind_package_has_no_source_scores_or_context(ep):
    c=make_candidate(ep,0,30000)
    payload,mapping=blind_package(c)
    assert set(payload)=={'clip_id','duration_ms','events'}
    assert 'E001' not in json.dumps(payload)
    assert all(e['speaker'] is None or e['speaker'].startswith('S') for e in payload['events'])
    assert mapping


def test_adjacent_held_out_opening_not_leaked(ep,config):
    adjacent=ep.model_copy(deep=True)
    adjacent.episode_number=2
    adjacent.episode_id='E002'
    for e in adjacent.events:e.text='HELD_OUT_SECRET'
    c=make_candidate(ep,35000,69000)
    payload,mapping=blind_package(c)
    review=blind_response(list(mapping))
    package,known,warnings=context_package(c,ep,[ep,adjacent],config,review,mapping)
    assert package['next_episode_opening'] is None
    assert 'HELD_OUT_SECRET' not in json.dumps(package)
    assert 'next_episode_context_withheld_heldout' in warnings


def test_high_score_cannot_override_misleading_context(ep,config):
    c=make_candidate(ep,0,30000)
    package,mapping=blind_package(c)
    ids=list(mapping)
    review=blind_response(ids)
    context=ContextReview(candidate_id=c.candidate_id,source_supported='yes',misleading_cut='yes',
        essential_context_missing='no',ending_resolved_inside_clip='no',boundary_issue='none',
        evidence_event_ids=c.selected_event_ids,reasons=['Synthetic misleading cut'])
    assert review_decision(c,review,context,mapping,config,[])['status']=='rejected'


def test_thin_slice_refuses_held_out_before_inference(ep,config,tmp_path):
    config['evaluation']['development_episode_numbers']=[6]
    config['evaluation']['held_out_episode_numbers']=[1]
    class Never:
        def request(self,*args):raise AssertionError('Must not call provider')
    with pytest.raises(ValueError):run_thin_slice(tmp_path,[ep],'hash',config,tmp_path/'run',1,Never())


def test_factually_wrong_paraphrase_cannot_pass_eligibility(ep,config):
    c=make_candidate(ep,0,30000)
    package,mapping=blind_package(c)
    review=blind_response(list(mapping))
    context=ContextReview(candidate_id=c.candidate_id,source_supported='yes',misleading_cut='no',
        essential_context_missing='no',ending_resolved_inside_clip='no',boundary_issue='none',
        evidence_event_ids=c.selected_event_ids,reasons=['Synthetic subject mismatch'],
        hook_factually_supported='yes',cliffhanger_factually_supported='no')
    assert review_decision(c,review,context,mapping,config,[])['status']=='rejected'


def test_interruption_preserves_honest_partial_checkpoint(ep,config,tmp_path):
    import src.thin_slice as module
    signals=json.loads((ROOT/'data/examples/synthetic-signals.json').read_text())
    for signal in signals:signal['origin']='model'
    story=StoryAnalysis.model_validate({'episode_id':ep.episode_id,'signals':signals,'limitations':[]})
    class StopAfterAnalysis:
        def request(self,role,stage,system,payload,response_model,validate):
            if role=='analysis':
                validate(story)
                return story
            raise ProviderFailure('Synthetic rate-limit interruption')
    destination=tmp_path/'run'
    with pytest.raises(ProviderFailure):
        module.run_thin_slice(tmp_path,[ep],'hash',config,destination,1,StopAfterAnalysis())
    report=json.loads((destination/'thin_slice_report.json').read_text())
    assert report['complete'] is False and report['reviewed']==0 and report['pending']==2
    assert json.loads((destination/'promos.json').read_text())==[]
