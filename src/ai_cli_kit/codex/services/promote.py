"""Targeted Desktop visibility repair for one session."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ToolkitError
from ..models import PromoteSessionResult
from ..paths import CodexPaths
from ..services.provider import detect_provider
from ..stores.desktop_state import (
    merge_desktop_visibility_state,
    session_workspace_roots,
    upsert_thread_entries,
    workspace_write_permission,
)
from ..stores.history import first_history_messages
from ..stores.index import load_existing_index, upsert_session_index
from ..stores.session_files import (
    build_session_preview,
    find_session_file,
    is_placeholder_thread_name,
    parse_jsonl_records,
)
from ..support import (
    atomic_write,
    backup_file,
    classify_session_kind,
    file_lock,
    lock_path_for,
    normalize_iso,
    prune_old_backups,
)


def _write_promoted_session_meta(session_file: Path, records: list[tuple[str, dict | None]], session_meta: dict) -> None:
    first_meta_done = False
    with atomic_write(session_file) as fh:
        for raw, obj in records:
            if not isinstance(obj, dict):
                fh.write(raw)
                continue
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                patched = dict(obj)
                if not first_meta_done:
                    patched["payload"] = session_meta
                    first_meta_done = True
                else:
                    patched["type"] = "session_meta_embedded"
                fh.write(json.dumps(patched, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                fh.write(raw)


def promote_session(
    paths: CodexPaths,
    session_id: str,
    *,
    target_provider: str = "",
    dry_run: bool = False,
) -> PromoteSessionResult:
    if not paths.code_dir.is_dir():
        raise ToolkitError(f"Missing Codex data directory: {paths.code_dir}")

    provider = detect_provider(paths, explicit=target_provider)
    session_file = find_session_file(paths, session_id)
    if session_file is None:
        raise ToolkitError(f"Session file not found: {session_id}")

    backup_parent = paths.code_dir / "repair_backups"
    if not dry_run:
        prune_old_backups(backup_parent, keep_last=20)
    backup_root = backup_parent / f"promote-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    backed_up: set[str] = set()
    warnings: list[str] = []

    records = parse_jsonl_records(session_file)
    session_meta: dict[str, Any] | None = None
    turn_context: dict[str, Any] = {}
    last_timestamp = ""
    for _, obj in records:
        if not isinstance(obj, dict):
            continue
        timestamp = obj.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            last_timestamp = timestamp
        if obj.get("type") == "session_meta" and session_meta is None and isinstance(obj.get("payload"), dict):
            session_meta = dict(obj["payload"])
        elif obj.get("type") == "turn_context" and not turn_context and isinstance(obj.get("payload"), dict):
            turn_context = dict(obj["payload"])

    if session_meta is None:
        raise ToolkitError(f"{session_file}: session_meta not found")

    payload_id = session_meta.get("id")
    if payload_id != session_id:
        warnings.append(f"Filename id and session_meta id differ: {session_id} vs {payload_id}")

    updated_meta = dict(session_meta)
    retagged = False
    converted = False
    if provider and updated_meta.get("model_provider") != provider:
        updated_meta["model_provider"] = provider
        retagged = True

    if classify_session_kind(updated_meta.get("source", ""), updated_meta.get("originator", "")) != "desktop":
        updated_meta["source"] = "vscode"
        updated_meta["originator"] = "Codex Desktop"
        converted = True

    if (retagged or converted) and not dry_run:
        backup_file(paths.code_dir, backup_root, backed_up, session_file, enabled=True)
        _write_promoted_session_meta(session_file, records, updated_meta)

    cwd = updated_meta.get("cwd") if isinstance(updated_meta.get("cwd"), str) else ""
    created_iso = normalize_iso(str(updated_meta.get("timestamp", ""))) or normalize_iso(last_timestamp)
    updated_iso = (
        normalize_iso(last_timestamp)
        or created_iso
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    existing_index = load_existing_index(paths.index_file)
    history_first_messages = first_history_messages(paths.history_file)
    preview_title = build_session_preview(history_first_messages.get(session_id, ""), session_file, cwd)
    existing_thread_name = existing_index.get(session_id, {}).get("thread_name", "")
    thread_name = preview_title if is_placeholder_thread_name(existing_thread_name, session_id) else existing_thread_name or preview_title or session_id

    workspace_roots = session_workspace_roots(cwd)
    workspace_root = workspace_roots[0] if workspace_roots else ""

    if not dry_run:
        backup_file(paths.code_dir, backup_root, backed_up, paths.index_file, enabled=paths.index_file.exists())
        upsert_session_index(paths.index_file, session_id, thread_name, updated_iso)

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
            visible_thread_ids=([session_id] if workspace_root else []),
            thread_workspace_hints=({session_id: workspace_root} if workspace_root else {}),
            thread_permissions=({session_id: workspace_write_permission(workspace_root)} if workspace_root else {}),
            expand_workspace_roots=workspace_roots,
        )

        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, paths.state_file, enabled=True)
            with atomic_write(paths.state_file) as fh:
                json.dump(state_data, fh, ensure_ascii=False, separators=(",", ":"))
                fh.write("\n")

    thread_upserted = False
    state_db = paths.latest_state_db()
    if state_db and state_db.exists():
        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, state_db, enabled=True)
        thread_count, thread_warnings = upsert_thread_entries(
            state_db,
            [
                {
                    "id": session_id,
                    "session_file": session_file,
                    "created_iso": created_iso or updated_iso,
                    "updated_iso": updated_iso,
                    "source": "vscode",
                    "model_provider": provider,
                    "cwd": cwd,
                    "thread_name": thread_name,
                    "sandbox_policy": json.dumps(turn_context.get("sandbox_policy", {}), ensure_ascii=False, separators=(",", ":")),
                    "approval_mode": turn_context.get("approval_policy", "on-request"),
                    "archived": 0,
                    "cli_version": updated_meta.get("cli_version", ""),
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

    return PromoteSessionResult(
        provider=provider,
        session_id=session_id,
        dry_run=dry_run,
        session_file=session_file,
        index_upserted=True,
        thread_upserted=thread_upserted,
        state_updated=state_updated,
        retagged=retagged,
        converted_to_desktop=converted,
        workspace_root=workspace_root,
        backup_root=(None if dry_run else backup_root),
        warnings=warnings,
    )
