"""Ingestion and deterministic candidates; no live inference at these stages."""
import argparse
import json
from pathlib import Path
import sys
import zipfile
import yaml

from src.ingestion import ingest, save_ingestion
from src.deterministic import run_deterministic, load_signals
from src.evaluation import reference_template
from src.budget import BudgetPause


def main():
    parser = argparse.ArgumentParser(description="Promo Finder: validated screenplay ingestion")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("runs/ingestion"))
    parser.add_argument("--stage", choices=["ingest","candidates","prepare-references","thin-slice"], default="ingest")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent/'config.yaml')
    parser.add_argument("--phase", choices=['development','tuning'], default='development')
    parser.add_argument("--signals", type=Path)
    parser.add_argument("--episode",type=int,default=1)
    parser.add_argument("--resume",action='store_true')
    parser.add_argument("--analysis-model",help='One-line Groq story-model override; recorded in config hash')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    dest = args.output.resolve()
    if not dest.is_relative_to(root / "runs"):
        parser.error("Use a private output directory inside this project's runs/ folder")
    if args.resume and args.stage!='thin-slice':parser.error('Resume currently supports thin-slice only')
    if not args.resume and dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        parser.error("Output directory is not empty; use a fresh run directory")
    try:
        episodes, report = ingest(args.input)
        config=yaml.safe_load(args.config.read_text()) if args.stage!='ingest' else None
        if args.analysis_model:
            if args.stage!='thin-slice':raise ValueError('Model override applies to thin-slice')
            config['models']['analysis']['model']=args.analysis_model
        signals=load_signals(args.signals) if args.stage=='candidates' else []
        if args.resume:
            from src.evaluation import config_hash
            existing=args.output/'live/manifest.json'
            target=next((e for e in episodes if e.episode_number==args.episode),None)
            expected={'input_hash':report['input_hash'],'config_hash':config_hash(config),
                      'episode_id':target.episode_id if target else None}
            if not existing.exists() or json.loads(existing.read_text())!=expected:
                raise ValueError('Resume identity mismatch; original artifacts were not changed')
        save_ingestion(episodes, report, args.output)
        if args.stage=='prepare-references':
            bundle=reference_template(episodes,report['input_hash'],config)
            (args.output/'human_references.pending.json').write_text(bundle.model_dump_json(indent=2)+'\n')
        elif args.stage=='candidates':
            result=run_deterministic(episodes,config,args.output/'deterministic',args.phase,signals)
            print(json.dumps(result,indent=2))
        elif args.stage=='thin-slice':
            from src.thin_slice import run_thin_slice
            result=run_thin_slice(root,episodes,report['input_hash'],config,args.output/'live',args.episode)
            print(json.dumps(result,indent=2))
    except BudgetPause as exc:
        print(json.dumps({'status':'paused_budget','reason':exc.reason,
                          'retry_after_seconds':exc.retry_after,'blocked_request_api_calls':0}),file=sys.stderr)
        return 3
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        print("Ingestion failed:", type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({k:v for k,v in report.items() if k != "files"}, indent=2))
    return 0 if report["complete_ingestion"] else 1


if __name__ == "__main__":
    sys.exit(main())
