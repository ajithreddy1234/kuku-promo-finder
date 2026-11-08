"""Malformed-response/error rates from observed logs; no inference requests."""
import argparse
from collections import defaultdict
import json
from pathlib import Path


def report(folders,cache_directory=None):
    models=defaultdict(lambda:{'api_attempts':0,'json_validate_failed':0,
                              'unknown_400':0,'local_validation_errors':0,'rate_limits':0,'connection_errors':0})
    requests=defaultdict(set)
    malformed=defaultdict(set)
    folders=sorted(set(folders))
    hash_models={}
    for folder in folders:
        usage=folder/'usage.jsonl'
        if usage.exists():
            for line in usage.read_text().splitlines():
                row=json.loads(line)
                if row.get('api_calls') and not row.get('cache_hit'):hash_models[row['request_hash']]=row['model']
    for folder in folders:
        lookup={}
        usage=folder/'usage.jsonl'
        if usage.exists():
            for line in usage.read_text().splitlines():
                row=json.loads(line)
                if row.get('cache_hit'):continue
                models[row['model']]['api_attempts']+=row.get('api_calls',0)
                if row.get('api_calls'):requests[row['model']].add(row['request_hash'])
                lookup[(row['request_hash'],row.get('attempt',1))]=row['model']
        failures=folder/'failures.jsonl'
        if failures.exists():
            for line in failures.read_text().splitlines():
                row=json.loads(line)
                if row.get('inherited_from'):continue
                model=row.get('model') or lookup.get((row['request_hash'],row.get('attempt',1))) or hash_models.get(row['request_hash'],'unknown')
                error=row.get('provider_error') or {}
                error=error.get('error',error)
                if error.get('code')=='json_validate_failed':
                    models[model]['json_validate_failed']+=1
                    malformed[model].add(row['request_hash'])
                elif row.get('status_code')==400:models[model]['unknown_400']+=1
                if row.get('status_code')==429:models[model]['rate_limits']+=1
                if row['error_type'] in ('ValidationError','ValueError'):models[model]['local_validation_errors']+=1
                if row['error_type'] in ('APIConnectionError','APITimeoutError'):models[model]['connection_errors']+=1
    for model,row in models.items():
        row['known_json_failure_rate']=round(row['json_validate_failed']/row['api_attempts'],4) if row['api_attempts'] else None
        row['unique_requests']=len(requests[model])
        row['unique_requests_with_json_failure']=len(malformed[model])
        row['deduplicated_json_affected_request_rate']=round(len(malformed[model])/len(requests[model]),4) if requests[model] else None
        if cache_directory is not None:
            row['json_affected_requests_eventually_validated']=sum((cache_directory/f'{h}.json').exists() for h in malformed[model])
            row['json_affected_requests_without_validated_cache']=len(malformed[model])-row['json_affected_requests_eventually_validated']
    return {'models':dict(models),'scope':'development only; known provider JSON failures are a lower bound',
            'note':'Unique requests use exact payload/model/prompt/settings hashes, not unique clips or independent quality samples. Affected means at least one JSON failure, even if a retry later succeeded. Unknown 400s are not assumed malformed. Cache absence is not proof of permanent failure.',
            'model_calls_by_this_tool':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',nargs='+',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    value=report(args.runs,Path(__file__).resolve().parents[1]/'runs/_cache')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(value,indent=2))


if __name__=='__main__':main()
