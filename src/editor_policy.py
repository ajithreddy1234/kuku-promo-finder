"""Versioned, code-only editor routing. Does not change model responses/prompts."""
from copy import deepcopy

POLICY='editor-v1-fixed-rules'


def apply_editor_policy(row):
    row=deepcopy(row);status=row.get('status','unreviewed')
    reasons=[]
    blind=row.get('blind') or {};context=row.get('context') or {}
    kind=blind.get('cliffhanger_type',row.get('cliffhanger_type'))
    if kind=='none':status='rejected'
    for role,values in [('blind',blind),('context',context)]:
        for field,value in values.items():
            if value=='uncertain':reasons.append(f'{role}_{field}_uncertain')
    if 'partial_camera_record' in row.get('warnings',[]):reasons.append('partial_camera_record')
    if 'scene_audio_bridge_requires_editor' in row.get('warnings',[]):reasons.append('scene_audio_bridge_requires_editor')
    if 'duration_ms' in row and min(row['duration_ms']-20000,60000-row['duration_ms'])<1000:
        reasons.append('duration_margin_under_1s')
    if kind=='question':reasons.append('question_ending_requires_editor')
    if (row.get('judged_by') or {}).get('blind',{}).get('fallback_used'):reasons.append('backup_judge_requires_editor')
    if status in ('eligible','ai_eligible','pending_context') and reasons:status='editor_review'
    if status=='eligible':status='ai_eligible'
    row['status']=status;row['editor_policy_version']=POLICY
    row['editor_review_reasons']=sorted(set(reasons))
    row['editor_tier']=('ai_suggestions' if status=='ai_eligible' else 'needs_judgment' if status=='editor_review'
                        else 'rejected' if status in ('rejected','blind_rejected','invalid') else 'not_reviewed')
    return row
