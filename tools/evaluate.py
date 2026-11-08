"""Matched small held-out run, only after real labels and frozen-code checks."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import yaml
from src.evaluation import ReferenceBundle,validate_references,config_hash
from src.ingestion import ingest,save_ingestion
from src.batch import run_batch
from src.boundaries import interval_iou
from tools.freeze_evaluation import code_hashes


def temporal_recovery(bundle,rows,required,episodes,threshold=0.5):
    ids={e.episode_id for e in episodes if e.episode_number in required}
    references=[(e.episode_id,s) for e in bundle.entries if e.episode_id in ids for s in e.selections]
    hits=[]
    for episode,selection in references:
        matches=[r['candidate_id'] for r in rows if r['episode_id']==episode and
                 interval_iou(selection.start_ms,selection.end_ms,r['start_ms'],r['end_ms'])>=threshold]
        hits.append({'episode_id':episode,'start_ms':selection.start_ms,'end_ms':selection.end_ms,
                     'temporal_candidate_matches':matches,'editorial_confirmation':None})
    return {'reference_opportunities':len(references),'temporal_matches':sum(bool(h['temporal_candidate_matches']) for h in hits),
            'temporal_sample_recovery':sum(bool(h['temporal_candidate_matches']) for h in hits)/len(hits) if hits else None,
            'editorially_confirmed_recovery':None,'matches':hits,
            'note':'IoU matches are only proposals. Human confirmation of hook/ending is still required. Three episodes are anecdotal.'}


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--freeze',type=Path,required=True)
    parser.add_argument('--references',type=Path,default=root/'evaluation/private/human_references.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.output.resolve().is_relative_to(root/'runs'):parser.error('Use private project runs/')
    frozen=json.loads(args.freeze.read_text());config=frozen['config']
    episodes,report=ingest(args.input)
    if frozen['input_hash']!=report['input_hash'] or frozen['code_hashes']!=code_hashes(root):
        parser.error('Frozen source/code mismatch; do not silently change the held-out experiment')
    bundle=ReferenceBundle.model_validate_json(args.references.read_text())
    required=config['evaluation']['primary_reference_episode_numbers']
    # This check occurs before constructing any provider or writing exposure records.
    try:validate_references(bundle,episodes,report['input_hash'],config)
    except ValueError as exc:parser.error('Evaluation locked: '+str(exc))
    args.output.mkdir(parents=True,exist_ok=True)
    digest=__import__('hashlib').sha256(args.references.read_bytes()).hexdigest()
    ref_hash=args.output/'reference_hash.txt'
    if ref_hash.exists() and ref_hash.read_text()!=digest:parser.error('References changed after evaluation began')
    ref_hash.write_text(digest)
    save_ingestion(episodes,report,args.output)
    exposure=root/'evaluation/private/held_out_exposure.jsonl'
    exposure.parent.mkdir(parents=True,exist_ok=True)
    with exposure.open('a') as handle:
        for number in required:handle.write(json.dumps({'episode_id':f'E{number:03d}','scheduled_at':datetime.now(timezone.utc).isoformat(),'reference_hash':digest})+'\n')
    results={}
    for method in ('semantic','baseline'):
        folder=args.output/method
        result=run_batch(root,episodes,report['input_hash'],config,folder,selected_numbers=required,
                         reference_bundle=bundle,frozen_hash=frozen['config_hash'],method=method)
        if result['status']!='complete_subset':print(json.dumps(result));return 3
        rows=json.loads((folder/'promos.json').read_text())
        results[method]=temporal_recovery(bundle,rows,required,episodes,config['evaluation']['matching']['minimum_time_iou'])
    (args.output/'evaluation.json').write_text(json.dumps(results,indent=2)+'\n')
    print('Matched reference run completed; editorial confirmation remains a human task.')
    return 0


if __name__=='__main__':raise SystemExit(main())
