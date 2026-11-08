"""Structural coverage only; no semantic judgments or held-out recommendations."""
import argparse
from bisect import bisect_left, bisect_right
import json
from pathlib import Path

from src.ingestion import ingest
from src.boundaries import usable_boundaries


def measure(episodes):
    rows=[]
    for ep in episodes:
        points=usable_boundaries(ep)
        count=sum(bisect_right(points,p+60000)-bisect_left(points,p+20000) for p in points)
        rows.append({'episode_id':ep.episode_id,'source_ready':ep.source_ready,
                     'usable_points':len(points),'valid_20_60s_pairs':count})
    return {'semantic_assessment':False,'api_calls':0,'episodes':rows,
            'episodes_with_valid_pairs':sum(r['valid_20_60s_pairs']>0 for r in rows),
            'episodes_with_at_least_three_pairs':sum(r['valid_20_60s_pairs']>=3 for r in rows),
            'total_episodes':len(rows),
            'note':'Pair counts measure structural opportunity, not promo quality or hook/cliffhanger coverage.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    episodes,_=ingest(args.input)
    result=measure(episodes)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='episodes'},indent=2))


if __name__=='__main__':main()
