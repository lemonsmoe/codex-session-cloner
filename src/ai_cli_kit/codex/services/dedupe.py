"""Session deduplication services."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from ..models import DedupeResult
from ..paths import CodexPaths
from ..services.provider import detect_provider
from ..stores.index import remove_session_index_entries
from ..stores.session_files import iter_session_files, parse_codex_timestamp, parse_jsonl_records, read_session_payload
from ..support import atomic_write, backup_file, backup_operation_slug, long_path, prune_old_backups


@dataclass(frozen=True)
class _SessionRecord:
    session_id: str
    model_provider: str
    cloned_from: str
    path: Path
    mtime: float
    last_timestamp: str
    last_activity: float


_SCRUB_META_KEYS = {
    "id",
    "session_id",
    "thread_id",
    "model_provider",
    "cloned_from",
    "original_provider",
    "original_provider_fingerprint",
    "target_provider_fingerprint",
    "provider_fingerprint",
    "provider_context_label",
    "provider_base_url_host",
    "provider_wire_api",
    "ccswitch_provider_id",
    "ccswitch_api_format",
    "timestamp",
    "clone_timestamp",
}


def _timestamp_score(value: object, fallback: float) -> float:
    parsed = parse_codex_timestamp(value if isinstance(value, str) else None)
    if parsed is None:
        return float(fallback)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _session_catalog(paths: CodexPaths, *, active_only: bool) -> tuple[int, dict[str, _SessionRecord]]:
    files_checked = 0
    catalog: dict[str, _SessionRecord] = {}

    for session_file in iter_session_files(paths, active_only=active_only):
        files_checked += 1
        try:
            mtime = session_file.stat().st_mtime
            records = parse_jsonl_records(session_file)
        except Exception:
            continue

        payload: dict[str, Any] | None = None
        last_timestamp = ""
        for _, obj in records:
            if not isinstance(obj, dict):
                continue
            timestamp = obj.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                last_timestamp = timestamp
            if obj.get("type") != "session_meta":
                continue
            candidate = obj.get("payload")
            if payload is None and isinstance(candidate, dict):
                payload = dict(candidate)

        if payload is None:
            continue
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue

        model_provider = payload.get("model_provider")
        cloned_from = payload.get("cloned_from")
        record = _SessionRecord(
            session_id=session_id,
            model_provider=model_provider if isinstance(model_provider, str) else "",
            cloned_from=cloned_from if isinstance(cloned_from, str) else "",
            path=session_file,
            mtime=mtime,
            last_timestamp=last_timestamp,
            last_activity=_timestamp_score(last_timestamp, mtime),
        )

        existing = catalog.get(session_id)
        if existing is None or (record.mtime, str(record.path)) >= (existing.mtime, str(existing.path)):
            catalog[session_id] = record

    return files_checked, catalog


def _root_of(session_id: str, catalog: dict[str, _SessionRecord], cache: dict[str, str] | None = None) -> str:
    cache = {} if cache is None else cache
    if session_id in cache:
        return cache[session_id]

    trail: list[str] = []
    seen: set[str] = set()
    current = session_id

    while True:
        if current in cache:
            root_id = cache[current]
            break
        if current in seen:
            root_id = current
            break
        seen.add(current)
        trail.append(current)

        record = catalog.get(current)
        parent_id = record.cloned_from if record is not None else ""
        if not parent_id or parent_id not in catalog:
            root_id = current
            break
        current = parent_id

    for trail_id in trail:
        cache[trail_id] = root_id
    return root_id


def _depth_of(session_id: str, catalog: dict[str, _SessionRecord], cache: dict[str, int] | None = None) -> int:
    cache = {} if cache is None else cache
    if session_id in cache:
        return cache[session_id]

    trail: list[str] = []
    seen: set[str] = set()
    current = session_id
    depth = 0

    while True:
        if current in cache:
            depth += cache[current]
            break
        if current in seen:
            break
        seen.add(current)
        trail.append(current)

        record = catalog.get(current)
        parent_id = record.cloned_from if record is not None else ""
        if not parent_id or parent_id not in catalog:
            break
        current = parent_id
        depth += 1

    for index, trail_id in enumerate(trail):
        cache.setdefault(trail_id, max(depth - index, 0))
    return cache.get(session_id, depth)


def _representative_key(
    record: _SessionRecord,
    catalog: dict[str, _SessionRecord],
    depth_cache: dict[str, int] | None = None,
) -> tuple[float, int, float, str]:
    return (
        record.last_activity,
        _depth_of(record.session_id, catalog, depth_cache),
        record.mtime,
        str(record.path),
    )


def _scrub_session_meta_payload(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _scrub_session_meta_payload(item)
            for key, item in sorted(value.items())
            if key not in _SCRUB_META_KEYS
        }
    if isinstance(value, list):
        return [_scrub_session_meta_payload(item) for item in value]
    return value


def _canonical_record(obj: dict) -> str:
    candidate = dict(obj)
    if candidate.get("type") in {"session_meta", "session_meta_embedded"}:
        candidate.pop("timestamp", None)
        payload = candidate.get("payload")
        if isinstance(payload, dict):
            candidate["payload"] = _scrub_session_meta_payload(payload)
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_session_records(path: Path) -> list[str]:
    records: list[str] = []
    for _, obj in parse_jsonl_records(path):
        if isinstance(obj, dict):
            records.append(_canonical_record(obj))
    return records


def _content_duplicate_reason(candidate: _SessionRecord, representative: _SessionRecord) -> str:
    try:
        candidate_records = _canonical_session_records(candidate.path)
        representative_records = _canonical_session_records(representative.path)
    except Exception:
        return ""

    if not candidate_records or not representative_records:
        return ""
    if candidate_records == representative_records:
        return "same_content_keep_latest_representative"
    if len(candidate_records) <= len(representative_records) and representative_records[: len(candidate_records)] == candidate_records:
        return "prefix_content_keep_latest_representative"
    return ""


def _lineage_duplicate_pairs(catalog: dict[str, _SessionRecord]) -> list[tuple[Path, Path, str]]:
    root_cache: dict[str, str] = {}
    depth_cache: dict[str, int] = {}
    duplicate_groups: dict[str, list[_SessionRecord]] = {}

    for record in catalog.values():
        root_id = _root_of(record.session_id, catalog, root_cache)
        duplicate_groups.setdefault(root_id, []).append(record)

    duplicate_pairs: list[tuple[Path, Path, str]] = []
    for records in duplicate_groups.values():
        if len(records) <= 1:
            continue
        representative = max(records, key=lambda record: _representative_key(record, catalog, depth_cache))
        for duplicate in sorted(
            (record for record in records if record.session_id != representative.session_id),
            key=lambda record: _representative_key(record, catalog, depth_cache),
            reverse=True,
        ):
            reason = _content_duplicate_reason(duplicate, representative)
            if reason:
                duplicate_pairs.append((duplicate.path, representative.path, reason))

    duplicate_pairs.sort(key=lambda pair: (str(pair[1]), str(pair[0])))
    return duplicate_pairs


def _prune_state_file(state_file: Path, deleted_session_ids: set[str]) -> None:
    if not deleted_session_ids or not state_file.exists():
        return

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return

    def prune_mapping(mapping: object) -> object:
        if not isinstance(mapping, dict):
            return mapping
        return {key: value for key, value in mapping.items() if key not in deleted_session_ids}

    def prune_string_list(values: object) -> object:
        if not isinstance(values, list):
            return values
        return [value for value in values if not (isinstance(value, str) and value in deleted_session_ids)]

    data["thread-workspace-root-hints"] = prune_mapping(data.get("thread-workspace-root-hints"))
    data["projectless-thread-ids"] = prune_string_list(data.get("projectless-thread-ids"))
    atom_state = data.get("electron-persisted-atom-state", {})
    if isinstance(atom_state, dict):
        atom_state["thread-workspace-root-hints"] = prune_mapping(atom_state.get("thread-workspace-root-hints"))
        atom_state["projectless-thread-ids"] = prune_string_list(atom_state.get("projectless-thread-ids"))
        atom_state["heartbeat-thread-permissions-by-id"] = prune_mapping(
            atom_state.get("heartbeat-thread-permissions-by-id")
        )
        thread_titles = atom_state.get("thread-titles")
        if isinstance(thread_titles, dict):
            thread_titles["titles"] = prune_mapping(thread_titles.get("titles"))

    try:
        with atomic_write(state_file) as fh:
            fh.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    except OSError:
        return


def _delete_threads_rows(state_db: Path | None, deleted_session_ids: set[str]) -> None:
    if not deleted_session_ids or state_db is None or not state_db.exists():
        return

    try:
        with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn:
            cur = conn.cursor()
            row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
            if row:
                cur.executemany("delete from threads where id = ?", [(session_id,) for session_id in deleted_session_ids])

            edge_row = cur.execute("select name from sqlite_master where type='table' and name='thread_spawn_edges'").fetchone()
            if edge_row:
                columns = [info[1] for info in cur.execute("pragma table_info(thread_spawn_edges)").fetchall()]
                if "parent_thread_id" in columns:
                    cur.executemany(
                        "delete from thread_spawn_edges where parent_thread_id = ?",
                        [(session_id,) for session_id in deleted_session_ids],
                    )
                if "child_thread_id" in columns:
                    cur.executemany(
                        "delete from thread_spawn_edges where child_thread_id = ?",
                        [(session_id,) for session_id in deleted_session_ids],
                    )

            conn.commit()
    except sqlite3.Error:
        return


def dedupe_clones(
    paths: CodexPaths,
    *,
    target_provider: str = "",
    dry_run: bool = False,
    active_only: bool = False,
) -> DedupeResult:
    provider = detect_provider(paths, explicit=target_provider)
    files_checked, catalog = _session_catalog(paths, active_only=active_only)
    duplicate_pairs = _lineage_duplicate_pairs(catalog)

    if dry_run or not duplicate_pairs:
        return DedupeResult(
            provider=provider,
            dry_run=dry_run,
            files_checked=files_checked,
            duplicate_pairs=duplicate_pairs,
        )

    backup_parent = paths.code_dir / "repair_backups"
    prune_old_backups(backup_parent, keep_last=20)
    backup_root = backup_parent / backup_operation_slug("dedupe")
    backed_up: set[str] = set()
    deleted_session_ids: list[str] = []
    deleted_files: list[Path] = []
    errors: list[tuple[Path, str]] = []

    for delete_path, _, _ in duplicate_pairs:
        try:
            payload = read_session_payload(delete_path)
            session_id = payload.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("session_meta payload.id is missing")
            backup_file(paths.code_dir, backup_root, backed_up, delete_path, enabled=True)
            delete_path.unlink()
            deleted_session_ids.append(session_id)
            deleted_files.append(delete_path)
        except Exception as exc:
            errors.append((delete_path, str(exc)))

    deleted_session_id_set = set(deleted_session_ids)
    if deleted_session_id_set:
        backup_file(paths.code_dir, backup_root, backed_up, paths.index_file, enabled=paths.index_file.exists())
        state_db = paths.latest_state_db()
        if state_db is not None:
            backup_file(paths.code_dir, backup_root, backed_up, state_db, enabled=state_db.exists())
        backup_file(paths.code_dir, backup_root, backed_up, paths.state_file, enabled=paths.state_file.exists())
        remove_session_index_entries(paths.index_file, deleted_session_id_set)
        _delete_threads_rows(state_db, deleted_session_id_set)
        _prune_state_file(paths.state_file, deleted_session_id_set)

    return DedupeResult(
        provider=provider,
        dry_run=False,
        files_checked=files_checked,
        duplicate_pairs=duplicate_pairs,
        deleted_session_ids=deleted_session_ids,
        deleted_files=deleted_files,
        backup_root=backup_root,
        errors=errors,
    )
