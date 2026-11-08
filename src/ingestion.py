"""Bounded local file import. ZIP contents are read, never extracted."""
from collections import Counter
from pathlib import Path, PurePosixPath
import hashlib
import json
import stat
import zipfile

from .models import Episode
from .parser import parse_text
from .validation import validate_episode
from .input_adapters import parse_source,SUPPORTED_FORMATS,SourceFormatError

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 2000
PARSER_VERSION = "0.2.0"


def ingest(path: Path):
    files = []
    failures = []
    total = 0

    def add(name, raw):
        nonlocal total
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Input size limit exceeded")
        files.append((name, raw))

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                raise ValueError("Too many archive entries")
            if sum(x.file_size for x in entries) > MAX_TOTAL_BYTES:
                raise ValueError("Archive uncompressed size limit exceeded")
            for item in entries:
                if item.is_dir():
                    continue
                name = item.filename
                if (PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                        or "\\" in name or stat.S_ISLNK(item.external_attr >> 16)):
                    failures.append({"source_file": name, "code": "unsafe_archive_entry"})
                elif item.file_size > MAX_FILE_BYTES:
                    failures.append({"source_file": name, "code": "file_size_limit"})
                else:
                    try:
                        add(name, archive.read(item))
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                        failures.append({"source_file": name, "code": "unreadable_archive_entry"})
    elif path.is_dir():
        entries = sorted(p for p in path.rglob("*") if p.is_file())
        if len(entries) > MAX_FILES:
            raise ValueError("Too many input files")
        for item in entries:
            name = item.relative_to(path).as_posix()
            if item.is_symlink() or not item.resolve().is_relative_to(path.resolve()):
                failures.append({"source_file": name, "code": "external_symlink"})
            elif item.stat().st_size > MAX_FILE_BYTES:
                failures.append({"source_file": name, "code": "file_size_limit"})
            else:
                add(name, item.read_bytes())
    else:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File size limit exceeded")
        add(path.name, path.read_bytes())

    episodes = []
    outcomes = list(failures)
    hashes = []
    identities = set()
    episode_numbers = set()
    for file_number,(name, raw) in enumerate(files,1):
        hashes.append((name, hashlib.sha256(raw).hexdigest()))
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_FORMATS:
            outcomes.append({"source_file": name, "status": "failed", "code": "unsupported_format"})
            continue
        try:
            parsed = parse_source(raw,name,default_number=file_number)
            if not parsed:raise ValueError('No episodes in input')
            for episode in parsed:
                if episode.episode_id in identities or episode.episode_number in episode_numbers:
                    raise ValueError("Duplicate episode ID")
                validate_episode(episode)
            new_ids = [e.episode_id for e in parsed]
            if len(set(new_ids)) != len(new_ids):
                raise ValueError("Duplicate episode IDs in JSON")
            if len({e.episode_number for e in parsed}) != len(parsed):
                raise ValueError("Duplicate episode numbers in JSON")
            for episode in parsed:
                identities.add(episode.episode_id)
                episode_numbers.add(episode.episode_number)
                episodes.append(episode)
                issues = [i.model_dump() for i in episode.issues]
                errors = sum(i["severity"] == "error" for i in issues)
                outcomes.append({"source_file": name, "episode_id": episode.episode_id,
                                 "status": "partial" if errors else "parsed_with_warnings" if issues else "parsed",
                                 "events": len(episode.events), "scenes": len(episode.scenes),
                                 "duration_ms": episode.duration_ms,
                                 "source_ready": episode.source_ready, "issues": issues})
        except SourceFormatError as exc:
            outcomes.append({'source_file':name,'status':'failed','code':exc.code,'message':str(exc)})
        except (ValueError, UnicodeError, TypeError, AttributeError):
            outcomes.append({"source_file": name, "status": "failed", "code": "invalid_source"})
    for outcome in outcomes:
        outcome.setdefault("status", "failed")
    episodes.sort(key=lambda e: e.episode_number)
    statuses = Counter(row["status"] for row in outcomes)
    if path.is_file():
        with path.open("rb") as handle:
            input_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    else:
        input_hash = hashlib.sha256(json.dumps({"files": sorted(hashes), "failures": failures}, sort_keys=True).encode()).hexdigest()
    report = {
        "parser_version": PARSER_VERSION,
        "input_hash": input_hash,
        "input_hash_kind": "file_bytes" if path.is_file() else "directory_manifest",
        "order_policy": {"playback": "start_ms_then_source_order", "provenance": "source_order"},
        "files_discovered": len(files) + len(failures),
        "episodes_parsed": len(episodes),
        "source_ready_episodes": sum(e.source_ready for e in episodes),
        "requires_source_review": [e.episode_id for e in episodes if not e.source_ready],
        "events_parsed": sum(len(e.events) for e in episodes),
        "event_types": dict(Counter(e.event_type for ep in episodes for e in ep.events)),
        "issue_counts": dict(Counter(i.code for e in episodes for i in e.issues)),
        "status_counts": dict(statuses),
        "complete_ingestion": not statuses["failed"] and not statuses["partial"] and bool(episodes),
        "files": outcomes,
    }
    return episodes, report


def save_ingestion(episodes, report, destination):
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "episodes.jsonl").write_text(
        "".join(e.model_dump_json() + "\n" for e in episodes), encoding="utf-8")
    (destination / "ingestion_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (destination / "episode.schema.json").write_text(json.dumps(Episode.model_json_schema(), indent=2) + "\n", encoding="utf-8")
