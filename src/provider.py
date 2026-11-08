"""Bounded JSON inference, immutable raw logs, local caching and real usage."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import threading
import uuid

from dotenv import dotenv_values
from .budget import BudgetLedger, BudgetPause, estimate_request
from . import public_mode


def strict_schema(model):
    schema=model.model_json_schema()
    def visit(node):
        if isinstance(node,dict):
            node.pop('default',None)
            if node.get('type')=='object':
                node['additionalProperties']=False
                node['required']=list(node.get('properties',{}))
            for value in node.values():visit(value)
        elif isinstance(node,list):
            for value in node:visit(value)
    visit(schema)
    return schema


class ProviderFailure(RuntimeError):
    pass


class RequestExhausted(ProviderFailure):
    """A response failed its bounded attempts, including across worker resumes."""


LOG_LOCK=threading.Lock()


class JsonProvider:
    def __init__(self, root: Path, config: dict, destination: Path, transport=None,purpose='discovery'):
        self.root=root
        self.config=config
        self.destination=destination
        destination.mkdir(parents=True,exist_ok=True)
        self.cache=root/'runs/_cache'
        if public_mode.enabled():self.cache=root/'runs/_public_cache'/public_mode.destination_namespace(destination)
        self.cache.mkdir(parents=True,exist_ok=True)
        self.transport=transport
        self.purpose=purpose
        self.budget=None
        if config.get('quota',{}).get('enabled') and transport is None:
            self.budget=BudgetLedger(root,config)
            if not public_mode.enabled():self.budget.import_history()

    def _append(self,name,value):
        with LOG_LOCK, (self.destination/name).open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(value,ensure_ascii=False)+'\n')

    def _network(self, model_config, messages, schema, settings):
        if model_config['provider']!='groq':
            raise ProviderFailure('Processing supports Groq only; no local model runtime is enabled')
        from groq import Groq
        keys=dotenv_values(self.root/'.env')
        key=public_mode.destination_key(self.destination) if public_mode.enabled() else keys.get('GROQ_API_KEY')
        if not key:raise ProviderFailure('GROQ_API_KEY is missing')
        with Groq(api_key=key,timeout=settings['timeout_seconds'],max_retries=0) as client:
            mode=model_config.get('structured_mode','strict')
            if mode=='json_object':
                messages=[{'role':m['role'],'content':m['content']} for m in messages]
                messages[0]['content']+='\nReturn only JSON satisfying this schema: '+json.dumps(schema)
            response_format=({'type':'json_object'} if mode=='json_object' else
                 {'type':'json_schema','json_schema':{'name':'result','strict':True,'schema':schema}})
            response=client.chat.completions.with_raw_response.create(
                model=model_config['model'],messages=messages,temperature=settings['temperature'],
                reasoning_effort=model_config.get('reasoning_effort','low'),max_completion_tokens=settings['max_output_tokens'],
                response_format=response_format)
            parsed=response.parse()
            headers={k:v for k,v in response.headers.items() if k.lower().startswith('x-ratelimit-') or k.lower()=='retry-after'}
            return parsed.model_dump(mode='json'), headers

    def prepare(self,role,stage,system,payload,response_model):
        settings=dict(self.config['inference'])
        model_config=self.config['models'][role]
        if 'max_output_tokens' in model_config:
            settings['max_output_tokens']=model_config['max_output_tokens']
        if 'timeout_seconds' in model_config:
            settings['timeout_seconds']=model_config['timeout_seconds']
        if 'retry_limit' in model_config:
            settings['retry_limit']=model_config['retry_limit']
        schema=strict_schema(response_model)
        messages=[{'role':'system','content':system},
                  {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
        identity={'role':role,'stage':stage,'model':model_config,'settings':settings,
                  'schema':schema,'messages':messages}
        digest=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        return settings,model_config,schema,messages,identity,digest

    def request_cost(self,role,stage,system,payload,response_model):
        settings,model,schema,messages,identity,digest=self.prepare(role,stage,system,payload,response_model)
        if (self.cache/f'{digest}.json').exists():return 0
        return estimate_request(messages,schema,settings['max_output_tokens'])['reserved_tokens']

    def request(self,role,stage,system,payload,response_model,validate=None):
        settings,model_config,schema,messages,identity,digest=self.prepare(role,stage,system,payload,response_model)
        cache=self.cache/f'{digest}.json'
        if cache.exists():
            saved=json.loads(cache.read_text())
            result=response_model.model_validate_json(saved['content'])
            if validate:validate(result)
            self._append('usage.jsonl',{'stage':stage,'role':role,'model':model_config['model'],
                                      'cache_hit':True,'request_hash':digest,'api_calls':0})
            self._append('raw_responses.jsonl',{'stage':stage,'role':role,'model':model_config,
                'request_hash':digest,'cache_hit':True,'cache_origin':saved['origin'],
                'response':saved['response']})
            return result
        # Original request is confidential and stays only in ignored runs/.
        self._append('requests.jsonl',{'request_hash':digest,**identity})
        # A pacing checkpoint must not reset malformed-response retry counts.
        failures=self.destination/'failures.jsonl'
        prior=sum(json.loads(line).get('request_hash')==digest
                  for line in failures.read_text().splitlines() if line.strip()) if failures.exists() else 0
        if prior>=settings['retry_limit']+1:
            raise RequestExhausted(f'{stage}: persisted attempt limit reached; no new call made')
        for attempt in range(prior,settings['retry_limit']+1):
            reservation=None
            if self.budget and model_config['provider']=='groq':
                try:
                    reservation=self.budget.reserve(model_config['model'],estimate_request(messages,schema,settings['max_output_tokens']),self.purpose)
                except BudgetPause as exc:
                    self._append('budget_pauses.jsonl',{'role':role,'stage':stage,'reason':exc.reason,
                        'retry_after_seconds':exc.retry_after,'api_calls':0})
                    raise
            start=time.perf_counter()
            envelope=None
            try:
                envelope,headers=(self.transport or self._network)(model_config,messages,schema,settings)
                elapsed=time.perf_counter()-start
                usage=envelope.get('usage') or {}
                cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)
                cached=cached if isinstance(cached,int) and 0<=cached<=(usage.get('prompt_tokens') or 0) else 0
                if reservation:self.budget.settle(reservation,usage.get('total_tokens'),cached)
                row={'stage':stage,'role':role,'model':model_config['model'],'cache_hit':False,
                     'request_hash':digest,'api_calls':1,'attempt':attempt+1,'seconds':round(elapsed,3),
                     'timestamp':time.time(),'quota_reservation_id':reservation,
                     'prompt_tokens':usage.get('prompt_tokens'),'completion_tokens':usage.get('completion_tokens'),
                     'total_tokens':usage.get('total_tokens'),'cached_input_tokens':cached,
                     'rate_limit_tokens':usage['total_tokens']-cached if usage.get('total_tokens') is not None else None,
                     'provider':model_config['provider'],'rate_limits':headers}
                self._append('usage.jsonl',row)
                timestamp=datetime.now(timezone.utc).isoformat()
                self._append('raw_responses.jsonl',{'stage':stage,'role':role,'model':model_config,
                    'request_hash':digest,'cache_hit':False,'attempt':attempt+1,'at':timestamp,'response':envelope})
                choice=envelope['choices'][0]
                if choice.get('finish_reason')!='stop':
                    raise ValueError('Incomplete provider output')
                content=choice['message']['content']
                if not content:raise ValueError('Empty provider output')
                result=response_model.model_validate_json(content)
                if validate:validate(result)
                temporary=cache.with_suffix(f'.{uuid.uuid4().hex}.tmp')
                temporary.write_text(json.dumps({'content':content,'response':envelope,
                                                 'origin':timestamp},ensure_ascii=False))
                temporary.replace(cache)
                return result
            except Exception as exc:
                status=getattr(exc,'status_code',None)
                response=getattr(exc,'response',None)
                response_headers=getattr(response,'headers',{}) or {}
                safe={'stage':stage,'role':role,'request_hash':digest,'attempt':attempt+1,
                      'error_type':type(exc).__name__,'status_code':status}
                safe['model']=model_config['model']
                body=getattr(exc,'body',None)
                if body is not None:
                    serialized=json.dumps(body,default=str)
                    secrets_to_redact=list(dotenv_values(self.root/'.env').values())
                    if public_mode.enabled():secrets_to_redact.append(public_mode.destination_key(self.destination))
                    for secret in secrets_to_redact:
                        if secret:serialized=serialized.replace(secret,'[REDACTED]')
                    safe['provider_error']=json.loads(serialized)
                if envelope is None:
                    if reservation:self.budget.settle(reservation,None)
                    self._append('usage.jsonl',{'stage':stage,'role':role,'model':model_config['model'],
                         'request_hash':digest,'cache_hit':False,'api_calls':1,'attempt':attempt+1,
                         'timestamp':time.time(),'quota_reservation_id':reservation,
                         'prompt_tokens':None,'completion_tokens':None,'total_tokens':None,'status_code':status})
                self._append('failures.jsonl',safe)
                # Never log raw exception strings: HTTP failures may contain sensitive data.
                if status==429:
                    try:delay=float(response_headers.get('retry-after',0))
                    except ValueError:delay=0
                    if 0 < delay <= 55 and attempt<settings['retry_limit']:
                        time.sleep(delay)
                        continue
                    raise ProviderFailure('Quota/rate limit: checkpoint saved; rerun resumes from cache') from None
                error=(body or {}).get('error',body or {}) if isinstance(body,dict) else {}
                retryable=(status in (500,502,503,504) or error.get('code')=='json_validate_failed'
                           or type(exc).__name__ in ('APITimeoutError','APIConnectionError') or isinstance(exc,ValueError))
                if retryable and attempt<settings['retry_limit']:
                    time.sleep(1)
                    continue
                failure_type=RequestExhausted if retryable else ProviderFailure
                raise failure_type(f'{stage} failed ({type(exc).__name__}, status {status}); see private failure log') from None
