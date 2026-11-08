"""Tolerant timed screenplay reader, preserving overlapping source tracks."""
import hashlib
import re
from pathlib import PurePosixPath
from decimal import Decimal, InvalidOperation

from .models import Episode, Event, Issue, Scene
from .validation import validate_episode

TIMING = re.compile(r"^Start\s*:\s*\(?([^|)]+)\)?\s*\|\s*End\s*:\s*\(?([^|)]+)\)?\s*$", re.I)
SCENE = re.compile(r"^SCENE\s+(\d+)\s*:?\s*$", re.I)
EPISODE = re.compile(r"^EPISODE\s+(?:NUMBER|NO\.?)\s*:\s*(?:Episode\s*)?(\d+)\s*$", re.I | re.M)
SCRIPT_END = re.compile(r"^-{3,}\s*SCRIPT END\s*-{3,}$", re.I)


def parse_time(value: str) -> int:
    value = value.strip().strip("()").replace(",", ".")
    if not re.fullmatch(r"\d+:\d{2}(?::\d{2})?(?:\.\d{1,3})?", value):
        raise ValueError("Expected MM:SS[.mmm] or HH:MM:SS[.mmm]")
    parts = value.split(":")
    try:
        seconds = Decimal(parts[-1])
        if not 0 <= seconds < 60 or (len(parts) == 3 and int(parts[1]) >= 60):
            raise ValueError("Invalid clock component")
        total = seconds + int(parts[-2]) * 60
        if len(parts) == 3:
            total += int(parts[0]) * 3600
        return int(total * 1000)
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid timestamp") from exc


def clean(line: str) -> str:
    return line.strip().strip("*").strip()


def parse_text(raw: bytes, source_file: str, default_number: int | None = None) -> Episode:
    text = raw.decode("utf-8-sig")
    lines = text.splitlines()
    metadata_match = EPISODE.search("\n".join(clean(line) for line in lines))
    filename_match = re.search(r"episode[-_ ]*(\d+)", PurePosixPath(source_file).name, re.I)
    match = metadata_match or filename_match
    if match is None and default_number is None:
        raise ValueError("Episode number missing in header and filename")
    number = int(match[1]) if match else default_number
    episode = Episode(episode_id=f"E{number:03d}", episode_number=number,
                      source_file=source_file, source_sha256=hashlib.sha256(raw).hexdigest())

    def issue(code, message, line=None, severity="warning"):
        episode.issues.append(Issue(code=code, severity=severity, message=message,
                                    source_file=source_file, source_line=line))

    if metadata_match and filename_match and int(metadata_match[1]) != int(filename_match[1]):
        issue("episode_identity_conflict", "Header and filename disagree", severity="error")
    scene = None
    used_timing_lines = set()
    i = 0
    while i < len(lines):
        line = clean(lines[i])
        if SCRIPT_END.fullmatch(line):
            break
        sm = SCENE.fullmatch(line)
        if sm:
            scene = Scene(scene_id=f"{episode.episode_id}_S{int(sm[1]):03d}")
            if any(s.scene_id == scene.scene_id for s in episode.scenes):
                issue("duplicate_scene", "Repeated scene identifier", i + 1, "error")
            episode.scenes.append(scene)
            i += 1
            continue
        field = re.match(r"^(SCENE HEADING|SCENE START|SCENE END|EPISODE LOGLINE|CONTINUITY NOTES)\s*:\s*(.*)$", line, re.I)
        if field:
            key, value = field[1].upper(), field[2]
            if key in ("EPISODE LOGLINE", "CONTINUITY NOTES"):
                episode.metadata[key.lower().replace(" ", "_")] = value
            elif scene is not None:
                if key == "SCENE HEADING":
                    scene.heading = value
                else:
                    try:
                        setattr(scene, "start_ms" if key == "SCENE START" else "end_ms", parse_time(value))
                    except ValueError:
                        issue("invalid_scene_time", "Cannot parse scene timestamp", i + 1, "error")
            else:
                issue("orphan_scene_metadata", "Scene metadata without a scene identifier", i+1, "error")
            i += 1
            continue
        # Support bold or plain labels, but only recognize event blocks near Start:.
        if line.endswith(":") and not line.upper().startswith(("EPISODE", "ACTIONS AND DIALOGUE")):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and re.match(r"^Start\s*:", clean(lines[j]), re.I):
                used_timing_lines.add(j)
                tm = TIMING.fullmatch(clean(lines[j]))
                k = j + 1
                while k < len(lines):
                    nxt = clean(lines[k])
                    if SCENE.fullmatch(nxt) or re.match(r"^SCENE\s+(HEADING|START|END)\s*:", nxt, re.I) or SCRIPT_END.fullmatch(nxt):
                        break
                    if nxt.endswith(":"):
                        if lines[k].strip().startswith("**"):
                            break
                        after = k + 1
                        while after < len(lines) and not lines[after].strip():
                            after += 1
                        if after < len(lines) and re.match(r"^Start\s*:", clean(lines[after]), re.I):
                            break
                    k += 1
                label = line[:-1].strip()
                upper = label.upper()
                event_type = {"CAMERA": "camera", "ACTION": "action", "SOUND FX": "sound",
                              "SOUND": "sound", "SFX": "sound", "MUSIC": "sound"}.get(upper, "dialogue")
                content = "\n".join(lines[j + 1:k]).strip()
                try:
                    if tm is None:
                        raise ValueError("Missing or malformed interval")
                    event = Event(event_id=f"{episode.episode_id}_V{len(episode.events)+1:04d}",
                                  episode_id=episode.episode_id, scene_id=scene.scene_id if scene else None,
                                  event_type=event_type, start_ms=parse_time(tm[1]), end_ms=parse_time(tm[2]),
                                  speaker_raw=label if event_type == "dialogue" else None,
                                  text=content, source_file=source_file, source_line_start=i+1,
                                  source_line_end=max(i+1,k), source_order=len(episode.events))
                    episode.events.append(event)
                except ValueError:
                    issue("invalid_event", "Missing content, invalid timestamp or nonpositive interval", i+1, "error")
                i = k
                continue
        if (line and "SCRIPT START" not in line.upper() and not EPISODE.fullmatch(line)
                and not re.fullmatch(r"ACTIONS AND DIALOGUE\s*:", line, re.I)):
            issue("unparsed_content", "Source content outside a recognized timed event", i+1, "error")
        i += 1

    for idx, line in enumerate(lines):
        if re.match(r"^Start\s*:", clean(line), re.I) and idx not in used_timing_lines:
            issue("orphan_timing", "Timing row not attached to a recognized event", idx+1, "error")
    return validate_episode(episode)
