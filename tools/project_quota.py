"""Estimate workloads from real private usage logs; never assume daily capacity."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import yaml


def project(folders,config=None,analysis_calls=50):
    successful=defaultdict(list)
    seen=set()
    actual=[]
    limits={}
    for folder in folders:
        usage=folder/'usage.jsonl'
        if usage.exists():
            for line in usage.read_text().splitlines():
                row=json.loads(line)
                if row.get('cache_hit'):continue
                actual.append(row)
                if row.get('rate_limits'):
                    limits[row['model']]=row['rate_limits']
        raw=folder/'raw_responses.jsonl'
        if raw.exists():
            for line in raw.read_text().splitlines():
                row=json.loads(line)
                key=row['request_hash']
                if key in seen:continue
                envelope=row.get('response',{})
                usage=envelope.get('usage') or {}
                if usage.get('total_tokens') is None:continue
                if not envelope.get('choices') or envelope['choices'][0].get('finish_reason')!='stop':continue
                seen.add(key)
                successful[row['role']].append(usage)
    config=config or yaml.safe_load((Path(__file__).resolve().parents[1]/'config.yaml').read_text())
    count=config['candidates']['blind_reviewed_per_episode']
    baseline_count=len(config['evaluation']['baseline_comparison_episode_numbers'])
    baseline_context=config['evaluation']['baseline_context_target']
    planned={'analysis':analysis_calls,'blind':50*count+baseline_count*count,
             'context':config['review']['context_initial_pool']+baseline_context}
    maximum={**planned,'context':planned['context']+config['review']['context_additional_cap']}
    rows={}
    for role,values in successful.items():
        means={k:round(sum(v.get(k) or 0 for v in values)/len(values),1)
               for k in ('prompt_tokens','completion_tokens','total_tokens')}
        rows[role]={'observed_distinct_successful_requests':len(values),'mean_tokens':means,
                    'planned_requests_initial':planned[role],'planned_requests_with_context_reserves':maximum[role],
                    'estimated_tokens_initial':round(means['total_tokens']*planned[role]),
                    'estimated_tokens_with_context_reserves':round(means['total_tokens']*maximum[role])}
    return {'roles':rows,'observed_api_attempts_in_logs':sum(r.get('api_calls',0) for r in actual),
            'observed_tokens_lower_bound':sum(r.get('total_tokens') or 0 for r in actual),
            'attempts_missing_token_usage':sum(r.get('total_tokens') is None for r in actual),
            'observed_response_limits':limits,'account_daily_token_limit':'not returned; needs dashboard confirmation',
            'estimated_initial_tokens':sum(r['estimated_tokens_initial'] for r in rows.values()),
            'estimated_with_context_reserves':sum(r['estimated_tokens_with_context_reserves'] for r in rows.values()),
            'limitations':['Small development sample, not exact full-pack tokenization.',
                          'Cache-origin response usage measures logical request cost, not a second charge.',
                          'Actual attempted calls include failed and resumed development requests.',
                          'Daily quota, reset timing and model-specific sharing must be checked before full execution.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',nargs='+',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=Path(__file__).resolve().parents[1]/'config.yaml')
    parser.add_argument('--analysis-plan',type=Path,help='Optional offline source-chunk plan with planned_analysis_calls')
    args=parser.parse_args()
    count=json.loads(args.analysis_plan.read_text())['planned_analysis_calls'] if args.analysis_plan else 50
    result=project(args.runs,yaml.safe_load(args.config.read_text()),count)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('observed_response_limits','roles')},indent=2))


if __name__=='__main__':main()
