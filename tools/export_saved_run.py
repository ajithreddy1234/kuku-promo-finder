"""Re-export evidence-rich saved results OFFLINE; original runs stay unchanged."""
import argparse
import json
from pathlib import Path
from src.models import Episode
from src.candidates import Candidate
from src.exports import enrich_review, write_review_exports


def export_saved(source,output):
    if output.resolve()==source.resolve() or (output.exists() and any(output.iterdir())):
        raise ValueError('Use a fresh export destination; preserve original run artifacts')
    manifest=json.loads((source/'manifest.json').read_text())
    config=json.loads((source/'effective_config.json').read_text())
    report=json.loads((source/'thin_slice_report.json').read_text())
    episodes={e.episode_id:e for e in (Episode.model_validate_json(x) for x in (source.parent/'episodes.jsonl').read_text().splitlines())}
    candidates={e.candidate_id:e for e in (Candidate.model_validate_json(x) for x in (source/'review_queue.jsonl').read_text().splitlines())}
    rows=json.loads((source/'candidate_reviews.json').read_text())
    output.mkdir(parents=True,exist_ok=True)
    updated=[enrich_review(r,candidates[r['candidate_id']],episodes[r['episode_id']],
        {**manifest,'models':config['models'],'complete':report.get('complete',False),
         'duration_margin_warning_ms':500}) for r in rows]
    ranked=write_review_exports(output,updated)
    summary={'source_run':source.name,'source_config_hash':manifest['config_hash'],'complete':report.get('complete',False),
             'reviewed':len(rows),'pending':report.get('pending',0),'ai_eligible':len(ranked),
             'human_reviewed':False,'model_calls':0,
             'cliffhanger_types':{t:sum(r['cliffhanger_type']==t for r in updated) for t in sorted({r['cliffhanger_type'] for r in updated})},
             'note':'New display/export schema only. Original judgments, raw responses and source config preserved.'}
    (output/'export_manifest.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(export_saved(args.run,args.output),indent=2))


if __name__=='__main__':main()
