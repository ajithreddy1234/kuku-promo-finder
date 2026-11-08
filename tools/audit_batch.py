"""Offline all-candidate audit; failed/unreviewed candidates remain visible."""
import argparse
import csv
import json
from pathlib import Path


def audit(batch,output):
    report=json.loads((batch/'batch_report.json').read_text())
    reviewed={r['candidate_id']:r for r in json.loads((batch/'candidate_reviews.json').read_text())}
    rows=[]
    for queue in sorted((batch/'episodes').glob('*/review_queue.json')):
        for c in json.loads(queue.read_text()):
            cid=c['candidate_id'];blind=batch/f'reviews/blind/{cid}.json'
            b=json.loads(blind.read_text()) if blind.exists() else {}
            decision=batch/f'blind_decisions/{cid}.json'
            d=json.loads(decision.read_text()) if decision.exists() else {}
            failed=(batch/f'review_failures/blind-{cid}.json').exists() or (batch/f'review_failures/context-{cid}.json').exists()
            rows.append({'candidate_id':cid,'episode_id':c['episode_id'],'start_ms':c['start_ms'],'end_ms':c['end_ms'],
                'status':reviewed[cid]['status'] if cid in reviewed else 'failed_not_recommended' if failed else d.get('status','not_reviewed'),
                'blind_complete':bool(b),'context_complete':(batch/f'reviews/context/{cid}.json').exists(),
                'hook_score':b.get('hook_score'),'cliffhanger_score':b.get('cliffhanger_score'),
                'standalone_clarity_score':b.get('standalone_clarity_score'),'ending_resolved':b.get('ending_resolved'),
                'cliffhanger_type':b.get('cliffhanger_type','not_assessed'),'human_reviewed':False})
    output.mkdir(parents=True,exist_ok=True)
    for name,values in [('candidate_audit',rows),('episode_coverage',report['coverage'])]:
        (output/f'{name}.json').write_text(json.dumps(values,indent=2)+'\n')
        with (output/f'{name}.csv').open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=list(values[0]) if values else ['status'])
            writer.writeheader();writer.writerows(values)
    return {'queued':len(rows),'blind_complete':sum(r['blind_complete'] for r in rows),
        'context_complete':sum(r['context_complete'] for r in rows),'statuses':{s:sum(r['status']==s for r in rows) for s in sorted({r['status'] for r in rows})},
        'model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(audit(args.batch,args.output),indent=2))
