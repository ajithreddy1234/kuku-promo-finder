"""Offline planning only. Never authenticates to a hosted inference provider."""
from .analysis_planning import plan_analysis_parts
from .budget import BudgetLedger,BudgetPause


def plan_run(root,episodes,config,selected):
    by_number={e.episode_number:e for e in episodes}
    if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(by_number):raise ValueError('Choose valid episodes for the capacity check.')
    if any(not by_number[n].source_ready for n in selected):raise ValueError('Selected episodes need source timing review.')
    role=config['models']['analysis'];parts=[];errors=[]
    for n in selected:
        try:parts.extend(plan_analysis_parts(by_number[n],config))
        except BudgetPause as exc:errors.append({'episode_number':n,'reason':str(exc)})
    estimate=sum(p['estimate']['reserved_tokens'] for p in parts)
    ledger=BudgetLedger(root,config).snapshot();headroom=None;fits=None
    if role['provider']=='groq':
        limit=config['quota']['models'][role['model']]['tokens_per_day']
        used=ledger['models'][role['model']]['rolling_24h_accounted_tokens']
        headroom=max(0,limit-config['quota'].get('daily_safety_reserve_tokens',10000)-used)
        fits=estimate<=headroom and not errors
    return {'selected_episodes':len(selected),'analysis_requests':len(parts),'analysis_reserved_tokens_estimate':estimate,
        'analysis_model':role['model'],'analysis_provider':role['provider'],'analysis_headroom':headroom,
        'analysis_estimate_fits':fits,'source_errors':errors,'api_calls':0,
        'blind_review_cap':config['batch'].get('blind_global_limit',len(selected)*config['candidates']['blind_reviewed_per_episode']),
        'context_review_cap':config['batch']['context_limit'],
        'note':'Conservative request reservations, not measured token usage. Reviews need additional capacity. Cached-token savings are not assumed; other organization usage is invisible.'}
