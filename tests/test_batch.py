import json
from pathlib import Path
import pytest
import yaml
from src.batch import run_batch
from src.parser import parse_text
from src.provider import ProviderFailure
from src.budget import BudgetPause
from src.review_models import StoryAnalysis,BlindReview

ROOT=Path(__file__).resolve().parents[1]


def setup():
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    config['batch']['ai_episode_numbers']=[1,3]
    config['evaluation'].update(held_out_episode_numbers=[2],primary_reference_episode_numbers=[2],
        development_episode_numbers=[1],tuning_episode_numbers=[3])
    source=(ROOT/'data/examples/synthetic-episode-001.txt').read_text()
    episodes=[parse_text(source.replace('Episode 1',f'Episode {n}').encode(),f'episode-{n:03d}.txt') for n in (1,2,3)]
    return config,episodes


class RejectingProvider:
    def __init__(self,pause=False):self.calls=[];self.pause=pause
    def request(self,role,stage,system,payload,response_model,validate):
        self.calls.append((role,payload.get('episode_id')))
        if role=='analysis':
            if self.pause and payload['episode_id']=='E003':
                self.pause=False
                raise BudgetPause('Synthetic pause',1)
            value=StoryAnalysis(episode_id=payload['episode_id'],signals=[],limitations=[])
        elif role=='blind':
            extra={'threat_target_quote':None,'threat_target_evidence_id':None} if response_model.__name__=='SourceBlindReview' else {}
            value=response_model(clip_id='clip',clear_to_new_viewer='yes',hook_within_first_3s='yes',ending_resolved='yes',
                hook_score=0,cliffhanger_score=0,standalone_clarity_score=0,emotional_stakes_score=0,
                hook='Synthetic test',cliffhanger='None',why_it_works='Does not work',hook_evidence_ids=[],
                cliffhanger_evidence_ids=[],uncertainty_reasons=[],**extra)
        else:raise AssertionError('Do not spend context calls on blind-rejected candidates')
        validate(value)
        return value


def test_full_pack_accounting_does_not_claim_full_ai_coverage(tmp_path):
    config,episodes=setup();api=RejectingProvider()
    result=run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',api)
    assert result['status']=='complete_subset' and result['total_episodes']==3
    assert result['ai_analyzed_episodes']==2 and result['blind_reviewed']==4 and result['context_reviewed']==0
    assert result['coverage'][1]['scope_status']=='held_out_locked'
    assert json.loads((tmp_path/'batch/episodes/E002/deterministic_unreviewed.json').read_text())==[]
    assert json.loads((tmp_path/'batch/promos.json').read_text())==[]


def test_resume_does_not_repeat_completed_analysis(tmp_path):
    config,episodes=setup();api=RejectingProvider(pause=True)
    first=run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',api)
    assert first['status']=='paused_budget' and first['ai_analyzed_episodes']==1
    final=run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',api)
    assert final['status']=='complete_subset'
    assert api.calls.count(('analysis','E001'))==1


def test_batch_cannot_include_held_out_episode(tmp_path):
    config,episodes=setup();config['batch']['ai_episode_numbers']=[1,2]
    with pytest.raises(ValueError):run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',RejectingProvider())


def test_resume_config_change_does_not_overwrite_manifest(tmp_path):
    config,episodes=setup();api=RejectingProvider()
    run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',api)
    manifest=(tmp_path/'batch/manifest.json').read_text()
    config['batch']['context_limit']=1
    with pytest.raises(ValueError):run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',api)
    assert (tmp_path/'batch/manifest.json').read_text()==manifest


def test_failed_blind_response_never_becomes_recommendation(tmp_path):
    from src.provider import RequestExhausted
    class Failing(RejectingProvider):
        def request(self,role,*args,**kwargs):
            if role=='blind':raise RequestExhausted('Synthetic malformed response')
            return super().request(role,*args,**kwargs)
    config,episodes=setup();config['review']['blind_fallback_role']=None
    result=run_batch(tmp_path,episodes,'source',config,tmp_path/'batch',Failing())
    assert result['status']=='complete_subset' and result['failed_reviews']==4
    assert result['blind_reviewed']==0 and result['ai_eligible']==0
    assert json.loads((tmp_path/'batch/promos.json').read_text())==[]


def test_empty_manual_profile_detects_new_episode_numbers(tmp_path):
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    assert config['batch']['ai_episode_numbers']==[]
    source=(ROOT/'data/examples/synthetic-episode-001.txt').read_text()
    episodes=[parse_text(source.replace('Episode 1',f'Episode {n}').encode(),f'episode-{n}.txt') for n in (73,81)]
    result=run_batch(tmp_path,episodes,'new-input',config,tmp_path/'batch',RejectingProvider())
    assert result['status']=='complete_subset'
    assert result['ai_episode_numbers']==[73,81]
    assert result['ai_analyzed_episodes']==2
