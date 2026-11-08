from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
import yaml

from src.parser import parse_text
from src.models import Episode
from src.boundaries import usable_boundaries, interval_iou, union_duration
from src.timeline import reconstruct, render_timeline, format_time
from src.candidates import (StorySignal, make_candidate, revise_candidate, sliding_windows,
                             semantic_candidates, assemble, prune, select_baseline)
from src.deterministic import run_deterministic, load_signals
from src.evaluation import (reference_template, validate_references, require_held_out_access,
                            config_hash, split_sets, ReferenceSelection)

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def ep():
    path=ROOT/'data/examples/synthetic-episode-001.txt'
    return parse_text(path.read_bytes(),path.name)


@pytest.fixture
def signals():
    return load_signals(ROOT/'data/examples/synthetic-signals.json')


@pytest.fixture
def config():
    c=yaml.safe_load((ROOT/'config.yaml').read_text())
    c['evaluation'].update(development_episode_numbers=[1,6,12],tuning_episode_numbers=[3,25,50],held_out_episode_numbers=[2,9,18,27,36,45],primary_reference_episode_numbers=[2,18,36])
    return c


def test_source_boundary_not_camera_cut_inside_speech(ep):
    camera=ep.events[0].model_copy(update={'event_id':'CAM2','start_ms':2000,'end_ms':7000,'source_order':99})
    ep.events.append(camera)
    points=usable_boundaries(ep)
    assert 2000 not in points and 7000 not in points
    assert 0 in points and 3000 in points


def test_action_is_protected(ep):
    assert 31000 not in usable_boundaries(ep)
    with pytest.raises(ValueError): make_candidate(ep,31000,55000)


def test_partial_visual_is_not_presented_as_full_content(ep):
    clip=reconstruct(ep,20000,52000)
    camera=clip[0]
    assert camera.event_type=='camera' and camera.content_status=='partial_unknown'
    assert camera.text is None and camera.clip_start_ms==0 and camera.clip_end_ms==32000
    assert all(e.text is not None for e in clip if e.content_status=='full')


def test_partial_speech_has_no_fabricated_words(ep):
    clip=reconstruct(ep,1000,25000)
    speech=next(e for e in clip if e.event_type=='dialogue')
    assert speech.text is None and speech.content_status=='partial_unknown'


def test_endpoint_events_are_excluded(ep):
    clip=reconstruct(ep,20000,25000)
    assert not any(e.source_start_ms==25000 for e in clip)
    assert not any(e.source_end_ms==20000 for e in clip)


def test_renderer_is_temporal_and_excludes_metadata(ep):
    ep.events.reverse()
    text=render_timeline(ep)
    assert text.index('E001_V0002')<text.index('E001_V0007')
    assert 'Invented software fixture' not in text


def test_format_and_union_do_not_sum_overlap():
    assert format_time(3723005)=='01:02:03.005'
    assert union_duration([(0,5000),(1000,3000),(4000,7000)])==7000
    assert interval_iou(0,30,10,40)==0.5
    assert interval_iou(0,10,10,20)==0


@pytest.mark.parametrize('start,end',[(0,19000),(0,65000),(1000,30000),(0,30001),(60000,50000)])
def test_invalid_or_unsupported_cuts_are_rejected(ep,start,end):
    with pytest.raises(ValueError): make_candidate(ep,start,end)


def test_timing_quarantine_blocks_all_candidates(ep):
    ep.scenes[0].end_ms=69000
    assert usable_boundaries(ep)==[]
    assert sliding_windows(ep)==[]
    with pytest.raises(ValueError): make_candidate(ep,0,30000)


def test_derived_timing_is_not_promoted(ep):
    ep.events[0].timing_status='derived'
    assert not usable_boundaries(ep)


def test_reconstruction_checks_interval_bounds(ep):
    with pytest.raises(ValueError): reconstruct(ep,-1,20000)
    with pytest.raises(ValueError): reconstruct(ep,0,71000)


def test_candidates_include_all_tracks_and_no_score(ep):
    candidate=make_candidate(ep,0,30000)
    assert candidate.duration_ms==30000
    assert 'E001_V0001' in candidate.selected_event_ids
    assert candidate.statistics['dialogue_coverage_ms']<=candidate.duration_ms
    assert candidate.status=='unreviewed'
    assert 'score' not in candidate.model_dump()


def test_changed_cut_invalidates_identity_and_rebuilds_content(ep):
    before=make_candidate(ep,0,30000)
    after=revise_candidate(ep,before,0,39000)
    assert after.candidate_id==before.candidate_id
    assert after.candidate_version==before.candidate_version+1
    assert after.candidate_hash!=before.candidate_hash
    assert len(after.selected_content)>len(before.selected_content)
    assert after.status=='unreviewed'


def test_source_change_changes_candidate_hash(ep):
    old=make_candidate(ep,0,30000)
    ep.events[1].text='A changed synthetic sentence.'
    assert make_candidate(ep,0,30000).candidate_hash!=old.candidate_hash


def test_sliding_windows_valid_and_unique(ep):
    windows=sliding_windows(ep)
    assert windows
    assert len({(c.start_ms,c.end_ms) for c in windows})==len(windows)
    assert all(20000<=c.duration_ms<=60000 for c in windows)
    assert all(c.source=='baseline' for c in windows)


def test_semantic_candidates_use_existing_evidence(ep,signals):
    candidates=semantic_candidates(ep,signals)
    assert candidates and all(c.source=='semantic' for c in candidates)
    assert all(c.signal_ids for c in candidates)


def test_unknown_signal_is_rejected_not_silently_dropped(ep,signals):
    signals[0].source_event_ids=['INVENTED']
    with pytest.raises(ValueError): assemble(ep,signals)


def test_cross_episode_evidence_is_rejected(ep,signals):
    signals[0].episode_id='E999'
    with pytest.raises(ValueError): semantic_candidates(ep,signals)


def test_duplicate_signal_identity_rejected(ep,signals):
    with pytest.raises(ValueError): assemble(ep,signals+[signals[0]])


def test_budget_roles_and_reproducibility(ep,signals):
    a=assemble(ep,signals)
    b=assemble(ep,signals)
    assert len(a['raw'])<=12 and len(a['retained'])<=5 and len(a['review_queue'])<=3
    assert any(c.source=='semantic' for c in a['review_queue'])
    assert any(c.source=='wildcard' for c in a['review_queue'])
    assert [c.candidate_hash for c in a['review_queue']]==[c.candidate_hash for c in b['review_queue']]
    assert all('start_ms' in row and 'selected_event_ids' in row for row in a['ledger'])
    assert len(a['ledger'])==a['enumerated']
    assert any(row['status']=='not_reviewed' for row in a['ledger'])


def test_review_pool_does_not_duplicate_intervals(ep,signals):
    a=assemble(ep,signals)
    for c in a['review_queue']:
        assert c in a['retained']
    assert len({(c.start_ms,c.end_ms) for c in a['review_queue']})==len(a['review_queue'])


def test_baseline_selection_ignores_semantic_signals(ep,signals):
    with_signals=assemble(ep,signals)['baseline_queue']
    without=assemble(ep,[])['baseline_queue']
    assert [c.candidate_hash for c in with_signals]==[c.candidate_hash for c in without]
    assert all(c.source=='baseline' for c in with_signals)


@pytest.mark.parametrize('budgets',[(0,5,3),(12,2,3),(5,6,3)])
def test_invalid_budgets_rejected(ep,budgets):
    with pytest.raises(ValueError): assemble(ep,[],*budgets)


def test_no_signals_is_explicit_window_only_not_ai_result(ep):
    a=assemble(ep,[])
    assert a['semantic_signal_count']==0 and a['mode']=='deterministic_unreviewed'
    assert all(c.source=='wildcard' for c in a['review_queue'])


def test_split_overlap_rejected(config):
    config['evaluation']['tuning_episode_numbers'].append(1)
    with pytest.raises(ValueError): split_sets(config)


def test_hash_is_canonical_and_settings_changes_invalidate(config):
    before=config_hash(config)
    assert config_hash(dict(reversed(list(config.items()))))==before
    config['candidates']['blind_reviewed_per_episode']+=1
    assert config_hash(config)!=before


def held_out_fixture(ep,config):
    config['evaluation']['held_out_episode_numbers']=[1]
    config['evaluation']['primary_reference_episode_numbers']=[1]
    config['evaluation']['development_episode_numbers']=[6]
    return reference_template([ep],'packhash',config)


def complete_bundle(bundle):
    bundle.evaluator='Independent human'
    bundle.independent_before_system_outputs=True
    bundle.recorded_at=datetime.now(timezone.utc)
    bundle.entries[0].decision='none'
    bundle.entries[0].reason='No viable moment selected by the human.'


def test_reference_template_does_not_fabricate_labels(ep,config):
    bundle=held_out_fixture(ep,config)
    assert not bundle.independent_before_system_outputs and bundle.entries[0].decision=='pending'
    with pytest.raises(ValueError): validate_references(bundle,[ep],'packhash',config)


def test_missing_reference_episode_is_error(ep,config):
    with pytest.raises(ValueError): reference_template([ep],'packhash',config)


def test_held_out_requires_real_labels_and_config_freeze(ep,config):
    bundle=held_out_fixture(ep,config)
    with pytest.raises(ValueError): require_held_out_access(ep,[ep],'packhash',config)
    complete_bundle(bundle)
    with pytest.raises(ValueError): require_held_out_access(ep,[ep],'packhash',config,bundle,'oldhash')
    require_held_out_access(ep,[ep],'packhash',config,bundle,config_hash(config))
    config['candidates']['blind_reviewed_per_episode']=2
    with pytest.raises(ValueError): require_held_out_access(ep,[ep],'packhash',config,bundle,'oldhash')


def test_reference_checks_source_and_decisions(ep,config):
    bundle=held_out_fixture(ep,config)
    complete_bundle(bundle)
    validate_references(bundle,[ep],'packhash',config)
    bundle.entries[0].source_sha256='wrong'
    with pytest.raises(ValueError): validate_references(bundle,[ep],'packhash',config)


def test_reference_no_selection_needs_reason(ep,config):
    bundle=held_out_fixture(ep,config)
    complete_bundle(bundle)
    bundle.entries[0].reason=''
    with pytest.raises(ValueError): validate_references(bundle,[ep],'packhash',config)


def test_reference_rejects_future_exposure_without_attestation(ep,config):
    bundle=held_out_fixture(ep,config)
    complete_bundle(bundle)
    bundle.independent_before_system_outputs=False
    with pytest.raises(ValueError): validate_references(bundle,[ep],'packhash',config)


def test_deterministic_run_saves_unreviewed_outputs_only(tmp_path,ep,config):
    result=run_deterministic([ep],config,tmp_path/'run')
    assert result['api_calls']==0 and result['recommended_promos']==0
    assert (tmp_path/'run'/'E001'/'timeline.txt').exists()
    assert 'unreviewed' in (tmp_path/'run'/'unreviewed_candidates.csv').read_text()


def test_deterministic_stage_cannot_unblind_held_out(tmp_path,ep,config):
    held_out_fixture(ep,config)
    with pytest.raises(ValueError): run_deterministic([ep],config,tmp_path/'run',phase='held_out')
    with pytest.raises(ValueError): run_deterministic([ep],config,tmp_path/'run',phase='development')


def test_deterministic_path_does_not_import_provider_sdks(tmp_path,ep,config,monkeypatch):
    import builtins
    original=builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.split('.')[0] in ('google','groq'): raise AssertionError('No provider SDK in deterministic path')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    run_deterministic([ep],config,tmp_path/'run')


@pytest.mark.parametrize('start,end',[(0,19000),(0,65000),(60000,80000)])
def test_human_reference_intervals_must_fit_source_and_promo(ep,config,start,end):
    bundle=held_out_fixture(ep,config)
    complete_bundle(bundle)
    bundle.entries[0].decision='selected'
    bundle.entries[0].selections=[ReferenceSelection(start_ms=start,end_ms=end,why_it_works='Independent reason')]
    with pytest.raises(ValueError): validate_references(bundle,[ep],'packhash',config)


def test_human_reference_selected_is_accepted_and_duplicates_rejected(ep,config):
    bundle=held_out_fixture(ep,config)
    complete_bundle(bundle)
    bundle.entries[0].decision='selected'
    selection=ReferenceSelection(start_ms=0,end_ms=30000,why_it_works='An independent human assessment')
    bundle.entries[0].selections=[selection]
    validate_references(bundle,[ep],'packhash',config)
    bundle.entries[0].selections.append(selection)
    with pytest.raises(ValueError): validate_references(bundle,[ep],'packhash',config)


def test_unknown_pruning_policy_not_silently_ignored(tmp_path,ep,config):
    config['candidates']['pruning_policy']='unknown'
    with pytest.raises(ValueError): run_deterministic([ep],config,tmp_path/'run')


def test_changed_promo_limits_not_silently_ignored(tmp_path,ep,config):
    config['promo']['min_seconds']=10
    with pytest.raises(ValueError): run_deterministic([ep],config,tmp_path/'run')


def test_cli_generates_synthetic_pipeline_outputs_without_api(tmp_path,monkeypatch):
    import run
    dest=tmp_path/'runs'/'synthetic'
    monkeypatch.setattr(run,'__file__',str(tmp_path/'run.py'))
    monkeypatch.setattr('sys.argv',['run.py','--input',str(ROOT/'data/examples/synthetic-episode-001.txt'),
         '--signals',str(ROOT/'data/examples/synthetic-signals.json'),'--output',str(dest),
         '--config',str(ROOT/'config.yaml'),'--stage','candidates'])
    assert run.main()==0
    report=json.loads((dest/'deterministic/deterministic_report.json').read_text())
    assert report['api_calls']==0 and report['episodes'][0]['signal_origins']==['synthetic']
