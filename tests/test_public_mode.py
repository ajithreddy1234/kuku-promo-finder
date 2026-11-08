"""Public deployment checks use fake credentials and invented source only."""
import json
import pytest
from fastapi.testclient import TestClient
import api
from src import public_mode,workspace as ws
from test_studio_api import client,upload,H


@pytest.fixture
def public_client(client,monkeypatch):
    monkeypatch.setenv('PROMO_PUBLIC','1')
    public_mode.SESSIONS.clear();public_mode.OWNERS.clear()
    (ws.ROOT/'.env').write_text('GROQ_API_KEY=gsk_owner-secret-must-never-be-used\n')
    return client


def test_public_sessions_cannot_list_or_fetch_each_others_uploads(public_client):
    one=public_client;two=TestClient(api.app)
    first=upload(one);second=upload(two)
    assert {p['id'] for p in one.get('/api/projects').json()}=={first['id']}
    assert {p['id'] for p in two.get('/api/projects').json()}=={second['id']}
    for suffix in ['', '/candidates','/promo-cuts','/export/raw-outputs']:
        assert two.get(f"/api/projects/{first['id']}"+suffix).status_code==404
    assert 'httponly' in one.get('/api/status').headers['set-cookie'].lower()


def test_public_key_is_memory_only_owner_key_not_exposed_or_used(public_client,monkeypatch):
    one=public_client;two=TestClient(api.app);p=upload(one)
    assert one.get('/api/status').json()['api_configured'] is False
    blocked=one.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[73]})
    assert blocked.status_code==400
    key='gsk_synthetic-visitor-key-not-real'
    assert one.post('/api/session/key',headers=H,json={'api_key':key}).status_code==200
    assert one.get('/api/status').json()['api_configured'] is True
    assert two.get('/api/status').json()['api_configured'] is False
    calls=[];monkeypatch.setattr(ws,'start_job',lambda *args:calls.append(args))
    assert one.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[73]}).status_code==200
    assert calls
    assert public_mode.project_key(p['id'])==key
    assert key not in one.get(f"/api/projects/{p['id']}").text
    assert all(key not in f.read_text() for f in ws.project_path(p['id']).rglob('*.json'))


def test_public_model_modes_and_origins_are_restricted(public_client):
    p=upload(public_client)
    r=public_client.post(f"/api/projects/{p['id']}/jobs",headers=H,json={'episode_numbers':[73],'profile':'local'})
    assert r.status_code==400
    assert public_client.get('/api/status').json()['local_models']==[]
    assert public_client.post('/api/session/key',headers={**H,'origin':'https://unrelated.example'},json={'api_key':'gsk_synthetic-not-real'}).status_code==403


def test_clear_removes_only_own_uploads_and_keeps_other_sessions(public_client):
    one=public_client;two=TestClient(api.app);a=upload(one);b=upload(two)
    cache=ws.ROOT/'runs/_public_cache'/public_mode.destination_namespace(ws.project_path(a['id'])/'batch')
    cache.mkdir(parents=True);(cache/'private-response.json').write_text('Synthetic cached script text')
    result=one.post('/api/session/clear',headers=H)
    assert result.status_code==200 and result.json()['quota_reset'] is False
    assert one.get('/api/projects').json()==[]
    assert two.get('/api/projects').json()[0]['id']==b['id']
    assert not (ws.WORKSPACES/a['id']).exists() and (ws.WORKSPACES/b['id']).exists()
    assert not cache.exists()


def test_deliberate_shared_server_key_has_shared_quota_namespace(public_client,monkeypatch):
    monkeypatch.setenv('PROMO_SHARED_KEY','1');monkeypatch.setenv('GROQ_API_KEY','gsk_synthetic-shared-not-real')
    one=public_client;two=TestClient(api.app);a=upload(one);b=upload(two)
    assert one.get('/api/status').json()['shared_key_configured']
    assert two.get('/api/status').json()['api_configured']
    cfg_a=json.loads((ws.project_path(a['id'])/'config.json').read_text())
    cfg_b=json.loads((ws.project_path(b['id'])/'config.json').read_text())
    assert cfg_a['quota']['organization_namespace']==cfg_b['quota']['organization_namespace']=='public_shared_organization'
    assert 'gsk_synthetic-shared-not-real' not in one.get('/api/status').text


def test_public_identical_uploads_do_not_inherit_old_cuts_or_request_cache(public_client):
    from test_cut_delivery import seed
    from src.provider import JsonProvider
    first=upload(public_client);seed(public_client,first['id'])
    assert len(public_client.get(f"/api/projects/{first['id']}/promo-cuts").json())==1
    second=upload(public_client)
    assert public_client.get(f"/api/projects/{second['id']}/promo-cuts").json()==[]
    assert public_client.get(f"/api/projects/{second['id']}/export/promo-cuts-json").json()==[]
    assert len(public_client.get(f"/api/projects/{second['id']}/export/promo-cuts-csv").text.splitlines())==1
    assert 'No source-checked promo cuts are available yet' in public_client.get(f"/api/projects/{second['id']}/export/promo-cuts-txt").text
    token=public_mode.CURRENT.set(public_mode.OWNERS[first['id']])
    try:
        config={'quota':{'enabled':False}}
        a=JsonProvider(ws.ROOT,config,ws.project_path(first['id'])/'batch')
        b=JsonProvider(ws.ROOT,config,ws.project_path(second['id'])/'batch')
        assert a.cache!=b.cache
        again=JsonProvider(ws.ROOT,config,ws.project_path(first['id'])/'batch')
        assert again.cache==a.cache  # resume this run, not replay another upload
    finally:public_mode.CURRENT.reset(token)
