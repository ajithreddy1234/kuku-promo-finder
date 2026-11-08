"""Conservative app-local org/model accounting, never a promise of server quota."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
from pathlib import Path
import time
import uuid


class BudgetPause(RuntimeError):
    def __init__(self, reason, retry_after=None):
        super().__init__(reason)
        self.reason=reason
        self.retry_after=retry_after


def estimate_request(messages, schema, max_output_tokens, bytes_per_token=3.0):
    """Heuristic including schema/chat overhead; not an exact tokenizer."""
    size=len(json.dumps({'messages':messages,'schema':schema},ensure_ascii=False).encode())
    prompt=math.ceil(size/bytes_per_token)+128
    return {'prompt_tokens_estimate':prompt,'reserved_tokens':prompt+max_output_tokens,
            'estimator':'utf8_bytes_over_3_plus_128_chat_overhead', 'exact':False}


class BudgetLedger:
    def __init__(self, root: Path, config: dict, clock=time.time, sleep=time.sleep):
        self.root=root
        self.settings=config.get('quota',{})
        self.path=root/'runs/quota/ledger.jsonl'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.clock=clock;self.sleep=sleep
        self.org=self.settings.get('organization_namespace','single_organization')

    @contextmanager
    def locked(self):
        with self.path.open('a+',encoding='utf-8') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            handle.seek(0)
            events=[json.loads(x) for x in handle if x.strip()]
            try:yield handle,events
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

    def _write(self,handle,event):
        handle.seek(0,2);handle.write(json.dumps(event)+'\n');handle.flush()

    def limits(self,model):
        values=self.settings.get('models',{}).get(model)
        if not values:raise BudgetPause('No configured quota for this model; set explicit limits before live use')
        return values

    @staticmethod
    def usage(events,org,model,now):
        settlements={e['reservation_id']:e for e in events if e['type'] in ('settle','cache_credit')}
        rows=[]
        for event in events:
            if event['type'] not in ('reserve','import') or event['organization']!=org or event['model']!=model:
                continue
            settled=settlements.get(event['reservation_id'])
            charge=settled['charged_tokens'] if settled else event['reserved_tokens']
            rows.append({**event,'charged_tokens':charge,
                         'tokens_estimated':settled['tokens_estimated'] if settled else event.get('tokens_estimated',True)})
        active=[r for r in rows if now-r['timestamp']<86400]
        minute=[r for r in active if now-r['timestamp']<60]
        return active,minute

    def import_history(self):
        """Include past app use. Missing timestamps/tokens get conservative estimates."""
        pending=[]
        for usage in sorted((self.root/'runs').glob('**/usage.jsonl')):
            requests={}
            observed_times={}
            raw_file=usage.parent/'raw_responses.jsonl'
            if raw_file.exists():
                for line in raw_file.read_text().splitlines():
                    raw=json.loads(line)
                    if not raw.get('cache_hit') and raw.get('at'):
                        observed_times[raw['request_hash']]=datetime.fromisoformat(raw['at']).timestamp()
            request_file=usage.parent/'requests.jsonl'
            if request_file.exists():
                requests={r['request_hash']:r for r in (json.loads(x) for x in request_file.read_text().splitlines())}
            for line_number,line in enumerate(usage.read_text().splitlines(),1):
                row=json.loads(line)
                if row.get('cache_hit') or not row.get('api_calls'):continue
                if row.get('quota_reservation_id'):continue
                identity=f"{usage.relative_to(self.root)}:{line_number}"
                request=requests.get(row.get('request_hash'),{})
                if row.get('provider',request.get('model',{}).get('provider','groq'))!='groq':continue
                known=row.get('total_tokens')
                cached=row.get('cached_input_tokens')
                if known is not None and isinstance(cached,int):known=max(0,known-cached)
                estimate=(estimate_request(request.get('messages',[]),request.get('schema',{}),
                          request.get('settings',{}).get('max_output_tokens',4096))['reserved_tokens'])
                timestamp=row.get('timestamp',observed_times.get(row.get('request_hash')))
                if timestamp is None:timestamp=self.clock() # conservative 24h hold; legacy time not established
                pending.append({'type':'import','reservation_id':identity,'history_id':identity,
                    'organization':self.org,'provider':'groq','model':row['model'],'timestamp':timestamp,
                    'reserved_tokens':known if known is not None else estimate,
                    'timestamp_inferred':row.get('timestamp') is None and row.get('request_hash') not in observed_times,'tokens_estimated':known is None})
        with self.locked() as (handle,events):
            seen={e.get('history_id') for e in events if e['type']=='import'}
            for event in pending:
                if event['history_id'] not in seen:
                    self._write(handle,event);seen.add(event['history_id'])

    def reserve(self,model,estimate,purpose='discovery'):
        limits=self.limits(model)
        tokens=estimate['reserved_tokens']
        if tokens>limits['tokens_per_minute']:
            raise BudgetPause('Single request exceeds configured TPM estimate; compact/chunk it first')
        waited=False
        while True:
            now=self.clock()
            with self.locked() as (handle,events):
                daily,minute=self.usage(events,self.org,model,now)
                ceiling=limits['tokens_per_day']-self.settings.get('daily_safety_reserve_tokens',10000)
                if purpose!='evaluation':
                    ceiling-=self.settings.get('evaluation_reserve_by_model',{}).get(model,0)
                if sum(e['charged_tokens'] for e in daily)+tokens>ceiling:
                    retry=min((e['timestamp']+86400-now for e in daily),default=86400)
                    raise BudgetPause('App-local rolling daily token budget reached; checkpoint, do not switch keys',retry)
                if len(daily)>=limits['requests_per_day']:
                    raise BudgetPause('App-local daily request budget reached',86400)
                # Unsettled requests still hold their full reservation. Once the
                # response arrives, use measured uncached input + output.
                minute_total=sum(e['charged_tokens'] for e in minute)
                if minute_total+tokens<=limits['tokens_per_minute'] and len(minute)<limits['requests_per_minute']:
                    identity=uuid.uuid4().hex
                    self._write(handle,{'type':'reserve','reservation_id':identity,'organization':self.org,
                        'provider':'groq','model':model,'timestamp':now,'utc_day':datetime.fromtimestamp(now,timezone.utc).date().isoformat(),
                        **estimate})
                    return identity
                # Wake at the first expiry that actually makes room, rather
                # than waiting for every reservation in the minute to expire.
                remaining=minute_total;count=len(minute);delay=60.05
                for event in sorted(minute,key=lambda e:e['timestamp']):
                    remaining-=event['charged_tokens'];count-=1
                    if remaining+tokens<=limits['tokens_per_minute'] and count<limits['requests_per_minute']:
                        delay=max(.05,event['timestamp']+60-now+.05);break
            if waited or delay>self.settings.get('max_pacing_wait_seconds',55):
                raise BudgetPause('Token-aware pacing pause; checkpoint before another request',delay)
            self.sleep(delay);waited=True

    def settle(self,identity,actual_tokens=None,cached_input_tokens=0):
        with self.locked() as (handle,events):
            reservation=next(e for e in events if e.get('reservation_id')==identity and e['type']=='reserve')
            if any(e['type']=='settle' and e['reservation_id']==identity for e in events):return
            cached=max(0,min(cached_input_tokens or 0,actual_tokens or 0))
            self._write(handle,{'type':'settle','reservation_id':identity,'timestamp':self.clock(),
                'charged_tokens':actual_tokens-cached if actual_tokens is not None else reservation['reserved_tokens'],
                'actual_total_tokens':actual_tokens,'cached_input_tokens':cached,
                'tokens_estimated':actual_tokens is None})

    def reconcile_cached_usage(self,usage_path):
        """Credit only cache tokens proved by a matching logged raw response.

        Original ledger events remain immutable. Missing/unknown usage is never
        forgiven. A repeat invocation cannot grant the same credit twice.
        """
        raw_path=usage_path.parent/'raw_responses.jsonl'
        if not raw_path.exists() or not usage_path.exists():return 0
        raw={}
        for line in raw_path.read_text().splitlines():
            row=json.loads(line)
            if not row.get('cache_hit'):
                raw[(row.get('request_hash'),row.get('attempt',1))]=row
        credits=0
        with self.locked() as (handle,events):
            settled={e['reservation_id']:e for e in events if e['type']=='settle'}
            credited={e['reservation_id'] for e in events if e['type']=='cache_credit'}
            reservations={e['reservation_id']:e for e in events if e['type']=='reserve'}
            for line in usage_path.read_text().splitlines():
                row=json.loads(line);rid=row.get('quota_reservation_id')
                if not rid or rid in credited or rid not in settled or rid not in reservations:continue
                entry=raw.get((row.get('request_hash'),row.get('attempt',1)),{})
                usage=entry.get('response',{}).get('usage') or {}
                cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')
                total=usage.get('total_tokens');old=settled[rid]
                if (not isinstance(cached,int) or cached<=0 or total!=row.get('total_tokens')
                    or not isinstance(total,int) or cached>usage.get('prompt_tokens',0)
                    or old.get('tokens_estimated') or reservations[rid]['model']!=row.get('model')):continue
                charge=total-cached
                if charge>=old['charged_tokens']:continue
                self._write(handle,{'type':'cache_credit','reservation_id':rid,'timestamp':self.clock(),
                    'charged_tokens':charge,'tokens_estimated':False,'actual_total_tokens':total,
                    'cached_input_tokens':cached,'proof_request_hash':row['request_hash']})
                credits+=old['charged_tokens']-charge;credited.add(rid)
        return credits

    def snapshot(self):
        with self.locked() as (_,events):
            rows={}
            for model in self.settings.get('models',{}):
                daily,minute=self.usage(events,self.org,model,self.clock())
                rows[model]={'rolling_24h_accounted_tokens':sum(e['charged_tokens'] for e in daily),
                    'rolling_24h_requests':len(daily),'minute_reserved_tokens':sum(e['charged_tokens'] for e in minute),
                    'estimated_rows':sum(e.get('tokens_estimated',False) for e in daily)}
            return {'models':rows,'organization_namespace':self.org,
                    'limit_source':self.settings.get('limit_source','unconfirmed planning limits'),
                    'coverage':'this app only; other organization use is not visible',
                    'accounting_window':'rolling 24h conservatively, not an asserted provider reset time'}
