"""Explicit run profiles. Never change a model halfway through a pinned run."""
from copy import deepcopy


def local_models():
    try:
        import httpx
        response=httpx.get('http://127.0.0.1:11434/api/tags',timeout=1)
        response.raise_for_status()
        return [m['name'] for m in response.json().get('models',[]) if 'cloud' not in m['name'].lower()]
    except Exception:
        return []


def apply_profile(config,profile='groq',local_model='llama3.1:8b'):
    config=deepcopy(config)
    if profile not in ('groq','hybrid','local'):raise ValueError('Choose Groq, hybrid or local processing.')
    config['batch']['processing_profile']=profile
    if profile=='local':config.setdefault('review',{})['blind_fallback_role']=None
    if profile in ('local','hybrid'):
        if local_model not in local_models():raise ValueError('Choose an installed local Ollama model. Cloud model tags are disabled.')
        for role in (('analysis',) if profile=='hybrid' else ('analysis','blind','context')):
            config['models'][role].update(provider='ollama',model=local_model,context_tokens=8192)
            config['models'][role].pop('structured_mode',None)
            config['models'][role]['max_output_tokens']=1536 if role=='analysis' else 1024
            config['models'][role]['timeout_seconds']=180
        config['batch']['max_workers']=1
        if profile=='hybrid':
            config['models']['context'].update(provider='groq',model='qwen/qwen3.8-27b',
                structured_mode='json_object',reasoning_effort='none',prompt_version='context-v8-compact-source')
    return config
