"""Step 4a runner: no SDK imports, no API calls, no creative scores."""
import csv
import json
from pathlib import Path

from .candidates import StorySignal, assemble
from .evaluation import split_sets, config_hash
from .timeline import render_timeline, format_time


def run_deterministic(episodes, config, destination: Path, phase='development', signals=None):
    splits=split_sets(config)
    if config['candidates']['pruning_policy']!='semantic_distinct_opening_wildcard_v1':
        raise ValueError('Unsupported pruning policy')
    if config['promo']!={'min_seconds':20,'max_seconds':60,'max_segments':1}:
        raise ValueError('This stage implements continuous 20–60 second candidates only')
    if phase not in ('development','tuning'):
        raise ValueError('This stage only exposes development or tuning candidates')
    selected=[e for e in episodes if e.episode_number in splits[phase]]
    if not splits[phase] and phase=='development':
        selected=[e for e in episodes if e.episode_number not in splits['held_out']]
    if not selected:
        raise ValueError('No episodes match this development/tuning split')
    signals=signals or []
    known={e.episode_id for e in selected}
    if any(s.episode_id not in known for s in signals):
        raise ValueError('Signal file includes an episode outside the requested split')
    destination.mkdir(parents=True,exist_ok=True)
    rows, summaries=[],[]
    for episode in selected:
        result=assemble(episode,[s for s in signals if s.episode_id==episode.episode_id],
            raw_cap=config['candidates']['raw_per_episode'],
            retained_cap=config['candidates']['assembled_per_episode'],
            review_cap=config['candidates']['blind_reviewed_per_episode'],
            overlap=config['candidates']['near_duplicate_iou'])
        folder=destination/episode.episode_id
        folder.mkdir()
        (folder/'timeline.txt').write_text(render_timeline(episode),encoding='utf-8')
        for key in ('raw','retained','review_queue','baseline_queue'):
            (folder/f'{key}.jsonl').write_text(''.join(c.model_dump_json()+'\n' for c in result[key]),encoding='utf-8')
        (folder/'candidate_decisions.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in result['ledger']),encoding='utf-8')
        for c in result['review_queue']:
            rows.append({'candidate_id':c.candidate_id,'candidate_version':c.candidate_version,
                         'episode_id':c.episode_id,'start':format_time(c.start_ms),'end':format_time(c.end_ms),
                         'duration_seconds':c.duration_ms/1000,'source':c.source,'status':c.status,
                         'warnings':';'.join(c.warnings)})
        summaries.append({'episode_id':episode.episode_id,'source_ready':result['source_ready'],
                          'enumerated':result['enumerated'],'raw':len(result['raw']),
                          'retained':len(result['retained']),'review_queue':len(result['review_queue']),
                          'baseline_queue':len(result['baseline_queue']),
                          'signal_origins':sorted({s.origin for s in signals if s.episode_id==episode.episode_id})})
    fields=['candidate_id','candidate_version','episode_id','start','end','duration_seconds','source','status','warnings']
    with (destination/'unreviewed_candidates.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader();writer.writerows(rows)
    summary={'stage':'4a','mode':'deterministic_unreviewed','api_calls':0,'config_hash':config_hash(config),
             'phase':phase,'episodes':summaries,'recommended_promos':0,
             'note':'No editorial judgments or recommendations. Empty signal lists use windows only.'}
    (destination/'deterministic_report.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


def load_signals(path: Path | None):
    if path is None:
        return []
    return [StorySignal.model_validate(x) for x in json.loads(path.read_text())]
