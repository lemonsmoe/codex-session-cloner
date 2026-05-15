"""Targeted Desktop visibility repair for one session."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import OrderedDict
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ToolkitError
from ..models import PromoteSessionResult
from ..paths import CodexPaths
from ..services.provider import detect_provider
from ..services.repair import (
    _desktop_visible_path,
    _merge_ordered_strings,
    _merge_string_mapping,
    _sqlite_value,
    _workspace_write_permission,
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
    iso_to_epoch,
    lock_path_for,
    long_path,
    nearest_existing_parent,
    normalize_iso,
    prune_old_backups,
)


def _session_workspace_roots(cwd: str) -> list[str]:
    if not cwd:
        return []
    visible_cwd = _desktop_visible_path(cwd)
    roots: "OrderedDict[str, bool]" = OrderedDict()
    nearest = nearest_existing_parent(visible_cwd) or visible_cwd
    for candidate in (nearest, visible_cwd):
        if candidate:
            roots[_desktop_visible_path(candidate)] = True
    parent = str(Path(nearest).parent) if nearest else ""
    if parent and parent != nearest:
        roots[_desktop_visible_path(parent)] = True
    return list(roots)


def _is_subpath_ci(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.normcase(str(child))).relative_to(Path(os.path.normcase(str(parent))))
        return True
    except ValueError:
        return False


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

    workspace_roots = _session_workspace_roots(cwd)
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

        saved_roots = [item for item in state_data.get("electron-saved-workspace-roots", []) if isinstance(item, str)]
        project_order = [item for item in state_data.get("project-order", []) if isinstance(item, str)]
        for root in workspace_roots:
            root_path = Path(root)
            if not any(_is_subpath_ci(root_path, Path(existing)) for existing in saved_roots):
                saved_roots.append(root)
            if root not in project_order:
                project_order.append(root)

        if not dry_run:
            state_data["electron-saved-workspace-roots"] = saved_roots
            state_data["active-workspace-roots"] = list(saved_roots)
            state_data["project-order"] = project_order
            if workspace_root:
                state_data["projectless-thread-ids"] = _merge_ordered_strings(
                    state_data.get("projectless-thread-ids"),
                    [session_id],
                )
                state_data["thread-workspace-root-hints"] = _merge_string_mapping(
                    state_data.get("thread-workspace-root-hints"),
                    {session_id: workspace_root},
                )
            atom_state = state_data.setdefault("electron-persisted-atom-state", {})
            if isinstance(atom_state, dict):
                collapsed = atom_state.get("sidebar-collapsed-groups")
                if isinstance(collapsed, dict):
                    for root in workspace_roots:
                        collapsed.pop(root, None)
                if workspace_root:
                    atom_state["projectless-thread-ids"] = _merge_ordered_strings(
                        atom_state.get("projectless-thread-ids"),
                        [session_id],
                    )
                    atom_state["thread-workspace-root-hints"] = _merge_string_mapping(
                        atom_state.get("thread-workspace-root-hints"),
                        {session_id: workspace_root},
                    )
                    permissions = atom_state.get("heartbeat-thread-permissions-by-id")
                    if not isinstance(permissions, dict):
                        permissions = {}
                    permissions[session_id] = _workspace_write_permission(workspace_root)
                    atom_state["heartbeat-thread-permissions-by-id"] = permissions

            backup_file(paths.code_dir, backup_root, backed_up, paths.state_file, enabled=True)
            with atomic_write(paths.state_file) as fh:
                json.dump(state_data, fh, ensure_ascii=False, separators=(",", ":"))
                fh.write("\n")

    thread_upserted = False
    state_db = paths.latest_state_db()
    if state_db and state_db.exists():
        if not dry_run:
            backup_file(paths.code_dir, backup_root, backed_up, state_db, enabled=True)
        with closing(sqlite3.connect(long_path(state_db), timeout=30)) as conn, conn:
            cur = conn.cursor()
            row = cur.execute("select name from sqlite_master where type='table' and name='threads'").fetchone()
            if row:
                columns = [r[1] for r in cur.execute("pragma table_info(threads)").fetchall()]
                data = {
                    "id": session_id,
                    "rollout_path": str(session_file),
                    "created_at": iso_to_epoch(created_iso or updated_iso),
                    "updated_at": iso_to_epoch(updated_iso),
                    "source": "vscode",
                    "model_provider": provider,
                    "cwd": cwd,
                    "title": thread_name,
                    "sandbox_policy": json.dumps(turn_context.get("sandbox_policy", {}), ensure_ascii=False, separators=(",", ":")),
                    "approval_mode": turn_context.get("approval_policy", "on-request"),
                    "tokens_used": 0,
                    "has_user_event": 1,
                    "archived": 0,
                    "archived_at": None,
                    "cli_version": updated_meta.get("cli_version", ""),
                    "first_user_message": preview_title or thread_name,
                    "memory_mode": "enabled",
                    "model": turn_context.get("model"),
                    "reasoning_effort": turn_context.get("effort"),
                }
                insert_cols = [name for name in data if name in columns]
                placeholders = ", ".join("?" for _ in insert_cols)
                col_list = ", ".join(insert_cols)
                update_cols = [name for name in insert_cols if name != "id"]
                update_sql = ", ".join(f"{name}=excluded.{name}" for name in update_cols)
                sql = f"insert into threads ({col_list}) values ({placeholders}) on conflict(id) do update set {update_sql}"
                if not dry_run:
                    cur.execute(sql, [_sqlite_value(data[name]) for name in insert_cols])
                thread_upserted = True
            else:
                warnings.append(f"threads table not found in {state_db}")

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
