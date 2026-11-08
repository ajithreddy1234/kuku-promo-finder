"""Bounded UI wrapper around the same frozen, reference-gated evaluation CLI."""
import json
import subprocess
import sys
import threading
import time
from . import workspace as ws
from .batch import save
from .evaluation import ReferenceBundle,validate_references
from .ingestion import ingest
from tools.freeze_evaluation import code_hashes


def launch():
    private=ws.ROOT/'evaluation/private'
    record=private/'freeze-index.json';location=private/'source_location.json'
    if not record.exists() or not location.exists():raise ValueError('The frozen evaluation source has not been configured.')
    active=json.loads(record.read_text())['active'];freeze=private/active
    frozen=json.loads(freeze.read_text())
    if frozen['code_hashes']!=code_hashes(ws.ROOT):raise ValueError('Evaluation code changed after the freeze. Create a new freeze before seeing held-out results.')
    source=__import__('pathlib').Path(json.loads(location.read_text())['path'])
    eps,report=ingest(source)
    if report['input_hash']!=frozen['input_hash']:raise ValueError('Frozen evaluation input changed.')
    bundle=ReferenceBundle.model_validate_json((private/'human_references.json').read_text())
    validate_references(bundle,eps,report['input_hash'],frozen['config'])
    with ws.LOCK:
        if ws.ACTIVE:raise ValueError('Wait for the active discovery job before starting evaluation.')
        ws.ACTIVE.add('evaluation')
    output=ws.ROOT/'runs'/f'held-out-{freeze.stem}'
    save(private/'evaluation_job.json',{'status':'running','message':'Running the frozen semantic and matched baseline methods.','started_at':ws.now()})
    threading.Thread(target=worker,args=(source,freeze,output),daemon=True).start()


def worker(source,freeze,output):
    state=ws.ROOT/'evaluation/private/evaluation_job.json'
    stop=time.monotonic()+1800
    try:
        while True:
            # Argument arrays avoid shell interpolation. CLI validates before inference.
            result=subprocess.run([sys.executable,'-m','tools.evaluate','--input',str(source),
                '--freeze',str(freeze),'--output',str(output)],cwd=ws.ROOT,capture_output=True,text=True)
            log=ws.ROOT/'evaluation/private/evaluation_worker.log'
            with log.open('a') as handle:handle.write(result.stdout+result.stderr)
            if result.returncode==0:
                save(state,{'status':'complete','message':'Matched evaluation finished. Human match confirmation is still needed.',
                    'updated_at':ws.now(),'results':json.loads((output/'evaluation.json').read_text())})
                return
            paused=None
            for line in result.stdout.splitlines():
                try:
                    value=json.loads(line)
                    if isinstance(value,dict) and 'status' in value:paused=value
                except ValueError:pass
            delay=paused.get('retry_after_seconds') if paused else None
            if delay and delay<=120 and time.monotonic()+delay<stop:
                save(state,{'status':'running','message':'Free-tier pacing pause; completed evaluation responses are cached.','updated_at':ws.now()})
                end=time.monotonic()+delay+1
                while time.monotonic()<end:time.sleep(min(5,max(.01,end-time.monotonic())))
                continue
            save(state,{'status':'paused' if result.returncode==3 else 'failed',
                'message':paused.get('reason') if paused else 'Evaluation stopped. Check the private worker log; original artifacts are preserved.',
                'updated_at':ws.now(),'retry_after_seconds':delay})
            return
    except Exception as exc:
        save(state,{'status':'failed','message':'Evaluation stopped safely. See the private worker log.','error_type':type(exc).__name__})
    finally:
        with ws.LOCK:ws.ACTIVE.discard('evaluation')
