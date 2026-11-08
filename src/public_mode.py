"""Anonymous session isolation for the optional public prototype.

Keys live only in process memory. No owner key or private saved runs are exposed.
"""
from contextvars import ContextVar
import hashlib
import os
import secrets
import time

CURRENT=ContextVar('promo_public_session',default=None)
SESSIONS={}
OWNERS={}
TTL=3600


def enabled():return os.getenv('PROMO_PUBLIC')=='1'


def session(cookie):
    now=time.time()
    # An active job retains its credential until it finishes; requests expire
    # after one hour of browser inactivity and restart with a new session.
    if cookie not in SESSIONS or now-SESSIONS[cookie]['seen']>TTL:
        SESSIONS.pop(cookie,None)
        cookie=secrets.token_urlsafe(32);SESSIONS[cookie]={'seen':now,'key':None}
    SESSIONS[cookie]['seen']=now
    return cookie


def register(pid):
    if enabled():
        sid=CURRENT.get()
        if not sid or sid not in SESSIONS:raise ValueError('Start a public browser session first')
        OWNERS[pid]=sid


def visible(pid):return not enabled() or OWNERS.get(pid)==CURRENT.get() and CURRENT.get() is not None


def namespace():
    sid=CURRENT.get()
    if shared_key() and not SESSIONS.get(sid,{}).get('key'):return 'public_shared_organization'
    return 'public_'+hashlib.sha256(sid.encode()).hexdigest()[:20] if sid else 'public_unassigned'


def shared_key():return os.getenv('GROQ_API_KEY') if os.getenv('PROMO_SHARED_KEY')=='1' else None


def current_key():return SESSIONS.get(CURRENT.get(),{}).get('key') or shared_key()


def project_key(pid):return SESSIONS.get(OWNERS.get(pid),{}).get('key') or shared_key()


def destination_key(destination):
    for path in [destination,*destination.parents]:
        if path.name in OWNERS:return project_key(path.name)
    return None


def destination_namespace(destination):
    for path in [destination,*destination.parents]:
        sid=OWNERS.get(path.name)
        if sid:return 'public_'+hashlib.sha256(sid.encode()).hexdigest()[:20]
    raise ValueError('Public run has no session owner')
