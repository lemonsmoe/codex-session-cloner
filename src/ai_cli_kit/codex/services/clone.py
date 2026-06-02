"""Clone and cleanup services."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..errors import ToolkitError
from ..models import CleanupResult, CloneFileResult, CloneRunResult
from ..paths import CodexPaths
from ..services.dedupe import _representative_key, _root_of, _session_catalog
from ..services.provider import detect_provider
from ..stores.index import load_existing_index, upsert_session_index
from ..support import atomic_write, backup_file, backup_operation_slug, normalize_iso, prune_old_backups
from ..stores.session_files import (
    build_canonical_clone_path,
    extract_session_id_from_filename,
    extract_timestamp_from_rollout_name,
    is_codex_rollout_compatible,
    iter_session_files,
    parse_jsonl_records,
    read_session_payload,
)


def build_clone_index(
    paths: CodexPaths,
    *,
    target_provider: str = "",
    active_only: bool = True,
    quiet: bool = False,
    repair_index: bool = False,
) -> set[str]:
    provider = detect_provider(paths, explicit=target_provider)
    cloned_from_ids: set[str] = set()
    total_files = 0
    existing_index = load_existing_index(paths.index_file) if repair_index else {}

    if not quiet:
        print("Building clone index...", end="", flush=True)

    for session_file in iter_session_files(paths, active_only=active_only):
        total_files += 1
        try:
            payload = read_session_payload(session_file)
        except ToolkitError:
            continue

        if payload.get("model_provider") != provider:
            continue

        origin_id = payload.get("cloned_from")
        if isinstance(origin_id, str) and origin_id:
            if is_codex_rollout_compatible(paths, session_file, None):
                cloned_from_ids.add(origin_id)
                clone_id = payload.get("id")
                if repair_index and isinstance(clone_id, str) and clone_id and clone_id not in existing_index:
                    parent_entry = existing_index.get(origin_id, {})
                    thread_name = parent_entry.get("thread_name") or origin_id
                    updated_at = (
                        normalize_iso(str(payload.get("clone_timestamp", "")))
                        or normalize_iso(str(payload.get("timestamp", "")))
                        or datetime.now(timezone.utc).isoformat()
                    )
                    upsert_session_index(paths.index_file, clone_id, thread_name, updated_at)
                    existing_index[clone_id] = {"thread_name": thread_name, "updated_at": updated_at}

    if not quiet:
        print(f" Done. Found {len(cloned_from_ids)} existing clones out of {total_files} files.")

    return cloned_from_ids


def clone_session_file(
    paths: CodexPaths,
    session_file: Path,
    *,
    target_provider: str = "",
    already_cloned_ids: Optional[set[str]] = None,
    dry_run: bool = False,
) -> CloneFileResult:
    session_file = Path(session_file).expanduser()
    provider = detect_provider(paths, explicit=target_provider)
    if already_cloned_ids is None:
        already_cloned_ids = build_clone_index(paths, target_provider=provider, quiet=True)

    try:
        records = parse_jsonl_records(session_file)
    except ToolkitError as exc:
        return CloneFileResult("error", str(exc))

    if not records:
        return CloneFileResult("error", "Empty file")

    meta_index = -1
    session_meta: dict = {}
    for idx, (_, obj) in enumerate(records):
        if obj and obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
            meta_index = idx
            session_meta = dict(obj)
            break

    if meta_index < 0:
        return CloneFileResult("error", "Not a session file")

    payload = dict(session_meta["payload"])
    current_provider = payload.get("model_provider", "")
    current_id = payload.get("id")

    if not isinstance(current_id, str) or not current_id:
        return CloneFileResult("error", "Session id missing from session_meta")

    if current_provider == provider:
        return CloneFileResult("skipped_target", "Already on target provider")

    if current_id in already_cloned_ids:
        return CloneFileResult("skipped_exists", f"Already cloned (ID: {current_id})")

    new_id = str(uuid.uuid4())
    new_payload = dict(payload)
    new_payload["id"] = new_id
    new_payload["model_provider"] = provider
    new_payload["cloned_from"] = current_id
    new_payload["original_provider"] = current_provider
    new_payload["clone_timestamp"] = datetime.now(timezone.utc).isoformat()
    session_meta["payload"] = new_payload

    new_file_path = build_canonical_clone_path(paths, session_file, session_meta, payload, new_id)
    if new_file_path.exists():
        return CloneFileResult("skipped_exists", "Target file collision")

    output_lines = []
    for idx, (raw, obj) in enumerate(records):
        if idx == meta_index:
            output_lines.append(json.dumps(session_meta, ensure_ascii=False, separators=(",", ":")) + "\n")
        elif obj and obj.get("type") == "session_meta":
            embedded_meta = dict(obj)
            embedded_meta["type"] = "session_meta_embedded"
            output_lines.append(json.dumps(embedded_meta, ensure_ascii=False, separators=(",", ":")) + "\n")
        else:
            output_lines.append(raw)

    if not dry_run:
        with atomic_write(new_file_path) as fh:
            fh.writelines(output_lines)
        existing_index = load_existing_index(paths.index_file)
        parent_entry = existing_index.get(current_id, {})
        thread_name = parent_entry.get("thread_name") or current_id
        updated_at = normalize_iso(str(parent_entry.get("updated_at", ""))) or new_payload["clone_timestamp"]
        upsert_session_index(paths.index_file, new_id, thread_name, updated_at)

    already_cloned_ids.add(current_id)
    action_prefix = "[DRY-RUN] Would create" if dry_run else "Created"
    message = f"{action_prefix} {new_file_path.name} (from {current_provider or 'unknown'})"
    return CloneFileResult("cloned", message, new_file_path)


def clone_to_provider(
    paths: CodexPaths,
    *,
    target_provider: str = "",
    dry_run: bool = False,
    active_only: bool = True,
) -> CloneRunResult:
    provider = detect_provider(paths, explicit=target_provider)
    already_cloned = build_clone_index(
        paths,
        target_provider=provider,
        active_only=active_only,
        repair_index=not dry_run,
    )
    _, catalog = _session_catalog(paths, active_only=active_only)
    root_cache: dict[str, str] = {}
    depth_cache: dict[str, int] = {}
    lineages = {}
    for record in catalog.values():
        root_id = _root_of(record.session_id, catalog, root_cache)
        lineages.setdefault(root_id, []).append(record)

    representatives = [
        max(records, key=lambda record: _representative_key(record, catalog, depth_cache))
        for records in lineages.values()
    ]
    representatives.sort(key=lambda record: _representative_key(record, catalog, depth_cache), reverse=True)

    stats = {
        "lineages": len(representatives),
        "candidates": 0,
        "cloned": 0,
        "skipped_exists": 0,
        "skipped_target": 0,
        "error": 0,
    }
    messages = []
    errors = []

    for representative in representatives:
        if representative.model_provider == provider:
            stats["skipped_target"] += 1
            continue
        stats["candidates"] += 1
        result = clone_session_file(
            paths,
            representative.path,
            target_provider=provider,
            already_cloned_ids=already_cloned,
            dry_run=dry_run,
        )
        stats[result.action] = stats.get(result.action, 0) + 1
        if result.action == "cloned":
            messages.append(result.message)
        elif result.action == "error":
            errors.append(f"{representative.path.name}: {result.message}")

    return CloneRunResult(
        provider=provider,
        dry_run=dry_run,
        stats=stats,
        messages=messages,
        errors=errors,
    )


def cleanup_clones(
    paths: CodexPaths,
    *,
    target_provider: str = "",
    dry_run: bool = False,
    active_only: bool = True,
) -> CleanupResult:
    provider = detect_provider(paths, explicit=target_provider)

    originals_by_ts: dict[str, set[str]] = {}
    targets_without_tag_by_ts: dict[str, list[tuple[Path, str]]] = {}
    files_checked = 0

    for session_file in iter_session_files(paths, active_only=active_only):
        files_checked += 1
        timestamp = extract_timestamp_from_rollout_name(session_file.name)
        if not timestamp:
            continue

        session_id = extract_session_id_from_filename(session_file.name) or ""

        try:
            payload = read_session_payload(session_file)
        except ToolkitError:
            continue

        current_provider = payload.get("model_provider", "")
        cloned_from = payload.get("cloned_from")
        if current_provider == provider:
            if not isinstance(cloned_from, str) or not cloned_from:
                targets_without_tag_by_ts.setdefault(timestamp, []).append((session_file, session_id))
        else:
            originals_by_ts.setdefault(timestamp, set()).add(session_id)

    files_to_delete: list[Path] = []
    for timestamp, entries in targets_without_tag_by_ts.items():
        original_ids = originals_by_ts.get(timestamp)
        if not original_ids:
            continue
        for file_path, sid in entries:
            if sid and sid not in original_ids:
                files_to_delete.append(file_path)

    deleted = []
    errors = []
    if not dry_run and files_to_delete:
        # Back the file up before unlinking — `clean-clones` used to delete
        # outright, so a mis-classified real session (e.g. a same-second
        # timestamp collision, or a clone the old code forgot to tag) was
        # unrecoverable. Mirrors dedupe_clones.
        backup_parent = paths.code_dir / "repair_backups"
        prune_old_backups(backup_parent, keep_last=20)
        backup_root = backup_parent / backup_operation_slug("clean-clones")
        backed_up: set[str] = set()
        for target_path in files_to_delete:
            try:
                backup_file(paths.code_dir, backup_root, backed_up, target_path, enabled=True)
                target_path.unlink()
                deleted.append(target_path)
            except OSError as exc:
                errors.append((target_path, str(exc)))

    return CleanupResult(
        provider=provider,
        dry_run=dry_run,
        files_checked=files_checked,
        files_to_delete=files_to_delete,
        deleted=deleted,
        errors=errors,
    )
