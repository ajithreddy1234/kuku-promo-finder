"""Source-linked whole-show index and bounded lexical evidence retrieval.

No embeddings/API call. Summaries are model hypotheses, never verified facts.
Original source records remain the evidence contract.
"""
import math
import re
from collections import Counter

STOP=set('the a an and or to of in is it this that what will would could should who why how for with her his she he they you your i me my we our not be on as at from but so if has have was are do does did'.split())


def terms(text):
    return Counter(w.lower() for w in re.findall(r'[A-Za-z][A-Za-z\-]{2,}',text) if w.lower() not in STOP)


def build_index(episodes,analyses,excluded_numbers=()):
    excluded=set(excluded_numbers)
    records=[];signals=[];df=Counter()
    for ep in episodes:
        if not ep.source_ready or ep.episode_number in excluded:continue
        for e in ep.temporal_events():
            text=f'{e.speaker_raw or ""} {e.text}';tokens=terms(text);df.update(tokens.keys())
            records.append({'episode_id':ep.episode_id,'episode_number':ep.episode_number,
                'id':e.event_id,'type':e.event_type,'start_ms':e.start_ms,'end_ms':e.end_ms,
                'speaker':e.speaker_raw,'text':e.text,'terms':dict(tokens)})
        analysis=analyses.get(ep.episode_id)
        if analysis:
            signals.extend({'episode_id':ep.episode_id,**s.model_dump()} for s in analysis.signals)
    return {'version':'source-index-v1','source_episode_ids':sorted({r['episode_id'] for r in records}),
        'ai_analyzed_episode_ids':sorted(analyses),'signals_are_hypotheses':True,
        'retrieval':'bounded lexical overlap; may miss paraphrases and cross-language links',
        'records':records,'document_frequency':dict(df),'signals':signals}


def retrieve(index,candidate,blind,max_records=6,max_chars=6500):
    query=terms(' '.join([blind.hook,blind.cliffhanger,*[e.text for e in candidate.selected_content if e.content_status=='full']]))
    records=index.get('records',[]);df=index.get('document_frequency',{});n=max(1,len(records))
    scored=[]
    for r in records:
        # The normal source package already covers the selected neighbourhood.
        if r['episode_id']==candidate.episode_id and r['end_ms']>candidate.start_ms-15000 and r['start_ms']<candidate.end_ms+15000:continue
        counts=r['terms'];length=max(1,sum(counts.values()))
        score=sum((1+math.log((n+1)/(df.get(t,0)+1)))*min(2,counts.get(t,0)) for t in query)/(1+length/70)
        if score>0:scored.append((score,r))
    scored.sort(key=lambda x:(-x[0],x[1]['episode_number'],x[1]['start_ms'],x[1]['id']))
    selected=[];size=0;per_episode=Counter()
    for score,r in scored:
        if size+len(r['text'])>max_chars or per_episode[r['episode_id']]>=2:continue
        selected.append({k:v for k,v in r.items() if k!='terms'});size+=len(r['text']);per_episode[r['episode_id']]+=1
        if len(selected)>=max_records:break
    return {'coverage_episode_ids':index.get('source_episode_ids',[]),
        'ai_analyzed_episode_ids':index.get('ai_analyzed_episode_ids',[]),
        'retrieval_is_exhaustive':False,'records':selected}
