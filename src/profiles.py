"""Groq-only processing. Historical runs remain readable, never rewritten."""
from copy import deepcopy


def apply_profile(config,profile='groq'):
    if profile!='groq':raise ValueError('Only Groq processing is supported.')
    config=deepcopy(config)
    if any(config['models'][r]['provider']!='groq' for r in ('analysis','blind','context')):
        raise ValueError('This historical model configuration cannot run. Start a new Groq run.')
    config['batch']['processing_profile']='groq'
    return config
