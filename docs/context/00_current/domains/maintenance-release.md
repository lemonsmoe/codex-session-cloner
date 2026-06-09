---
domain: maintenance-release
current_status: stable
last_event_id: EVT-2026-06-09-1801-personal-main-integration
last_event_time: 2026-06-09T18:01:29+08:00
last_recorded_at: 2026-06-09T18:01:29+08:00
owners:
  - 主 agent
active_branches:
  - codex/new-arch-migration
---

# maintenance-release

## Current State

- `origin` 是上游仓库，`lemonsmoe` 是个人远端。
- 持久定制开发位于 `codex/new-arch-migration`，应频繁同步 `origin/main`，减少集中冲突。
- `lemonsmoe/main` 与 `lemonsmoe/codex/new-arch-migration` 已快进到上下文基线提交 `8506f70`。
- 并行 agent 实现必须使用独立 task branch 和外部 worktree，由一个 integration agent 合入基线。
- 核心 Codex 验证入口是 `scripts/verify_codex_toolkit.ps1`。
- 提交应按功能边界拆分，避免把真实 `.codex` 数据、token、备份或临时产物纳入 Git。

## Active Work

- 暂无待集成提交。

## Blockers

- 远端合并前必须确认目标分支与个人远端 main 的关系。
- 全量测试可能遇到 Claude mtime 缓存用例的文件系统时间分辨率波动。

## Next Actions

- 推送前 fetch `origin` 与 `lemonsmoe`，检查 ahead/behind 和冲突。
- 合并完成后更新本领域文档或追加合并事件。

## Key Paths

- `docs/maintenance.md`
- `docs/runbooks/context-management.md`
- `scripts/verify_codex_toolkit.ps1`
- `tests/test_core_workflows.py`

## Recent Event Links

- [EVT-2026-06-09-1801-personal-main-integration](../../02_events/2026/06/EVT-2026-06-09-1801-personal-main-integration.md)
- [EVT-2026-06-09-1755-context-baseline](../../02_events/2026/06/EVT-2026-06-09-1755-context-baseline.md)
