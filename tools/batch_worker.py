"""Bounded resumable worker. Waits are short; daily-budget stalls stop cleanly."""
import argparse
import json
from pathlib import Path
import time
import yaml
from src.ingestion import ingest,save_ingestion
from src.batch import run_batch


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=root/'config.yaml')
    parser.add_argument('--max-minutes',type=int,default=45)
    parser.add_argument('--profile',choices=['groq'],default='groq')
    parser.add_argument('--episodes',help='Comma-separated episode numbers, or all (default: all usable episodes)')
    args=parser.parse_args()
    if not args.output.resolve().is_relative_to(root/'runs'):parser.error('Private output must be in project runs/')
    config=yaml.safe_load(args.config.read_text())
    if args.profile:
        from src.profiles import apply_profile
        config=apply_profile(config,args.profile)
    if args.episodes:config['batch']['ai_episode_numbers']=[] if args.episodes=='all' else [int(n) for n in args.episodes.split(',')]
    episodes,report=ingest(args.input)
    args.output.mkdir(parents=True,exist_ok=True)
    # run_batch checks an existing manifest before mutating its own artifacts.
    manifest=args.output/'batch/manifest.json'
    if manifest.exists():
        from src.evaluation import config_hash
        old=json.loads(manifest.read_text())
        if old['input_hash']!=report['input_hash'] or old['config_hash']!=config_hash(config):
            parser.error('Resume config/input mismatch; use a fresh directory')
    else:save_ingestion(episodes,report,args.output)
    stop=time.monotonic()+args.max_minutes*60
    retries=0
    while True:
        result=run_batch(root,episodes,report['input_hash'],config,args.output/'batch')
        print(json.dumps({k:v for k,v in result.items() if k!='coverage'}),flush=True)
        if result['status']=='complete_subset':return 0
        if result['status']=='paused_budget' and result.get('retry_after_seconds') is None:return 3
        wait=result.get('retry_after_seconds') or 30
        if wait>120 or time.monotonic()+wait>=stop:return 3
        if result['status']=='paused_provider':
            retries+=1
            if retries>3:return 3
        # Respect provider/backoff waits without a single blocking sleep over 30s.
        remaining=wait+1
        while remaining>0:
            step=min(30,remaining);time.sleep(step);remaining-=step


if __name__=='__main__':raise SystemExit(main())
