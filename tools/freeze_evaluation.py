"""Freeze effective settings and inference-critical code before held-out exposure."""
from datetime import datetime,timezone
import argparse
import hashlib
import json
from pathlib import Path
import yaml
from src.evaluation import config_hash
from src.ingestion import ingest

FILES=['src/prompts.py','src/review_models.py','src/candidates.py','src/analysis_planning.py',
       'src/discovery.py','src/coverage.py','src/editor_policy.py','src/show_context.py','src/context_view.py','src/profiles.py',
       'src/story.py','src/thin_slice.py','src/batch.py','src/ranking.py','src/exports.py',
       'src/provider.py','src/budget.py','src/evaluation.py','src/parser.py','src/validation.py',
       'src/models.py','src/boundaries.py','src/timeline.py','src/ingestion.py',
       'src/input_adapters.py','tools/evaluate.py','uv.lock']


def code_hashes(root):
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in FILES}


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=root/'config.yaml')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Freeze record exists; preserve it and choose a new version')
    config=yaml.safe_load(args.config.read_text());_,report=ingest(args.input)
    value={'config_hash':config_hash(config),'input_hash':report['input_hash'],'code_hashes':code_hashes(root),
           'frozen_at':datetime.now(timezone.utc).isoformat(),'config':config,
           'note':'Freeze is not proof of human labeling; that is validated separately.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,indent=2)+'\n')
    print('Frozen input/config/inference-code identities. No model calls.')


if __name__=='__main__':main()
