"""Cut-sheet delivery tests use invented scripts; no editorial claims."""
from copy import deepcopy
import json
from test_studio_api import client,upload
from src import workspace as ws
from src.cut_delivery import deliverable_cuts,cut_text


def seed(client,pid):
    row=client.get(f'/api/projects/{pid}/candidates').json()[0]
    row.update(status='editor_review',score=7,hook='Synthetic hook',cliffhanger='Synthetic pending outcome',
        why_it_works='Synthetic fixture, not a quality judgment',warnings=['partial_camera_record'],human_reviewed=False)
    row['blind']={'clear_to_new_viewer':'yes','hook_within_first_3s':'yes','ending_resolved':'no','cliffhanger_type':'question'}
    row['context']={'source_supported':'yes','hook_factually_supported':'yes','cliffhanger_factually_supported':'yes',
        'misleading_cut':'no','essential_context_missing':'no','ending_resolved_inside_clip':'no','boundary_issue':'none',
        'description_speakers_match_source':'yes','description_consequences_match_source':'yes'}
    batch=ws.project_path(pid)/'batch';(batch/'reviews/blind').mkdir(parents=True)
    (batch/'candidate_reviews.json').write_text(json.dumps([row]))
    (batch/'reviews/blind'/f"{row['candidate_id']}.json").write_text(json.dumps(row['blind']))
    return row


def test_source_checked_draft_is_downloadable_without_becoming_approved(client):
    p=upload(client);row=seed(client,p['id'])
    cuts=client.get(f"/api/projects/{p['id']}/promo-cuts").json()
    assert len(cuts)==1 and cuts[0]['delivery_kind']=='draft_editor_check'
    assert cuts[0]['status']=='editor_review' and cuts[0]['human_reviewed'] is False
    text=client.get(f"/api/projects/{p['id']}/export/promo-cuts-txt").text
    assert 'PROMO CUT SHEET' in text and 'Someone is inside.' in text and 'not an AI-approved' in text
    csv=client.get(f"/api/projects/{p['id']}/export/promo-cuts-csv").text
    assert 'draft_editor_check' in csv and '00:00:00.000' in csv
    assert client.get(f"/api/projects/{p['id']}/export/shortlist-json").json()==[]


def test_unreviewed_resolved_misleading_and_unsafe_cuts_never_delivered(client):
    p=upload(client);assert client.get(f"/api/projects/{p['id']}/promo-cuts").json()==[]
    row=seed(client,p['id']);eps=ws.episodes(ws.project_path(p['id']))
    for field,value in [('ending_resolved_inside_clip','yes'),('misleading_cut','yes'),('source_supported','uncertain')]:
        changed=deepcopy(row);changed['context'][field]=value
        assert deliverable_cuts([changed],eps,p['id'])==[]
    changed=deepcopy(row);changed['blind']['cliffhanger_type']='none'
    assert deliverable_cuts([changed],eps,p['id'])==[]
    changed=deepcopy(row);changed['start_ms']=1000
    assert deliverable_cuts([changed],eps,p['id'])==[]


def test_matching_upload_does_not_automatically_reuse_prior_cuts(client):
    original=upload(client);seed(client,original['id']);fresh=upload(client)
    assert fresh['batch'] is None
    cuts=client.get(f"/api/projects/{fresh['id']}/promo-cuts").json()
    assert cuts==[]
    assert client.get(f"/api/projects/{fresh['id']}/export/promo-cuts-json").json()==[]
    # Offline audit can explicitly inspect an earlier source match. The app
    # and its exports never opt into this historical lookup.
    saved=ws.get_promo_cuts(fresh['id'],allow_saved=True)
    assert len(saved)==1 and saved[0]['origin_project_id']==original['id'] and saved[0]['reused_saved_cut']
    assert not (ws.project_path(fresh['id'])/'batch').exists()
    unrelated=upload(client,'1\n00:00:00,000 --> 00:00:25,000\nAVA: A completely different screenplay.\n')
    assert client.get(f"/api/projects/{unrelated['id']}/promo-cuts").json()==[]


def test_normal_upload_has_no_optional_evaluation_reservations(client):
    import hashlib
    text='1\n00:00:00,000 --> 00:00:25,000\nAVA: A new show.\n'
    private=ws.ROOT/'evaluation/private';private.mkdir(parents=True)
    (private/'coverage_reservations.json').write_text(json.dumps({'entries':[{'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'reason':'Optional benchmark reservation'}]}))
    p=upload(client,text,'episode-9.srt')
    assert p['selected_episodes']==[9] and not p['episodes'][0]['reference_reserved']


def test_saved_cut_provenance_does_not_increment_current_run_progress(client):
    original=upload(client);seed(client,original['id'])
    origin=ws.project_path(original['id'])
    (origin/'batch/batch_report.json').write_text(json.dumps({'ai_analyzed_episodes':1,'blind_reviewed':1,'context_reviewed':1,'ai_eligible':0}))
    fresh=upload(client);current=ws.project_path(fresh['id']);(current/'batch').mkdir()
    (current/'batch/batch_report.json').write_text(json.dumps({'ai_analyzed_episodes':1,'blind_reviewed':0,'context_reviewed':0,'ai_eligible':0}))
    assert client.get(f"/api/projects/{fresh['id']}/promo-cuts").json()==[]
    cuts=ws.get_promo_cuts(fresh['id'],allow_saved=True)
    assert cuts[0]['origin_run_summary']['id']==original['id']
    assert cuts[0]['origin_run_summary']['audience_reviews']==1
    assert cuts[0]['exact_source_match_verified'] is True
    progress=client.get(f"/api/projects/{fresh['id']}").json()['batch']
    assert progress['blind_reviewed']==0 and progress['context_reviewed']==0
    assert not list((current/'batch').glob('reviews/**/*.json'))
