"""Three fixed development cases; separate-family pilot never enters main ranking."""
import argparse
import json
from pathlib import Path
import yaml
from src.models import Episode
from src.candidates import Candidate
from src.provider import JsonProvider
from src.provider import ProviderFailure
from src.budget import BudgetPause
from src.review_models import BlindReview
from src.thin_slice import blind_package
from src import prompts


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    config=json.loads((args.batch/'effective_config.json').read_text())
    selected=config['batch']['ai_episode_numbers'][:2]
    candidates=[]
    for index,number in enumerate(selected):
        p=args.batch/f'episodes/E{number:03d}/review_queue.json'
        if not p.exists():raise ValueError('Pilot waits for fixed cases to exist')
        values=[Candidate.model_validate(x) for x in json.loads(p.read_text())]
        candidates+=values[:2] if index==0 else values[:1]
    if len(candidates)!=3:raise ValueError('Need three fixed pilot cases')
    if any(not (args.batch/f'reviews/blind/{c.candidate_id}.json').exists() and
           not (args.batch/f'review_failures/blind-{c.candidate_id}.json').exists() for c in candidates):
        raise ValueError('Main fixed-case blind results not ready; no pilot call made')
    args.output.mkdir(parents=True,exist_ok=True)
    manifest={'cases':[c.candidate_id for c in candidates],'selection':'first two queue entries from first sampled episode; first from second; not selected by scores',
              'main_model':config['models']['blind']['model'],'pilot_model':config['models']['pilot_blind']['model']}
    path=args.output/'manifest.json'
    if path.exists() and json.loads(path.read_text())!=manifest:raise ValueError('Pilot case/config mismatch')
    path.write_text(json.dumps(manifest,indent=2)+'\n')
    api=JsonProvider(root,config,args.output)
    results=[]
    for c in candidates:
        payload,mapping=blind_package(c)
        def validate(value):
            if not set(value.hook_evidence_ids+value.cliffhanger_evidence_ids)<=set(mapping):raise ValueError('Unknown pilot evidence')
        value=api.request('pilot_blind',f'pilot:{c.candidate_hash}',prompts.BLIND,payload,BlindReview,validate)
        main_path=args.batch/f'reviews/blind/{c.candidate_id}.json'
        main=BlindReview.model_validate_json(main_path.read_text()) if main_path.exists() else None
        results.append({'candidate_id':c.candidate_id,'main':main.model_dump() if main else None,'pilot':value.model_dump(),
                        'main_status':'complete' if main else 'failed_response',
                        'same_clear_to_new_viewer':value.clear_to_new_viewer==main.clear_to_new_viewer if main else None,
                        'same_ending_resolved':value.ending_resolved==main.ending_resolved if main else None,'human_reviewed':False})
        (args.output/'pilot_results.json').write_text(json.dumps({'manifest':manifest,'cases':results,
             'rankings_affected':False,'limitations':'Three development cases, no human ground truth. API structured modes differ.'},indent=2)+'\n')
    print(json.dumps({'pilot_cases':len(results),'rankings_affected':False,'model':manifest['pilot_model']}))


if __name__=='__main__':
    try:main()
    except BudgetPause as exc:
        print(json.dumps({'status':'paused_budget','reason':exc.reason,'retry_after_seconds':exc.retry_after,'resume':'Repeat the same command; completed responses are cached.'}))
        raise SystemExit(3)
    except ProviderFailure as exc:
        print(json.dumps({'status':'paused_provider','reason':str(exc)}))
        raise SystemExit(3)
