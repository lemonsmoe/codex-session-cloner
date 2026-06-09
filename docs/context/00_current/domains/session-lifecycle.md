---
domain: session-lifecycle
current_status: stable
last_event_id: EVT-2026-06-09-1755-context-baseline
last_event_time: 2026-06-09T17:55:53+08:00
last_recorded_at: 2026-06-09T17:55:53+08:00
owners:
  - 主 agent
active_branches:
  - codex/new-arch-migration
---

# session-lifecycle

## Current State

- rollout JSONL 是会话内容的主要载体，active 与 archived 由 `iter_session_files` 统一扫描。
- clone 通过 `cloned_from` 维护直接来源；逻辑谱系通过持续追溯根节点识别。
- 谱系代表排序依次考虑最后活动时间、链深、文件 mtime 和路径，保证结果稳定。
- 去重只删除与代表内容相同或为代表前缀的成员；发生分叉的内容不会自动删除。
- 去重会先备份，并清理 index、SQLite threads 和 Desktop global state 中的残留引用。
- `promote-session` 用于强制补齐指定会话可见性；`repair-session-history` 用于修复历史注册并在必要时建立 clean clone。
- `clean-archived` 删除归档会话和元数据，不创建备份，真实执行必须显式传 `--yes`。

## Active Work

- 暂无未完成实现。
- 后续若调整 canonical 内容比较，必须保持 provider、ID、timestamp 等迁移元数据不会制造假差异。

## Blockers

- 两个同谱系文件若内容已分叉，工具无法自动判断应合并哪一侧的独有消息。
- 真实删除后的语义恢复依赖操作前备份；`clean-archived` 明确不提供恢复。

## Next Actions

- 处理重复会话时先执行 `dedupe-clones <provider> --dry-run` 并检查 reason。
- 单会话问题先使用 `promote-session` 或 `repair-session-history`，避免全库 retag。

## Key Paths

- `src/ai_cli_kit/codex/services/clone.py`
- `src/ai_cli_kit/codex/services/dedupe.py`
- `src/ai_cli_kit/codex/services/promote.py`
- `src/ai_cli_kit/codex/services/history_repair.py`
- `src/ai_cli_kit/codex/services/archive_cleanup.py`
- `src/ai_cli_kit/codex/stores/session_files.py`

## Recent Event Links

- [EVT-2026-06-09-1755-context-baseline](../../02_events/2026/06/EVT-2026-06-09-1755-context-baseline.md)
