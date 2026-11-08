"""Deterministic coverage allocation; priorities are ordering, never quality proof."""
import json


def source_reserved_numbers(root,episodes,respect_optional=True):
    """Apply private reservations to CLI runs as well as uploaded workspaces."""
    hashes=set()
    for filename in ('coverage_reservations.json','human_references.json'):
        if filename=='coverage_reservations.json' and not respect_optional:continue
        path=root/'evaluation/private'/filename
        if path.exists():hashes.update(r['source_sha256'] for r in json.loads(path.read_text())['entries'])
    return {e.episode_number for e in episodes if e.source_sha256 in hashes}


def excluded_numbers(config):
    return set(config['evaluation'].get('held_out_episode_numbers',[])) | set(config['batch'].get('coverage_excluded_episode_numbers',[]))


def candidate_key(candidate):
    margin=min(candidate.duration_ms-20000,60000-candidate.duration_ms)
    partial=sum(e.content_status!='full' for e in candidate.selected_content)
    return (-candidate.discovery_priority,partial,len(candidate.warnings),-margin,
            candidate.start_ms,candidate.end_ms,candidate.candidate_id)


def allocate(items,cap,excluded=(),completed=()):
    """One best candidate per episode before any second candidate.

    Bands span the entire eligible episode list, even with a small cap. Within
    an episode: priority, complete records, warnings, duration margin, stable IDs.
    Caller must apply this same allocation again after a budget reduction.
    """
    if cap<=0:return []
    groups={};seen=set();excluded=set(excluded);completed=set(completed);represented=set()
    for ep,c in items:
        if not ep.source_ready or ep.episode_number in excluded or c.candidate_id in seen:continue
        if c.candidate_id in completed:
            represented.add(ep.episode_number);continue
        groups.setdefault(ep.episode_number,[]).append((ep,c));seen.add(c.candidate_id)
    if not groups:return []
    numbers=sorted(groups)
    for group in groups.values():group.sort(key=lambda item:candidate_key(item[1]))
    count=min(5,len(numbers),cap);bands=[[] for _ in range(count)]
    for position,number in enumerate(numbers):bands[min(count-1,position*count//len(numbers))].append(number)
    # Stable best candidate ordering within each chronological band.
    for band in bands:band.sort(key=lambda n:(candidate_key(groups[n][0][1]),n))
    episode_order=[]
    while any(bands):
        for band in bands:
            if band:episode_order.append(band.pop(0))
    selected=[];depth=0
    while len(selected)<cap:
        wave=[groups[n][depth] for n in episode_order if len(groups[n])>depth and n not in represented]
        # Already attempted episodes get an expansion only after every new one.
        if not wave:
            wave=[groups[n][depth] for n in episode_order if len(groups[n])>depth]
        if not wave:break
        chosen=wave[:cap-len(selected)];selected.extend(chosen)
        for ep,c in chosen:groups[ep.episode_number].remove((ep,c))
        represented.update(ep.episode_number for ep,_ in chosen)
    return selected
