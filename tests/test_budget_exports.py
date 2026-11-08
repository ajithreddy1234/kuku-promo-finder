from copy import deepcopy
import json
from pathlib import Path
import pytest
import yaml

from src.budget import BudgetLedger, BudgetPause, estimate_request
from src.candidates import Candidate, make_candidate
from src.exports import enrich_review, write_review_exports
from src.ranking import rank_candidates
from src.parser import parse_text
from src.thin_slice import analysis_package, episode_package
from src.analysis_planning import plan_analysis_parts

ROOT=Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):self.value=100000.;self.sleeps=[]
    def now(self):return self.value
    def sleep(self,seconds):self.sleeps.append(seconds);self.value+=seconds


def budget_config():
    limits={'tokens_per_minute':100,'tokens_per_day':1000,'requests_per_minute':10,'requests_per_day':100}
    return {'quota':{'organization_namespace':'same_org','daily_safety_reserve_tokens':0,
                     'max_pacing_wait_seconds':55,'models':{'A':limits,'B':dict(limits)}}}


def test_org_model_accounting_does_not_reset_by_role(tmp_path):
    clock=Clock();config=budget_config()
    a=BudgetLedger(tmp_path,config,clock.now,clock.sleep)
    r=a.reserve('A',{'reserved_tokens':60});a.settle(r,40)
    b=BudgetLedger(tmp_path,config,clock.now,clock.sleep)
    with pytest.raises(BudgetPause):b.reserve('A',{'reserved_tokens':61})
    b.reserve('B',{'reserved_tokens':60})
    assert b.snapshot()['models']['A']['rolling_24h_accounted_tokens']==40


def test_token_pacing_waits_only_when_within_bounded_wait(tmp_path):
    clock=Clock();ledger=BudgetLedger(tmp_path,budget_config(),clock.now,clock.sleep)
    r=ledger.reserve('A',{'reserved_tokens':60});ledger.settle(r,40)
    clock.value+=10
    ledger.reserve('A',{'reserved_tokens':61})
    assert len(clock.sleeps)==1 and 50<clock.sleeps[0]<51


def test_daily_rollover_does_not_grant_a_second_budget(tmp_path):
    clock=Clock();clock.value=86390
    config=budget_config();config['quota']['models']['A']['tokens_per_day']=80
    ledger=BudgetLedger(tmp_path,config,clock.now,clock.sleep)
    r=ledger.reserve('A',{'reserved_tokens':60});ledger.settle(r,60)
    clock.value+=70 # UTC day changed, rolling daily usage must still count
    with pytest.raises(BudgetPause):ledger.reserve('A',{'reserved_tokens':30})
    clock.value+=86400
    ledger.reserve('A',{'reserved_tokens':30})


def test_unknown_usage_charged_conservatively_and_settle_idempotent(tmp_path):
    clock=Clock();ledger=BudgetLedger(tmp_path,budget_config(),clock.now,clock.sleep)
    r=ledger.reserve('A',{'reserved_tokens':50});ledger.settle(r,None);ledger.settle(r,10)
    row=ledger.snapshot()['models']['A']
    assert row['rolling_24h_accounted_tokens']==50 and row['estimated_rows']==1


def test_history_import_is_idempotent_and_cache_hits_are_free(tmp_path):
    folder=tmp_path/'runs/old';folder.mkdir(parents=True)
    (folder/'usage.jsonl').write_text(json.dumps({'role':'analysis','model':'A','api_calls':1,'total_tokens':40})+'\n'+
       json.dumps({'role':'blind','model':'A','api_calls':0,'cache_hit':True})+'\n'+
       json.dumps({'role':'analysis','model':'A','provider':'ollama','api_calls':1,'total_tokens':900})+'\n')
    clock=Clock();ledger=BudgetLedger(tmp_path,budget_config(),clock.now,clock.sleep)
    ledger.import_history();ledger.import_history()
    assert ledger.snapshot()['models']['A']['rolling_24h_requests']==1
    assert ledger.snapshot()['models']['A']['rolling_24h_accounted_tokens']==40


def test_single_oversized_request_is_not_sent(tmp_path):
    ledger=BudgetLedger(tmp_path,budget_config())
    with pytest.raises(BudgetPause,match='Single request'):ledger.reserve('A',{'reserved_tokens':101})
    assert ledger.snapshot()['models']['A']['rolling_24h_requests']==0


def test_compact_analysis_preserves_every_camera_word_and_source_id():
    p=ROOT/'data/examples/synthetic-episode-001.txt';ep=parse_text(p.read_bytes(),p.name)
    full=episode_package(ep);compact=analysis_package(ep,'compact_rows_v1')
    restored=[dict(zip(compact['columns'],r)) for r in compact['records']]
    assert restored==full['events']
    assert len(json.dumps(compact))<len(json.dumps(full))


def sample_row(ep,candidate,score=7.2):
    dialogue=[e.event_id for e in ep.events if e.event_type=='dialogue' and candidate.start_ms<=e.start_ms and e.end_ms<=candidate.end_ms]
    return {'candidate_id':candidate.candidate_id,'episode_id':ep.episode_id,'start_ms':candidate.start_ms,
        'end_ms':candidate.end_ms,'duration_ms':candidate.duration_ms,'score':score,'status':'eligible',
        'human_reviewed':False,'hook_evidence_ids':[dialogue[0]],'cliffhanger_evidence_ids':[dialogue[-1]],'warnings':[],
        'blind':{'hook_score':7,'cliffhanger_score':7,'standalone_clarity_score':8,'emotional_stakes_score':6,
                 'clear_to_new_viewer':'yes','hook_within_first_3s':'yes','ending_resolved':'no'},
        'context':{'hook_factually_supported':'yes','cliffhanger_factually_supported':'yes','misleading_cut':'no',
                   'essential_context_missing':'no','source_supported':'yes'}}


def test_export_exact_quotes_duration_flags_and_ai_status(tmp_path):
    p=ROOT/'data/examples/synthetic-episode-001.txt';ep=parse_text(p.read_bytes(),p.name)
    candidate=make_candidate(ep,0,30000)
    row=enrich_review(sample_row(ep,candidate),candidate,ep,{'config_hash':'x','models':{},'complete':False})
    assert row['status']=='ai_eligible' and row['hook_quote']==ep.events[1].text
    assert row['hook_time']=='00:00:00.000' and row['cliffhanger_type']=='not_assessed'
    assert row['score_raw']==7.2 and not row['human_reviewed']
    write_review_exports(tmp_path,[row])
    header=(tmp_path/'candidate_reviews.csv').read_text().splitlines()[0]
    assert 'standalone_clarity_score' in header and 'model_blind' in header and 'ending_resolved' in header


def test_legacy_eligibility_not_silently_upgraded():
    p=ROOT/'data/examples/synthetic-episode-001.txt';ep=parse_text(p.read_bytes(),p.name)
    candidate=make_candidate(ep,0,30000);row=sample_row(ep,candidate)
    row['context']={}
    assert enrich_review(row,candidate,ep,{})['status']=='editor_review'


def rank_row(identity,episode,score,start=0):
    return {'candidate_id':identity,'episode_id':episode,'score':score,'score_raw':score,
            'start_ms':start,'end_ms':start+30000,'status':'ai_eligible','config_hash':'same',
            'standalone_clarity_score':8,'warnings':[],'duration_margin_ms':10000}


def test_near_ties_share_group_and_posting_prefers_episode_variety():
    rows=[rank_row('a','E001',7.2),rank_row('b','E001',7.15,40000),rank_row('c','E002',6.95)]
    ranked,posting=rank_candidates(rows)
    assert {r['priority_group'] for r in ranked}=={1}
    assert posting[0]['episode_id']!=posting[1]['episode_id']
    assert all(r.get('post_first_reason') for r in posting)


def test_rejected_and_duplicate_candidates_cannot_fill_shortlist():
    rows=[rank_row('a','E001',8),rank_row('b','E001',7.9,100)]
    rejected=rank_row('c','E002',10);rejected['status']='rejected';rows.append(rejected)
    ranked,_=rank_candidates(rows)
    assert len(ranked)==1 and ranked[0]['candidate_id']=='a'


def test_mixed_prompt_runs_cannot_be_merged_into_ranking():
    rows=[rank_row('a','E001',8),rank_row('b','E002',7)]
    rows[1]['config_hash']='different'
    with pytest.raises(ValueError):rank_candidates(rows)


def test_chunk_planner_preserves_all_records_under_token_limit():
    p=ROOT/'data/examples/synthetic-episode-001.txt';ep=parse_text(p.read_bytes(),p.name)
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    full=plan_analysis_parts(ep,config)
    ceiling=full[0]['estimate']['reserved_tokens']-100
    config['quota']['models'][config['models']['analysis']['model']]['tokens_per_minute']=ceiling
    parts=plan_analysis_parts(ep,config)
    assert len(parts)>1
    assert {part.get('anchor_map',{}).get(r[0],r[0]):r[-1] for part in parts for r in part['payload']['records']}=={e.event_id:e.text for e in ep.events}
    assert all(p['estimate']['reserved_tokens']<=ceiling for p in parts)


def test_giant_indivisible_record_is_not_silently_truncated():
    p=ROOT/'data/examples/synthetic-episode-001.txt';ep=parse_text(p.read_bytes(),p.name)
    ep.events[0].text='x'*50000
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    with pytest.raises(BudgetPause):plan_analysis_parts(ep,config)
    assert len(ep.events[0].text)==50000
