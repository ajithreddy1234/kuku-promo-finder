"""Check independently recorded human references without generating AI outputs."""
import argparse
from pathlib import Path
import sys
import zipfile
import yaml
from pydantic import ValidationError

from src.evaluation import ReferenceBundle, validate_references
from src.ingestion import ingest


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--references',type=Path,default=root/'evaluation/private/human_references.json')
    parser.add_argument('--config',type=Path,default=root/'config.yaml')
    args=parser.parse_args()
    try:
        episodes,report=ingest(args.input)
        config=yaml.safe_load(args.config.read_text())
        bundle=ReferenceBundle.model_validate_json(args.references.read_text())
        validate_references(bundle,episodes,report['input_hash'],config)
    except ValidationError:
        print('Reference schema is invalid. Use the prepared template and integer milliseconds.',file=sys.stderr)
        return 1
    except (ValueError,OSError,zipfile.BadZipFile) as exc:
        print('References not ready:',str(exc),file=sys.stderr)
        return 1
    print(f'Reference validation passed for {len(bundle.entries)} episodes. No model calls made.')
    return 0


if __name__=='__main__':
    sys.exit(main())
