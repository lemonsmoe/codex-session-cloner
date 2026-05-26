"""Repair Desktop history registration for one Codex session."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ToolkitError
from ..models import SessionHistoryRepairResult
from ..paths import CodexPaths
from ..services.provider import detect_provider
from ..stores.desktop_state import (
    desktop_visible_path,
    merge_desktop_visibility_state,
    session_workspace_roots,
    upsert_thread_entries,
    workspace_write_permission,
)
from ..stores.history import first_history_messages
from ..stores.index import load_existing_index, upsert_session_index
from ..stores.session_files import (
    build_canonical_clone_path,
    build_session_preview,
    find_session_file,
    is_placeholder_thread_name,
    parse_jsonl_records,
)
from ..support import (
    atomic_write,
    backup_file,
    file_lock,
    lock_path_for,
    long_path,
    normalize_iso,
    prune_old_backups,
)


def _message_counts(records: list[tuple[str, dict | None]]) -> tuple[int, int, int, int]:
    users = assistants = response_items = event_msgs = 0
    for _, obj in records:
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if obj.get("type") == "response_item":
            response_items += 1
            if isinstance(payload, dict):
                role = payload.get("role")
                if role == "user":
                    users += 1
                elif role == "assistant":
                    assistants += 1
        elif obj.get("type") == "event_msg":
            event_msgs += 1
            if isinstance(payload, dict):
                event_type = payload.get("type")
                if event_type == "user_message":
                    users += 1
                elif event_type in {"agent_message", "assistant_message"}:
                    assistants += 1
    return users, assistants, response_items, event_msgs


def _thread_row(state_db: Path | None, session_id: str) -> dict[str, str]:
    if state_db is None or not state_db.exists():
        return {}
    try:
        with sqlite3.connect(long_path(state_db), timeout=30) as conn:
            row = conn.execute(
                "select rollout_path, model_provider, cwd from threads where id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return {
        "rollout_path": row[0] if isinstance(row[0], str) else "",
        "model_provider": row[1] if isinstance(row[1], str) else "",
        "cwd": row[2] if isinstance(row[2], str) else "",
    }


def _first_session_meta(records: list[tuple[str, dict | None]]) -> tuple[int, dict, dict]:
    for idx, (_, obj) in enumerate(records):
        if isinstance(obj, dict) and obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
            return idx, obj, dict(obj["payload"])
    raise ToolkitError("session_meta not found")


def _first_turn_context(records: list[tuple[str, dict | None]]) -> dict[str, Any]:
    for _, obj in records:
        if isinstance(obj, dict) and obj.get("type") == "turn_context" and isinstance(obj.get("payload"), dict):
            return dict(obj["payload"])
    return {}


def _write_repaired_session(
    session_file: Path,
    records: list[tuple[str, dict | None]],
    *,
    meta_index: int,
    fixed_meta: dict,
) -> None:
    with atomic_write(session_file) as fh:
        for idx, (raw, obj) in enumerate(records):
            if not isinstance(obj, dict):
                fh.write(raw)
                continue
            if idx == meta_index:
                patched = dict(obj)
                patched["payload"] = fixed_meta
                fh.write(json.dumps(patched, ensure_ascii=False, separators=(",", ":")) + "\n")
                continue
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                patched = dict(obj)
                patched["type"] = "session_meta_embedded"
                fh.write(json.dumps(patched, ensure_ascii=False, separators=(",", ":")) + "\n")
                continue
            fh.write(raw)


def _write_clean_clone(
    paths: CodexPaths,
    source_file: Path,
    records: list[tuple[str, dict | None]],
    *,
    meta_index: int,
    original_meta_obj: dict,
    original_payload: dict,
    fixed_meta: dict,
) -> tuple[str, Path]:
    new_id = str(uuid.uuid4())
    clone_meta = dict(fixed_meta)
    clone_meta["id"] = new_id
    clone_meta["cloned_from"] = fixed_meta.get("id", "")
    clone_meta["original_provider"] = original_payload.get("model_provider", "")
    clone_meta["clone_timestamp"] = datetime.now(timezone.utc).isoformat()

    new_file = build_canonical_clone_path(paths, source_file, original_meta_obj, original_payload, new_id)
    if new_file.exists():
        raise ToolkitError(f"Target clean clone already exists: {new_file}")
    _write_repaired_session(new_file, records, meta_index=meta_index, fixed_meta=clone_meta)
    return new_id, new_file


def repair_session_history(
    paths: CodexPaths,
    session_id: str,
    *,
    target_provider: str = "",
    dry_run: bool = False,
    rebuild_clone: bool = False,
) -> SessionHistoryRepairResult:
    if not paths.code_dir.is_dir():
        raise ToolkitError(f"Missing Codex data directory: {paths.code_dir}")

    session_file = find_session_file(paths, session_id)
    if session_file is None:
        raise ToolkitError(f"Session file not found: {session_id}")

    records = parse_jsonl_records(session_file)
    if not records:
        raise ToolkitError(f"Empty session file: {session_file}")
    meta_index, meta_obj, session_meta = _first_session_meta(records)
    if session_meta.get("id") != session_id:
        raise ToolkitError(f"session_meta id mismatch: expected {session_id}, got {session_meta.get('id')}")

    session_provider = session_meta.get("model_provider")
    provider = target_provider or (session_provider.strip() if isinstance(session_provider, str) and session_provider.strip() else "")
    if not provider:
        provider = detect_provider(paths)

    users, assistants, response_items, event_msgs = _message_counts(records)
    turn_context = _first_turn_context(records)
    last_timestamp = ""
    for _, obj in records:
        if isinstance(obj, dict) and isinstance(obj.get("timestamp"), str) and obj["timestamp"]:
            last_timestamp = obj["timestamp"]
    state_db = paths.latest_state_db()
    existing_row = _thread_row(state_db, session_id)
    current_rollout_path = existing_row.get("rollout_path", "")
    current_provider = existing_row.get("model_provider", "")

    rollout_exists = Path(current_rollout_path).exists() if current_rollout_path else False
    provider_mismatch = bool(current_provider and current_provider != provider)
    rollout_mismatch = bool(current_rollout_path and Path(current_rollout_path) != session_file)
    needs_rebuild = bool(rebuild_clone or (current_rollout_path and (not rollout_exists or rollout_mismatch)))

    raw_cwd = session_meta.get("cwd") if isinstance(session_meta.get("cwd"), str) else ""
    clean_cwd = desktop_visible_path(raw_cwd)
    fixed_meta = dict(session_meta)
    fixed_meta["id"] = session_id
    fixed_meta["cwd"] = clean_cwd
    fixed_meta["source"] = "vscode"
    fixed_meta["originator"] = "Codex Desktop"
    fixed_meta["model_provider"] = provider

    backup_parent = paths.code_dir / "repair_backups"
    if not dry_run:
        prune_old_backups(backup_parent, keep_last=20)
    backup_root = backup_parent / f"history-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    backed_up: set[str] = set()
    warnings: list[str] = []

    target_session_id = session_id
    target_file = session_file
    rebuilt = False
    if rebuild_clone:
        if not dry_run:
            target_session_id, target_file = _write_clean_clone(
                paths,
                session_file,
                records,
                meta_index=meta_index,
                original_meta_obj=meta_obj,
                original_payload=session_meta,
                fixed_meta=fixed_meta,
            )
        else:
            target_session_id = "<new-clean-clone>"
        rebuilt = not dry_run
    elif fixed_meta != session_meta:
        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, session_file, enabled=True)
            _write_repaired_session(session_file, records, meta_index=meta_index, fixed_meta=fixed_meta)

    effective_meta = dict(fixed_meta)
    effective_meta["id"] = target_session_id
    cwd = clean_cwd
    created_iso = normalize_iso(str(effective_meta.get("timestamp", "")))
    updated_iso = normalize_iso(last_timestamp) or created_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing_index = load_existing_index(paths.index_file)
    history_first_messages = first_history_messages(paths.history_file)
    preview_title = build_session_preview(history_first_messages.get(session_id, ""), session_file, cwd)
    existing_thread_name = existing_index.get(session_id, {}).get("thread_name", "")
    thread_name = (
        preview_title
        if is_placeholder_thread_name(existing_thread_name, session_id)
        else existing_thread_name or preview_title or session_id
    )
    workspace_roots = session_workspace_roots(cwd)
    workspace_root = workspace_roots[0] if workspace_roots else ""

    index_upserted = False
    if not dry_run:
        backup_file(paths.code_dir, backup_root, backed_up, paths.index_file, enabled=paths.index_file.exists())
        upsert_session_index(paths.index_file, target_session_id, thread_name, updated_iso)
        index_upserted = True

    state_updated = bool(workspace_root)
    state_context = nullcontext() if dry_run else file_lock(lock_path_for(paths.state_file))
    with state_context:
        try:
            state_data = json.loads(paths.state_file.read_text(encoding="utf-8")) if paths.state_file.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"Warning: failed to read state file {paths.state_file}: {exc}")
            state_data = {}
        if not isinstance(state_data, dict):
            state_data = {}
        state_data = merge_desktop_visibility_state(
            state_data,
            workspace_roots=workspace_roots,
            visible_thread_ids=([target_session_id] if workspace_root else []),
            thread_workspace_hints=({target_session_id: workspace_root} if workspace_root else {}),
            thread_permissions=({target_session_id: workspace_write_permission(workspace_root)} if workspace_root else {}),
            expand_workspace_roots=workspace_roots,
        )
        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, paths.state_file, enabled=True)
            with atomic_write(paths.state_file) as fh:
                json.dump(state_data, fh, ensure_ascii=False, separators=(",", ":"))
                fh.write("\n")

    thread_upserted = False
    if state_db and state_db.exists():
        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, state_db, enabled=True)
        thread_count, thread_warnings = upsert_thread_entries(
            state_db,
            [
                {
                    "id": target_session_id,
                    "session_file": target_file,
                    "created_iso": created_iso or updated_iso,
                    "updated_iso": updated_iso,
                    "source": "vscode",
                    "model_provider": provider,
                    "cwd": cwd,
                    "thread_name": thread_name,
                    "sandbox_policy": json.dumps(turn_context.get("sandbox_policy", {}), ensure_ascii=False, separators=(",", ":")),
                    "approval_mode": turn_context.get("approval_policy", "on-request"),
                    "archived": 0,
                    "cli_version": effective_meta.get("cli_version", ""),
                    "first_user_message": preview_title or thread_name,
                    "model": turn_context.get("model"),
                    "reasoning_effort": turn_context.get("effort"),
                }
            ],
            provider=provider,
            dry_run=dry_run,
        )
        thread_upserted = thread_count > 0
        warnings.extend(thread_warnings)
    if provider_mismatch:
        warnings.append(f"threads.model_provider was {current_provider}, repaired target is {provider}")

    return SessionHistoryRepairResult(
        provider=provider,
        session_id=session_id,
        dry_run=dry_run,
        original_session_file=session_file,
        target_session_id=target_session_id,
        target_session_file=target_file,
        line_count=len(records),
        user_message_count=users,
        assistant_message_count=assistants,
        response_item_count=response_items,
        event_msg_count=event_msgs,
        current_rollout_path=current_rollout_path,
        current_provider=current_provider,
        needs_rebuild=needs_rebuild,
        rebuilt=rebuilt,
        index_upserted=index_upserted,
        thread_upserted=thread_upserted,
        state_updated=state_updated,
        backup_root=(None if dry_run else backup_root),
        warnings=warnings,
    )
