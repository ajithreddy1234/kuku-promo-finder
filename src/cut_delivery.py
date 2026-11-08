"""Usable screenplay cut instructions, without promoting drafts to AI approval."""
from copy import deepcopy
from .boundaries import usable_boundaries,interval_iou
from .timeline import reconstruct,format_time


def deliverable_cuts(rows,episodes,project_id,target=20):
    known={e.episode_id:e for e in episodes};cuts=[]
    for row in rows:
        if row.get('status') not in ('ai_eligible','editor_review'):continue
        ep=known.get(row.get('episode_id'));blind=row.get('blind') or {};context=row.get('context') or {}
        if not ep or not ep.source_ready:continue
        if any(blind.get(k)!=v for k,v in [('clear_to_new_viewer','yes'),('hook_within_first_3s','yes'),('ending_resolved','no')]):continue
        if blind.get('cliffhanger_type') in (None,'none','uncertain'):continue
        checks={'source_supported':'yes','hook_factually_supported':'yes','cliffhanger_factually_supported':'yes',
                'misleading_cut':'no','essential_context_missing':'no','ending_resolved_inside_clip':'no',
                'boundary_issue':'none','description_speakers_match_source':'yes','description_consequences_match_source':'yes'}
        if any(context.get(k)!=v for k,v in checks.items()):continue
        start,end=row['start_ms'],row['end_ms'];points=usable_boundaries(ep)
        if start not in points or end not in points or not 20000<=end-start<=60000:continue
        cut=deepcopy(row);cut['selected_content']=[e.model_dump() for e in reconstruct(ep,start,end)]
        if any(i.code=='tolerated_scene_audio_overlap' and i.message.split(':',1)[0] in {e['event_id'] for e in cut['selected_content']} for i in ep.issues):
            cut['warnings']=sorted(set(cut.get('warnings',[])+['scene_audio_bridge_requires_editor']))
            cut['editor_review_reasons']=sorted(set(cut.get('editor_review_reasons',[])+['scene_audio_bridge_requires_editor']))
            cut['status']='editor_review'
        cut['segments']=[{'episode_id':ep.episode_id,'start_ms':start,'end_ms':end}]
        cut['delivery_kind']='ai_suggestion' if cut['status']=='ai_eligible' else 'draft_editor_check'
        cut['origin_project_id']=project_id;cut['source_sha256']=ep.source_sha256
        cut['delivery_note']=('Passed both AI checks; human approval and footage check remain.' if cut['delivery_kind']=='ai_suggestion'
                              else 'Source-checked draft cut. Read the warnings; this is not an AI-approved or human-approved promo.')
        cuts.append(cut)
    cuts.sort(key=lambda r:(r['delivery_kind']!='ai_suggestion',-r.get('score_raw',r.get('score',0)),r['episode_id'],r['start_ms']))
    unique=[]
    for cut in cuts:
        if any(cut['episode_id']==p['episode_id'] and interval_iou(cut['start_ms'],cut['end_ms'],p['start_ms'],p['end_ms'])>=.8 for p in unique):continue
        cut['cut_order']=len(unique)+1
        cut['order_note']='Provisional order among available source-checked cuts; not a completed whole-show ranking.'
        unique.append(cut)
        if len(unique)>=target:break
    return unique


def cut_text(cuts):
    lines=['PROMO CUT SHEET','Script-based edit instructions. No video file is generated.','']
    if not cuts:lines.append('No source-checked promo cuts are available yet. Complete processing or inspect candidate clips; no cut has been invented.')
    for cut in cuts:
        lines.extend([f"CUT {cut['cut_order']} | {cut['episode_id']} | {format_time(cut['start_ms'])} - {format_time(cut['end_ms'])} | {(cut['end_ms']-cut['start_ms'])/1000:.1f}s",
                      cut['delivery_note'],f"Hook: {cut.get('hook_quote') or cut.get('hook','')}",
                      f"Cliffhanger: {cut.get('cliffhanger_quote') or cut.get('cliffhanger','')}",
                      f"Why: {cut.get('why_it_works','')}",f"Ordering: {cut['order_note']}",
                      'Checks/warnings: '+'; '.join(cut.get('editor_review_reasons',[])+cut.get('warnings',[])),
                      f"AI status: {cut['status']} | Human approved: {bool((cut.get('editor_note') or {}).get('verdict')=='accept')}",
                      f"Source run: {cut['origin_project_id']} | Candidate: {cut['candidate_id']}",'','SELECTED SCREENPLAY:'])
        for event in cut['selected_content']:
            text=event['text'] or '[Partial source record: exact words/images inside this cut require footage inspection.]'
            lines.append(f"{format_time(event['source_start_ms'])} - {format_time(event['source_end_ms'])} | {event['speaker_raw'] or event['event_type']} | {text}")
        lines.extend(['','-'*60,''])
    return '\n'.join(lines)+'\n'
