from pathlib import Path
import json
import shutil
import pytest
import yaml
from fastapi.testclient import TestClient
import api
from src import workspace as ws

REAL=Path(__file__).resolve().parents[1]
H={'X-Promo-Client':'studio'}

@pytest.fixture
def client(tmp_path,monkeypatch):
    shutil.copy2(REAL/'config.yaml',tmp_path/'config.yaml')
    (tmp_path/'runs/workspaces').mkdir(parents=True)
    monkeypatch.setattr(ws,'ROOT',tmp_path);monkeypatch.setattr(ws,'WORKSPACES',tmp_path/'runs/workspaces')
    return TestClient(api.app)


def upload(client,text=None,filename='episode-73.srt'):
    source=text or '1\n00:00:00,000 --> 00:00:25,000\nAVA: Someone is inside.\n\n2\n00:00:25,000 --> 00:00:50,000\nLEO: The signal is getting closer.\n'
    r=client.post('/api/projects',headers=H,data={'title':'An unrelated show'},files={'files':(filename,source.encode())})
    assert r.status_code==200,r.text
    return r.json()


def test_upload_uses_detected_episodes_not_assignment_numbers(client):
    p=upload(client)
    assert p['selected_episodes']==[73] and p['episodes'][0]['number']==73
    assert p['ingestion']['complete_ingestion']
    candidates=client.get(f"/api/projects/{p['id']}/candidates").json()
    assert candidates and all(c['status']=='unreviewed' for c in candidates)
    assert all(c['segments'][0]['episode_id']=='E073' for c in candidates)
    csv=client.get(f"/api/projects/{p['id']}/export/shortlist-csv")
    assert len(csv.text.splitlines())==1  # no fake AI recommendations


def test_offline_eligibility_correction_is_consistent_without_rewriting_judgments(client):
    p=upload(client);pid=p['id']
    original=client.get(f'/api/projects/{pid}/candidates').json()[0]
    batch=ws.project_path(pid)/'batch';batch.mkdir()
    row={**original,'status':'ai_eligible'}
    (batch/'candidate_reviews.json').write_text(json.dumps([row]))
    (batch/'batch_report.json').write_text(json.dumps({'ai_eligible':1}))
    corrected=batch/'eligibility_rechecks/v2';corrected.mkdir(parents=True)
    (corrected/'candidate_reviews.json').write_text(json.dumps([{**row,'status':'rejected'}]))
    (corrected/'correction.json').write_text(json.dumps({'corrected_recommendations':0,'policy_version':'v2-require-cliffhanger'}))
    (corrected/'promos.csv').write_text('candidate_id,status\n')
    meta=client.get(f'/api/projects/{pid}').json()['batch']
    assert meta['ai_eligible']==0 and meta['original_ai_eligible']==1
    values=client.get(f'/api/projects/{pid}/candidates').json()
    assert next(r for r in values if r['candidate_id']==row['candidate_id'])['status']=='rejected'
    assert client.get(f'/api/projects/{pid}/export/shortlist-json').json()==[]
    assert client.get(f'/api/projects/{pid}/export/shortlist-csv').text=='candidate_id,status\n'
    assert json.loads((batch/'candidate_reviews.json').read_text())[0]['status']=='ai_eligible'


def test_exact_original_reference_remains_protected_even_when_reuploaded(client):
    raw='AVA:\nStart: 00:00.000 | End: 00:25.000\nThis is a synthetic locked source.\n'
    import hashlib
    private=ws.ROOT/'evaluation/private';private.mkdir(parents=True)
    (private/'human_references.json').write_text(json.dumps({'entries':[{'source_sha256':hashlib.sha256(raw.encode()).hexdigest()}]}))
    p=upload(client,raw,'another-name.txt')
    assert p['selected_episodes']==[] and p['episodes'][0]['reference_reserved']
    assert client.get(f"/api/projects/{p['id']}/candidates").json()==[]


def test_coverage_reservation_is_source_bound_and_cannot_be_overridden(client):
    import hashlib
    config=yaml.safe_load((ws.ROOT/'config.yaml').read_text());config['batch']['respect_evaluation_reservations']=True
    (ws.ROOT/'config.yaml').write_text(yaml.safe_dump(config))
    text='1\n00:00:00,000 --> 00:00:25,000\nAVA: A reserved source.\n'
    private=ws.ROOT/'evaluation/private';private.mkdir(parents=True)
    (private/'coverage_reservations.json').write_text(json.dumps({'entries':[{
        'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'reason':'Exposed; excluded, not an independent reference'}]}))
    p=upload(client,text,'episode-9.srt')
    assert p['selected_episodes']==[] and p['episodes'][0]['reference_reserved']
    assert client.get(f"/api/projects/{p['id']}/candidates").json()==[]
    (ws.ROOT/'.env').write_text('GROQ_API_KEY=synthetic-not-a-key\n')
    result=client.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[9]})
    assert result.status_code==400
    unrelated=upload(client,filename='episode-9.srt')
    assert unrelated['selected_episodes']==[9] and not unrelated['episodes'][0]['reference_reserved']


def test_untimed_upload_gives_actionable_report_and_no_candidates(client):
    p=upload(client,'An untimed story with dialogue.','story.txt')
    assert p['episodes']==[] and p['ingestion']['files'][0]['code']=='timestamps_required'
    assert client.get(f"/api/projects/{p['id']}/candidates").json()==[]


def test_editor_notes_are_separate_from_ai_status(client):
    p=upload(client);pid=p['id'];c=client.get(f'/api/projects/{pid}/candidates').json()[0]
    r=client.post(f"/api/projects/{pid}/candidates/{c['candidate_id']}/review",headers=H,
        json={'evaluator':'Synthetic test only','verdict':'edit','reason':'Automated fixture: a cut needs a different ending.'})
    assert r.status_code==200 and r.json()['independent_reference'] is False
    row=client.get(f'/api/projects/{pid}/candidates').json()[0]
    assert row['status']=='unreviewed' and row['editor_note']['verdict']=='edit'


def test_local_mutation_header_and_project_path_boundaries(client):
    assert client.post('/api/demo').status_code==403
    assert client.post('/api/demo',headers={**H,'origin':'https://unrelated.example'}).status_code==403
    assert client.get('/api/projects/not-a-valid-id').status_code==404


def test_live_job_requires_key_and_never_runs_on_import(client):
    p=upload(client)
    assert not (ws.project_path(p['id'])/'batch').exists()
    r=client.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[73]})
    assert r.status_code==400 and 'key' in r.json()['detail']


def test_new_script_reference_example_is_not_claimed_independent(client):
    p=upload(client);pid=p['id']
    r=client.post(f'/api/projects/{pid}/references',headers=H,json={'episode_id':'E073','decision':'selected','start_ms':0,'end_ms':25000,'reason':'Automated synthetic fixture, not a real human label.'})
    assert r.status_code==200 and r.json()['independent_before_system_outputs'] is False
    assert r.json()['purpose']=='optional_human_examples'


def test_revising_a_cut_clears_scores_and_keeps_original_review(client):
    p=upload(client);pid=p['id'];original=client.get(f'/api/projects/{pid}/candidates').json()[0]
    client.post(f"/api/projects/{pid}/candidates/{original['candidate_id']}/review",headers=H,
        json={'evaluator':'Synthetic test','verdict':'accept','reason':'Automated synthetic fixture, not a real promo approval.'})
    r=client.post(f"/api/projects/{pid}/candidates/{original['candidate_id']}/revise",headers=H,
        json={'start_ms':0,'end_ms':50000,'reason':'Synthetic boundary change for testing.'})
    assert r.status_code==200,r.text
    changed=r.json()
    assert changed['candidate_id']!=original['candidate_id'] and changed['candidate_version']==2
    assert changed['status']=='unreviewed' and changed['blind']=={} and changed['context']=={}
    rows=client.get(f'/api/projects/{pid}/candidates').json()
    revised=next(x for x in rows if x['candidate_id']==changed['candidate_id'])
    assert revised['editor_note'] is None
    assert next(x for x in rows if x['candidate_id']==original['candidate_id'])['editor_note']['verdict']=='accept'


def test_cut_revision_cannot_slice_through_dialogue(client):
    p=upload(client);pid=p['id'];c=client.get(f'/api/projects/{pid}/candidates').json()[0]
    r=client.post(f"/api/projects/{pid}/candidates/{c['candidate_id']}/revise",headers=H,
        json={'start_ms':1000,'end_ms':50000,'reason':'Synthetic unsafe cut.'})
    assert r.status_code==400 and 'boundary' in r.json()['detail']


def test_explicit_job_endpoint_passes_new_project_selection_without_network(client,monkeypatch):
    p=upload(client);(ws.ROOT/'.env').write_text('GROQ_API_KEY=synthetic-test-value-not-a-real-key\n')
    calls=[];monkeypatch.setattr(ws,'start_job',lambda pid,selected:calls.append((pid,selected)))
    r=client.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[73]})
    assert r.status_code==200 and calls==[(p['id'],[73])]


def test_fresh_upload_never_preloads_old_ai_results(client):
    p=upload(client)
    assert p['batch'] is None and p['job'] is None
    rows=client.get(f"/api/projects/{p['id']}/candidates").json()
    assert rows and all(r['status']=='unreviewed' for r in rows)
    assert not (ws.ROOT/'.env').exists()
    assert client.post('/api/demo',headers=H).status_code==404


def test_new_project_examples_cannot_start_a_false_independent_evaluation(client):
    p=upload(client)
    r=client.post(f"/api/projects/{p['id']}/evaluation",headers=H)
    assert r.status_code==400 and 'reserved' in r.json()['detail']


def test_empty_app_has_no_previous_assignment_or_demo(client):
    assert client.get('/api/projects').json()==[]
    assert client.get('/api/projects/assignment').status_code==404
    assert client.post('/api/demo',headers=H).status_code==404


def test_upload_defaults_to_all_usable_episodes_and_has_offline_plan(client):
    raw='1\n00:00:00,000 --> 00:00:25,000\nAVA: Someone is inside.\n'
    files=[('files',(f'episode-{n}.srt',raw.encode())) for n in range(1,10)]
    r=client.post('/api/projects',headers=H,data={'title':'Nine episodes'},files=files)
    assert r.status_code==200
    p=r.json();assert p['selected_episodes']==list(range(1,10))
    plan=client.post(f"/api/projects/{p['id']}/plan",headers=H,json={'episode_numbers':list(range(1,10))})
    assert plan.status_code==200 and plan.json()['api_calls']==0
    assert plan.json()['analysis_requests']==9
    assert not (ws.project_path(p['id'])/'batch').exists()


def test_new_run_preserves_original_results_and_selects_all_again(client):
    p=upload(client);original=ws.project_path(p['id']);batch=original/'batch';batch.mkdir()
    (batch/'sentinel.json').write_text('{"unchanged":true}')
    r=client.post(f"/api/projects/{p['id']}/new-run",headers=H)
    assert r.status_code==200
    fresh=r.json();assert fresh['id']!=p['id'] and fresh['batch'] is None
    assert fresh['selected_episodes']==[73] and fresh['pipeline_version']==2
    assert json.loads((batch/'sentinel.json').read_text())=={'unchanged':True}


def test_local_mode_does_not_require_a_groq_key(client,monkeypatch):
    p=upload(client);calls=[]
    monkeypatch.setattr(ws,'start_job',lambda *args:calls.append(args))
    r=client.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[73],'profile':'local','local_model':'installed:8b'})
    assert r.status_code==200 and calls==[(p['id'],[73],'local','installed:8b')]


def test_private_raw_export_contains_source_and_results_but_no_environment(client):
    import zipfile
    from io import BytesIO
    p=upload(client);folder=ws.project_path(p['id']);(folder/'.env').write_text('DO_NOT_INCLUDE=private\n')
    batch=folder/'batch';batch.mkdir();(batch/'promos.json').write_text('[]')
    response=client.get(f"/api/projects/{p['id']}/export/raw-outputs")
    assert response.status_code==200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert 'episodes.jsonl' in archive.namelist() and 'batch/promos.json' in archive.namelist()
        assert not any('.env' in name for name in archive.namelist())


def test_folder_upload_preserves_distinct_relative_filenames(client):
    raw='1\n00:00:00,000 --> 00:00:25,000\nAVA: Someone is inside.\n'
    files=[('files',(f'show/episode-{n}/script.srt',raw.encode())) for n in (10,32)]
    response=client.post('/api/projects',headers=H,files=files)
    assert response.status_code==200 and response.json()['selected_episodes']==[10,32]
    assert all(e['filename'].startswith('show/episode-') for e in response.json()['episodes'])


def test_folder_upload_rejects_parent_path_escape(client):
    response=client.post('/api/projects',headers=H,files={'files':('../escape.srt',b'not used')})
    assert response.status_code==400
