"""Project-scoped local workspace service, shared by the API and worker."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
import yaml
from .models import Episode
from .ingestion import ingest,save_ingestion
from .candidates import sliding_windows,select_baseline
from .evaluation import config_hash
from .batch import run_batch,save
from .exports import write_review_exports
from .profiles import apply_profile
from .coverage import excluded_numbers
from .editor_policy import apply_editor_policy
from .cut_delivery import deliverable_cuts
from . import public_mode

ROOT=Path(__file__).resolve().parents[1]
WORKSPACES=ROOT/('runs/public_workspaces' if public_mode.enabled() else 'runs/workspaces')
WORKSPACES.mkdir(parents=True,exist_ok=True)
LOCK=threading.Lock()
ACTIVE=set()


def now():return datetime.now(timezone.utc).isoformat()


def project_path(project_id):
    if project_id=='assignment':
        p=ROOT/'runs/subset-discovery-v3'
        if (p/'episodes.jsonl').exists():return p
        raise ValueError('Unknown project')
    if not __import__('re').fullmatch(r'[a-f0-9]{32}',project_id):raise ValueError('Unknown project')
    p=WORKSPACES/project_id
    if not (p/'project.json').exists():raise ValueError('Unknown project')
    return p


def episodes(path):return [Episode.model_validate_json(x) for x in (path/'episodes.jsonl').read_text().splitlines() if x.strip()]


def protected_hashes():
    path=ROOT/'evaluation/private/human_references.json'
    if not path.exists():return set()
    return {e['source_sha256'] for e in json.loads(path.read_text())['entries']}


def coverage_reservations():
    """Private, source-bound exclusions; no episode-number rules for new shows."""
    path=ROOT/'evaluation/private/coverage_reservations.json'
    return {r['source_sha256']:r for r in json.loads(path.read_text())['entries']} if path.exists() else {}


def project_config(eps):
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    if public_mode.enabled():
        config['quota']['organization_namespace']=public_mode.namespace()
        config['batch']['processing_profile']='groq'
    protected=[e.episode_number for e in eps if e.source_sha256 in protected_hashes()]
    reservations=coverage_reservations() if config['batch'].get('respect_evaluation_reservations',True) else {}
    reserved=[e.episode_number for e in eps if e.source_sha256 in reservations]
    config['batch']['coverage_excluded_episode_numbers']=sorted(set(reserved))
    config['batch']['coverage_reservations']=[{'episode_number':e.episode_number,'reason':reservations[e.source_sha256]['reason']}
        for e in eps if e.source_sha256 in reservations]
    config['evaluation'].update(held_out_episode_numbers=protected,primary_reference_episode_numbers=protected,
        development_episode_numbers=[e.episode_number for e in eps if e.episode_number not in protected],
        tuning_episode_numbers=[],baseline_comparison_episode_numbers=protected)
    ready=[e.episode_number for e in eps if e.source_ready and e.episode_number not in set(protected+reserved)]
    cap=config['batch'].get('auto_episode_limit')
    if cap and len(ready)>cap:ready=[ready[round(i*(len(ready)-1)/(cap-1))] for i in range(cap)] if cap>1 else ready[:1]
    config['batch'].update(ai_episode_numbers=ready,scope='user_selected_new_input')
    return config


def create_project(input_path,title):
    eps,report=ingest(input_path)
    pid=uuid.uuid4().hex;path=WORKSPACES/pid;path.mkdir()
    save_ingestion(eps,report,path)
    save(path/'project.json',{'id':pid,'name':title.strip()[:100] or input_path.stem,'created_at':now(),'origin':'upload'})
    save(path/'config.json',project_config(eps))
    public_mode.register(pid)
    return pid


def new_run(pid):
    """New pinned run from the same upload; original results stay intact."""
    import shutil
    source=project_path(pid)
    with LOCK:
        if pid in ACTIVE:raise ValueError('Wait for the active run to finish or pause before creating a new run.')
    new_id=uuid.uuid4().hex;target=WORKSPACES/new_id;target.mkdir()
    for name in ('episodes.jsonl','episode.schema.json','ingestion_report.json'):
        if (source/name).exists():shutil.copy2(source/name,target/name)
    title=next(x['name'] for x in catalog() if x['id']==pid)
    save(target/'project.json',{'id':new_id,'name':title+' · new run','created_at':now(),'origin':'upload','previous_project_id':pid})
    save(target/'config.json',project_config(episodes(target)))
    public_mode.register(new_id)
    return new_id


def catalog():
    values=[]
    if not public_mode.enabled() and (ROOT/'runs/subset-discovery-v3/episodes.jsonl').exists():
        values.append({'id':'assignment','name':'Kuku assignment · saved discovery','origin':'private_saved','created_at':''})
    for f in WORKSPACES.glob('*/project.json'):
        values.append(json.loads(f.read_text()))
    return [v for v in values if public_mode.visible(v['id'])]


def decision_view(path):
    pointer=path/'batch/active_decision_view.json'
    if pointer.exists():
        folder=(path/'batch'/json.loads(pointer.read_text())['relative_path']).resolve()
        if not folder.is_relative_to((path/'batch').resolve()):raise ValueError('Invalid decision view path')
        return folder
    legacy=path/'batch/eligibility_rechecks/v2'
    return legacy if (legacy/'correction.json').exists() else path/'batch'


def metadata(pid):
    path=project_path(pid)
    info=next(x for x in catalog() if x['id']==pid)
    eps=episodes(path);report=json.loads((path/'ingestion_report.json').read_text())
    cfg=json.loads((path/'batch/effective_config.json').read_text()) if pid=='assignment' else json.loads((path/'config.json').read_text())
    protected=protected_hashes()
    excluded=excluded_numbers(cfg)
    reservation_reasons={r['episode_number']:r['reason'] for r in cfg['batch'].get('coverage_reservations',[])}
    job=json.loads((path/'job.json').read_text()) if (path/'job.json').exists() else None
    batch=json.loads((path/'batch/batch_report.json').read_text()) if (path/'batch/batch_report.json').exists() else None
    correction=decision_view(path)/'correction.json'
    if batch and correction.exists():
        policy=json.loads(correction.read_text())
        batch={**batch,'original_ai_eligible':batch['ai_eligible'],'ai_eligible':policy['corrected_recommendations'],
               'editor_review':policy.get('editor_review',0),
               'eligibility_policy_version':policy['policy_version'],'eligibility_rechecked_offline':True}
    if job and job.get('status')=='running' and pid not in ACTIVE:
        job={**job,'status':'interrupted','message':'Worker stopped. Resume to continue cached work.'}
    return {**info,'ingestion':report,'episodes':[{'id':e.episode_id,'number':e.episode_number,
        'filename':e.source_file,'events':len(e.events),'duration_ms':e.duration_ms,'source_ready':e.source_ready,
        'reference_reserved':e.source_sha256 in protected or e.episode_number in excluded,
        'reservation_reason':reservation_reasons.get(e.episode_number,'held-out reference' if e.source_sha256 in protected else ''),
        'issues':[i.model_dump() for i in e.issues],
        'format':e.metadata.get('input_format','txt')} for e in eps],
        'selected_episodes':cfg['batch']['ai_episode_numbers'],'job':job,'batch':batch,
        'processing_profile':cfg['batch'].get('processing_profile','groq'),
        'local_model':cfg['models']['analysis']['model'] if cfg['models']['analysis']['provider']=='ollama' else None,
        'pipeline_version':cfg['batch'].get('pipeline_version',1),
        'models':{r:cfg['models'][r]['model'] for r in ('analysis','blind','context')},'saved_read_only':pid=='assignment'}


def get_candidates(pid):
    path=project_path(pid);eps=episodes(path);rows=[]
    cfg=json.loads((path/'batch/effective_config.json').read_text()) if pid=='assignment' else json.loads((path/'config.json').read_text())
    excluded=excluded_numbers(cfg)
    saved=decision_view(path)/'candidate_reviews.json'
    if pid=='assignment' and (ROOT/'runs/subset-editor-export-v1/candidate_reviews.json').exists():
        saved=ROOT/'runs/subset-editor-export-v1/candidate_reviews.json'
    completed={r['candidate_id']:r for r in json.loads(saved.read_text())} if saved.exists() else {}
    protected=protected_hashes()
    for ep in eps:
        if ep.source_sha256 in protected or ep.episode_number in excluded or not ep.source_ready:continue
        queue=path/f'batch/episodes/{ep.episode_id}/review_queue.json'
        values=json.loads(queue.read_text()) if queue.exists() else [c.model_dump() for c in select_baseline(sliding_windows(ep),2)]
        for value in values:
            cid=value['candidate_id'];row=completed.get(cid)
            blind_path=path/f'batch/reviews/blind/{cid}.json'
            blind=json.loads(blind_path.read_text()) if blind_path.exists() else None
            decision=path/f'batch/blind_decisions/{cid}.json'
            decision=json.loads(decision.read_text()) if decision.exists() else {}
            failure=(path/f'batch/review_failures/blind-{cid}.json').exists() or (path/f'batch/review_failures/context-{cid}.json').exists()
            budget_skipped=(path/f'batch/context_budget_unreviewed/{cid}.json').exists()
            capacity_skipped=(path/f'batch/context_capacity_unreviewed/{cid}.json').exists()
            status=row['status'] if row else 'failed' if failure else 'not_verified_capacity' if capacity_skipped else 'not_verified_quota' if budget_skipped else decision.get('status','unreviewed')
            rows.append({**value,**(decision if not row else {}),**(row or {}),'status':status,'blind':blind or {},'context':row.get('context',{}) if row else {},
                'segments':[{'episode_id':ep.episode_id,'start_ms':value['start_ms'],'end_ms':value['end_ms']}],
                'source_name':ep.source_file})
    manual=path/'editor_candidates.json'
    if manual.exists():
        for row in json.loads(manual.read_text()):
            if row['episode_id'] not in {e.episode_id for e in eps if e.source_sha256 in protected or e.episode_number in excluded}:
                rows.append(row)
    notes=json.loads((path/'editor_notes.json').read_text()) if (path/'editor_notes.json').exists() else {}
    for row in rows:row['editor_note']=notes.get(row['candidate_id'])
    if cfg.get('review',{}).get('editor_policy'):rows=[apply_editor_policy(r) for r in rows]
    return sorted(rows,key=lambda r:(0 if r['status']=='ai_eligible' else 1,r.get('rank',999),r['episode_id'],r['start_ms']))


def get_promo_cuts(pid,allow_saved=True):
    path=project_path(pid);eps=episodes(path)
    def completed_rows(origin):
        file=decision_view(origin)/'candidate_reviews.json'
        if not file.exists():return []
        cfg=json.loads((origin/'config.json').read_text()) if (origin/'config.json').exists() else {}
        excluded=excluded_numbers(cfg) if cfg else set()
        protected=protected_hashes()
        blocked={e.episode_id for e in episodes(origin) if e.episode_number in excluded or e.source_sha256 in protected}
        notes=json.loads((origin/'editor_notes.json').read_text()) if (origin/'editor_notes.json').exists() else {}
        values=[r for r in json.loads(file.read_text()) if r['episode_id'] not in blocked]
        for row in values:row['editor_note']=notes.get(row['candidate_id'])
        if cfg.get('review',{}).get('editor_policy'):values=[apply_editor_policy(r) for r in values]
        return values
    cuts=deliverable_cuts(completed_rows(path),eps,pid)
    if cuts or not allow_saved:return cuts
    # Exact source bytes and episode identity must match. Never carry unrelated
    # show results or claim these as fresh calls by the selected run's models.
    signature=sorted((e.episode_id,e.source_sha256) for e in eps)
    cfg=json.loads((path/'config.json').read_text()) if pid!='assignment' else {}
    excluded=excluded_numbers(cfg) if cfg else set()
    matches=[]
    for info in catalog():
        if info['id']==pid:continue
        origin=project_path(info['id']);old_eps=episodes(origin)
        if sorted((e.episode_id,e.source_sha256) for e in old_eps)!=signature:continue
        previous=deliverable_cuts(completed_rows(origin),old_eps,info['id'])
        previous=[r for r in previous if int(r['episode_id'][1:]) not in excluded]
        if previous:matches.append(previous)
    if matches:
        cuts=max(matches,key=lambda values:(len(values),values[0]['origin_project_id']))
        for cut in cuts:
            cut['reused_saved_cut']=True
            cut['delivery_note']+=' Reused from a saved run for these exact source files; no new AI call.'
    return cuts


def start_job(pid,selected,profile=None,local_model='llama3.1:8b'):
    path=project_path(pid)
    if pid=='assignment':raise ValueError('Saved discovery is immutable. Upload scripts to create a new working project.')
    config=json.loads((path/'config.json').read_text())
    eps=episodes(path);allowed={e.episode_number for e in eps if e.source_ready and e.source_sha256 not in protected_hashes() and e.episode_number not in excluded_numbers(config)}
    if not selected or len(selected)!=len(set(selected)) or not set(selected)<=allowed:raise ValueError('Choose valid, non-reference source-ready episodes.')
    config['batch']['ai_episode_numbers']=sorted(selected)
    manifest=path/'batch/manifest.json'
    if profile and not manifest.exists():config=apply_profile(config,profile,local_model)
    elif profile and profile!=config['batch'].get('processing_profile','groq'):
        raise ValueError('The model is pinned for this run. Start a new run to change processing mode.')
    if manifest.exists() and json.loads(manifest.read_text())['config_hash']!=config_hash(config):
        raise ValueError('This run is already pinned. Resume with the same episode selection, or create a new project.')
    with LOCK:
        if ACTIVE:raise ValueError('Another AI job is active. Wait for it to finish or pause before starting another.')
        ACTIVE.add(pid)
    save(path/'config.json',config)
    save(path/'job.json',{'status':'running','started_at':now(),'message':'Preparing source-aligned candidates.'})
    thread=threading.Thread(target=worker,args=(pid,config),daemon=True);thread.start()


def worker(pid,config):
    path=project_path(pid);stop=time.monotonic()+1800
    try:
        eps=episodes(path);report=json.loads((path/'ingestion_report.json').read_text())
        while True:
            result=run_batch(ROOT,eps,report['input_hash'],config,path/'batch')
            delay=result.get('retry_after_seconds')
            if result['status']=='paused_budget' and delay and delay<=120 and time.monotonic()+delay<stop:
                save(path/'job.json',{'status':'running','updated_at':now(),'message':'Waiting for the free-tier token window. Completed work is saved.','retry_at':time.time()+delay})
                end=time.monotonic()+delay+1
                while time.monotonic()<end:time.sleep(min(5,max(.01,end-time.monotonic())))
                continue
            save(path/'job.json',{'status':result['status'],'updated_at':now(),'message':result.get('reason') or 'Discovery completed. Review the shortlist.','retry_after_seconds':delay})
            break
    except Exception as exc:
        save(path/'job.json',{'status':'failed','updated_at':now(),'message':'Job stopped safely. Completed artifacts are preserved.','error_type':type(exc).__name__})
    finally:
        with LOCK:ACTIVE.discard(pid)
