"""Local React API. Bind to loopback; credentials stay in the Python process."""
from datetime import datetime,timezone
import csv
from io import StringIO,BytesIO
import json
from pathlib import Path,PurePosixPath
import shutil
import uuid
import zipfile

from fastapi import FastAPI,UploadFile,File,Form,HTTPException,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from dotenv import dotenv_values
import yaml
from src import workspace as ws
from src.batch import save
from src.budget import BudgetLedger
from src.evaluation import ReferenceBundle,ReferenceEntry,ReferenceSelection,validate_references
from src.timeline import reconstruct
from src.candidates import make_candidate
from src.profiles import local_models
from src.cut_delivery import cut_text
from src.timeline import format_time
from src import public_mode
from urllib.parse import urlsplit

app=FastAPI(title='Kuku Promo Studio',docs_url='/api/docs',openapi_url='/api/openapi.json')
app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:5173','http://localhost:5173','http://127.0.0.1:8000','http://localhost:8000'],allow_methods=['GET','POST'],allow_headers=['Content-Type','X-Promo-Client'])

@app.middleware('http')
async def local_mutations(request:Request,call_next):
    if request.method=='POST' and request.headers.get('X-Promo-Client')!='studio':
        return Response('Use the local Promo Studio client.',status_code=403)
    origin=request.headers.get('origin')
    public=public_mode.enabled()
    allowed=(urlsplit(origin).netloc==request.url.netloc) if public and origin else origin in ('http://127.0.0.1:5173','http://localhost:5173','http://127.0.0.1:8000','http://localhost:8000')
    if origin and not allowed:
        return Response('Origin not allowed.',status_code=403)
    if not public:return await call_next(request)
    sid=public_mode.session(request.cookies.get('promo_session'));token=public_mode.CURRENT.set(sid)
    try:
        parts=request.url.path.split('/')
        if len(parts)>3 and parts[1:3]==['api','projects'] and not public_mode.visible(parts[3]):
            response=Response('Project not found in this session.',status_code=404)
        else:response=await call_next(request)
        response.set_cookie('promo_session',sid,max_age=3600,httponly=True,secure=request.url.scheme=='https',samesite='strict')
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='private, no-store'
        return response
    finally:public_mode.CURRENT.reset(token)

@app.exception_handler(ValueError)
async def bad_input(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':str(exc)},status_code=400)


def path(pid):
    try:return ws.project_path(pid)
    except ValueError:raise HTTPException(404,'Project not found')

@app.get('/api/status')
def status():
    config=yaml.safe_load((ws.ROOT/'config.yaml').read_text())
    keys=dotenv_values(ws.ROOT/'.env')
    public=public_mode.enabled()
    if public:config['quota']['organization_namespace']=public_mode.namespace()
    return {'mode':'public_session' if public else 'private_local','api_configured':bool(public_mode.current_key()) if public else bool(keys.get('GROQ_API_KEY')),
        'shared_key_configured':bool(public_mode.shared_key()) if public else False,
        'active_jobs':[p for p in ws.ACTIVE if public_mode.visible(p)],'server_busy':bool(ws.ACTIVE),
        'quota':BudgetLedger(ws.ROOT,config).snapshot(),
        'local_models':[] if public else local_models(),
        'formats':['ZIP','TXT','MD','SRT','VTT','CSV','JSON','DOCX','text PDF']}

@app.get('/api/health')
def health():return {'ok':True}

class SessionKey(BaseModel):
    api_key:str=Field(min_length=20,max_length=256)

@app.post('/api/session/key')
def session_key(body:SessionKey):
    if not public_mode.enabled():raise HTTPException(404,'Public session mode is disabled')
    if not body.api_key.startswith('gsk_') or not body.api_key.isascii() or any(c.isspace() for c in body.api_key):raise ValueError('Enter a Groq API key beginning gsk_.')
    public_mode.SESSIONS[public_mode.CURRENT.get()]['key']=body.api_key
    return {'configured':True,'storage':'process memory only; not written to project files or browser storage'}

@app.post('/api/session/clear')
def clear_session():
    if not public_mode.enabled():raise HTTPException(404,'Public session mode is disabled')
    owned=[pid for pid in public_mode.OWNERS if public_mode.visible(pid)]
    if set(owned)&ws.ACTIVE:raise ValueError('Wait for processing to finish or pause before clearing this session.')
    for pid in owned:
        folder=ws.project_path(pid)
        shutil.rmtree(folder)
        public_mode.OWNERS.pop(pid,None)
    public_mode.SESSIONS[public_mode.CURRENT.get()]['key']=None
    # Quota history is retained: clearing uploads must not reset usage accounting.
    return {'cleared_projects':len(owned),'quota_reset':False}

@app.get('/api/projects')
def projects():return ws.catalog()

@app.post('/api/projects')
async def create(title:str=Form('New screenplay'),files:list[UploadFile]=File(...)):
    if not files or len(files)>100:raise ValueError('Upload 1-100 files, or one ZIP archive.')
    folder=ws.ROOT/'runs/uploads'/uuid.uuid4().hex;folder.mkdir(parents=True)
    size=0;names=set()
    try:
        for upload in files:
            relative=PurePosixPath((upload.filename or 'script.txt').replace('\\','/'))
            if relative.is_absolute() or '..' in relative.parts or not relative.parts or ':' in relative.parts[0]:
                raise ValueError('Upload paths must stay inside the selected episode folder.')
            name=relative.as_posix()
            if name.casefold() in {n.casefold() for n in names}:raise ValueError('Uploaded relative filenames must be unique.')
            names.add(name)
            limit=32*1024*1024 if name.lower().endswith('.zip') else 4*1024*1024
            raw=await upload.read(limit+1);size+=len(raw)
            if len(raw)>limit or size>64*1024*1024:raise ValueError('Upload exceeds the size limit (4 MB/file, 32 MB/ZIP, 64 MB total).')
            target=folder/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        if len(files)>1 and any(n.lower().endswith('.zip') for n in names):raise ValueError('Upload ZIP archives individually.')
        source=folder/next(iter(names)) if len(files)==1 else folder
        pid=ws.create_project(source,title)
        return ws.metadata(pid)
    except zipfile.BadZipFile:raise ValueError('This ZIP archive could not be read.') from None
    finally:shutil.rmtree(folder,ignore_errors=True)

@app.get('/api/projects/{pid}')
def project(pid:str):path(pid);return ws.metadata(pid)

@app.get('/api/projects/{pid}/episodes/{eid}')
def episode(pid:str,eid:str):
    ep=next((e for e in ws.episodes(path(pid)) if e.episode_id==eid),None)
    if not ep:raise HTTPException(404,'Episode not found')
    return ep.model_dump()

@app.get('/api/projects/{pid}/candidates')
def candidates(pid:str):path(pid);return ws.get_candidates(pid)

@app.get('/api/projects/{pid}/promo-cuts')
def promo_cuts(pid:str):path(pid);return ws.get_promo_cuts(pid)

class JobInput(BaseModel):
    episode_numbers:list[int]
    profile:str|None=None
    local_model:str='llama3.1:8b'

@app.post('/api/projects/{pid}/plan')
def capacity_plan(pid:str,body:JobInput):
    from src.capacity import plan_run
    from src.profiles import apply_profile
    p=path(pid);config=json.loads((p/'config.json').read_text())
    if public_mode.enabled() and body.profile not in (None,'groq'):raise ValueError('Public processing uses your Groq account; Ollama runs only in the local app.')
    if not (p/'batch/manifest.json').exists():config=apply_profile(config,body.profile or 'groq',body.local_model)
    return plan_run(ws.ROOT,ws.episodes(p),config,body.episode_numbers)

@app.post('/api/projects/{pid}/new-run')
def new_run(pid:str):
    path(pid)
    return ws.metadata(ws.new_run(pid))

@app.post('/api/projects/{pid}/jobs')
def run(pid:str,body:JobInput):
    path(pid)
    profile=body.profile or ws.metadata(pid)['processing_profile']
    if public_mode.enabled() and profile!='groq':raise ValueError('Choose Groq processing on the hosted prototype.')
    key=public_mode.project_key(pid) if public_mode.enabled() else dotenv_values(ws.ROOT/'.env').get('GROQ_API_KEY')
    if profile in ('groq','hybrid') and not key:raise ValueError('Add your Groq key in this browser session.' if public_mode.enabled() else 'Groq API key is missing from the local backend .env.')
    if body.profile is None:ws.start_job(pid,body.episode_numbers)
    else:ws.start_job(pid,body.episode_numbers,profile,body.local_model)
    return {'status':'started'}

class EditorNote(BaseModel):
    evaluator:str=Field(min_length=1,max_length=100)
    verdict:str
    reason:str=Field(min_length=1,max_length=3000)

@app.post('/api/projects/{pid}/candidates/{cid}/review')
def review(pid:str,cid:str,body:EditorNote):
    p=path(pid)
    if not body.evaluator.strip() or not body.reason.strip():raise ValueError('Enter your name and your reason.')
    if body.verdict not in ('accept','edit','reject'):raise ValueError('Choose accept, edit or reject.')
    if not any(c['candidate_id']==cid for c in ws.get_candidates(pid)):raise ValueError('Candidate not found.')
    notes=json.loads((p/'editor_notes.json').read_text()) if (p/'editor_notes.json').exists() else {}
    notes[cid]={**body.model_dump(),'recorded_at':ws.now(),'origin':'human_entered','independent_reference':False}
    save(p/'editor_notes.json',notes)
    return notes[cid]

class RevisionInput(BaseModel):
    start_ms:int
    end_ms:int
    reason:str=Field(min_length=1,max_length=3000)

@app.post('/api/projects/{pid}/candidates/{cid}/revise')
def revise(pid:str,cid:str,body:RevisionInput):
    p=path(pid)
    original=next((r for r in ws.get_candidates(pid) if r['candidate_id']==cid),None)
    if not original:raise ValueError('Candidate not found.')
    if not body.reason.strip():raise ValueError('Explain the cut change.')
    ep=next(e for e in ws.episodes(p) if e.episode_id==original['episode_id'])
    if (body.start_ms,body.end_ms)==(original['start_ms'],original['end_ms']):raise ValueError('Choose a different start or end.')
    candidate=make_candidate(ep,body.start_ms,body.end_ms,source='editor',version=original.get('candidate_version',1)+1)
    row={**candidate.model_dump(),'revision_of':cid,'revision_reason':body.reason.strip(),
        'segments':[{'episode_id':ep.episode_id,'start_ms':body.start_ms,'end_ms':body.end_ms}],
        'source_name':ep.source_file,'blind':{},'context':{},'human_reviewed':False}
    f=p/'editor_candidates.json';rows=json.loads(f.read_text()) if f.exists() else []
    if any(r['candidate_id']==row['candidate_id'] for r in rows):raise ValueError('This cut revision is already saved.')
    rows.append(row);save(f,rows)
    return row


def reference_state(pid):
    p=path(pid);eps=ws.episodes(p)
    if pid=='assignment':
        f=ws.ROOT/'evaluation/private/human_references.json'
        cfg=yaml.safe_load((ws.ROOT/'config.yaml').read_text())
        primary=cfg['evaluation']['primary_reference_episode_numbers']
        bundle=ReferenceBundle.model_validate_json(f.read_text())
    else:
        f=p/'references.json';primary=[]
        bundle=ReferenceBundle.model_validate_json(f.read_text()) if f.exists() else ReferenceBundle(
            input_hash=json.loads((p/'ingestion_report.json').read_text())['input_hash'],entries=[ReferenceEntry(episode_id=e.episode_id,source_sha256=e.source_sha256) for e in eps])
    exposed=ws.ROOT/'evaluation/private/held_out_exposure.jsonl'
    locked={json.loads(x)['episode_id'] for x in exposed.read_text().splitlines()} if pid=='assignment' and exposed.exists() else set()
    return p,eps,f,primary,bundle,locked

@app.get('/api/projects/{pid}/references')
def references(pid:str):
    p,eps,f,primary,bundle,locked=reference_state(pid)
    return {**bundle.model_dump(mode='json'),'primary_numbers':primary,'locked_episode_ids':sorted(locked),
        'purpose':'independent_evaluation' if pid=='assignment' else 'optional_human_examples',
        'system_results_exist':(p/'batch/reviews/blind').exists(),
        'evaluation_job':json.loads((ws.ROOT/'evaluation/private/evaluation_job.json').read_text()) if pid=='assignment' and (ws.ROOT/'evaluation/private/evaluation_job.json').exists() else None}

@app.post('/api/projects/{pid}/evaluation')
def start_evaluation(pid:str):
    path(pid)
    if pid!='assignment':raise ValueError('This new project has optional examples, not a reserved independent evaluation split.')
    if not dotenv_values(ws.ROOT/'.env').get('GROQ_API_KEY'):raise ValueError('Groq API key is missing from the local backend .env.')
    from src.evaluation_job import launch
    launch()
    return {'status':'started'}

class ReferenceInput(BaseModel):
    episode_id:str
    decision:str
    start_ms:int|None=None
    end_ms:int|None=None
    reason:str=Field(min_length=1,max_length=3000)
    remove_index:int|None=None

@app.post('/api/projects/{pid}/references')
def save_reference(pid:str,body:ReferenceInput):
    p,eps,f,primary,bundle,locked=reference_state(pid)
    if body.episode_id in locked:raise ValueError('Evaluation has begun; independent labels are locked.')
    if not body.reason.strip():raise ValueError('Enter your own reason.')
    ep=next((e for e in eps if e.episode_id==body.episode_id),None)
    entry=next((e for e in bundle.entries if e.episode_id==body.episode_id),None)
    if not ep or not entry:raise ValueError('Episode not found.')
    if body.remove_index is not None:
        if not 0<=body.remove_index<len(entry.selections):raise ValueError('Unknown selection.')
        entry.selections.pop(body.remove_index);entry.decision='selected' if entry.selections else 'pending'
    elif body.decision=='none':
        if entry.selections:raise ValueError('Remove saved picks before recording no strong moment.')
        entry.decision='none';entry.reason=body.reason.strip()
    elif body.decision=='selected':
        if body.start_ms is None or body.end_ms is None or not 0<=body.start_ms<body.end_ms<=ep.duration_ms or not 20000<=body.end_ms-body.start_ms<=60000:
            raise ValueError('Choose a 20-60 second interval inside the episode.')
        if any(s.start_ms==body.start_ms and s.end_ms==body.end_ms for s in entry.selections):raise ValueError('This selection is already saved.')
        entry.selections.append(ReferenceSelection(start_ms=body.start_ms,end_ms=body.end_ms,why_it_works=body.reason.strip()));entry.decision='selected';entry.reason=''
    else:raise ValueError('Choose a selection or no strong moment.')
    bundle.independent_before_system_outputs=False;bundle.recorded_at=None
    f.parent.mkdir(parents=True,exist_ok=True);f.write_text(bundle.model_dump_json(indent=2)+'\n')
    return references(pid)

class FinalizeInput(BaseModel):
    evaluator:str=Field(min_length=1,max_length=100)
    independent:bool

@app.post('/api/projects/{pid}/references/finalize')
def finalize(pid:str,body:FinalizeInput):
    p,eps,f,primary,bundle,locked=reference_state(pid)
    if pid!='assignment':raise ValueError('New-project examples are saved as editorial notes. Independent evaluation needs a reserved split before AI output exposure.')
    if not body.independent:raise ValueError('Confirm your own independent selections before finalizing.')
    bundle.evaluator=body.evaluator.strip();bundle.independent_before_system_outputs=True;bundle.recorded_at=datetime.now(timezone.utc)
    config=yaml.safe_load((ws.ROOT/'config.yaml').read_text())
    validate_references(bundle,eps,bundle.input_hash,config)
    f.write_text(bundle.model_dump_json(indent=2)+'\n')
    return references(pid)

@app.get('/api/projects/{pid}/export/{kind}')
def export(pid:str,kind:str):
    p=path(pid)
    if kind in ('promo-cuts-txt','promo-cuts-json','promo-cuts-csv'):
        cuts=ws.get_promo_cuts(pid)
        if kind=='promo-cuts-json':return Response(json.dumps(cuts,indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="promo-cuts.json"'})
        if kind=='promo-cuts-txt':return Response(cut_text(cuts),media_type='text/plain',headers={'Content-Disposition':'attachment; filename="promo-cut-sheet.txt"'})
        output=StringIO();fields=['cut_order','episode_id','start','end','seconds','delivery_kind','hook','cliffhanger','why_it_works','ai_status','human_verdict','warnings','origin_project_id','candidate_id']
        writer=csv.DictWriter(output,fieldnames=fields);writer.writeheader()
        for cut in cuts:writer.writerow({'cut_order':cut['cut_order'],'episode_id':cut['episode_id'],
            'start':format_time(cut['start_ms']),'end':format_time(cut['end_ms']),'seconds':(cut['end_ms']-cut['start_ms'])/1000,
            'delivery_kind':cut['delivery_kind'],'hook':cut.get('hook_quote') or cut.get('hook',''),
            'cliffhanger':cut.get('cliffhanger_quote') or cut.get('cliffhanger',''),'why_it_works':cut.get('why_it_works',''),
            'ai_status':cut['status'],'human_verdict':(cut.get('editor_note') or {}).get('verdict','not_reviewed'),
            'warnings':'; '.join(cut.get('editor_review_reasons',[])+cut.get('warnings',[])),
            'origin_project_id':cut['origin_project_id'],'candidate_id':cut['candidate_id']})
        return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="promo-cuts.csv"'})
    if kind=='raw-outputs':
        handle=BytesIO()
        with zipfile.ZipFile(handle,'w',zipfile.ZIP_DEFLATED) as archive:
            for name in ('episodes.jsonl','ingestion_report.json','config.json','project.json','job.json'):
                if (p/name).exists():archive.write(p/name,name)
            for f in sorted((p/'batch').rglob('*')):
                if f.is_file() and f.suffix in ('.json','.jsonl','.csv'):archive.write(f,str(f.relative_to(p)))
        return Response(handle.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="private-raw-outputs.zip"'})
    if kind=='coverage':return Response(json.dumps(ws.metadata(pid)['ingestion'],indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="ingestion-report.json"'})
    if kind=='references':return Response(json.dumps(references(pid),indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="human-reference-picks.json"'})
    all_rows=ws.get_candidates(pid)
    if kind=='shortlist-json':rows=[r for r in all_rows if r['status']=='ai_eligible']
    elif kind=='editor-approved-json':rows=[r for r in all_rows if (r.get('editor_note') or {}).get('verdict')=='accept']
    elif kind=='all-json':rows=all_rows
    elif kind=='shortlist-csv':
        view=ws.decision_view(p)/'promos.csv'
        if view.exists():return FileResponse(view,media_type='text/csv',filename='promo-shortlist.csv')
        if (p/'batch/promos.csv').exists():return FileResponse(p/'batch/promos.csv',media_type='text/csv',filename='promo-shortlist.csv')
        values=[r for r in all_rows if r['status']=='ai_eligible'];handle=StringIO()
        fields=['rank','episode_id','start_ms','end_ms','duration_ms','status','hook_quote','hook','cliffhanger','why_it_works','post_first_reason','cliffhanger_type','config_hash']
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows({k:r.get(k,'') for k in fields} for r in values)
        return Response(handle.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="promo-shortlist.csv"'})
    else:raise HTTPException(404,'Unknown export')
    return Response(json.dumps(rows,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="{kind}.json"'})

DIST=ws.ROOT/'web/dist'
if (DIST/'assets').exists():app.mount('/assets',StaticFiles(directory=DIST/'assets'),name='assets')
@app.get('/')
def index():
    if not (DIST/'index.html').exists():return {'message':'Build the React UI: npm --prefix web run build, then restart the API.'}
    return FileResponse(DIST/'index.html')
