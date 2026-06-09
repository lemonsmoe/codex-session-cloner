---
handoff_id: HOFF-2026-06-09-codex-toolkit-baseline
created_at: 2026-06-09T17:55:53+08:00
handoff_owner: 主 agent
related_domains:
  - provider-switching
  - session-lifecycle
  - desktop-visibility
  - tui-cli
  - maintenance-release
related_decisions:
  - DEC-001
  - DEC-002
  - DEC-003
related_events:
  - EVT-2026-06-09-1755-context-baseline
---

# Codex Session Toolkit 当前基线交接

## Current Goal

- 稳定维护 provider 切换、会话谱系、Desktop 可见性和官方风格 TUI。
- 在保留个人定制的前提下持续吸收 `origin/main`。

## Stable Facts

- 当前持久分支是 `codex/new-arch-migration`。
- provider 识别支持 OpenAI official、显式 provider、CcSwitch custom fingerprint。
- switch 是原地 retag 并重建 Desktop 可见性，执行前备份。
- dedupe 仅删除内容相同或前缀的谱系成员。
- Desktop 可见性必须同时处理 index、SQLite 和 global state。
- TUI 使用三个官方风格功能域并保留固定快捷键顺序。

## Active Risks

- 不要在未 dry-run 和未备份的情况下修改真实 `.codex`。
- 不要把 auth、token、API key 或真实会话备份提交到 Git。
- 不要在多个 agent 间共享同一个脏工作树进行并行提交。

## Read Next

1. [项目当前状态](../00_current/index.md)
2. 与任务最相关的领域文档
3. [项目协作运行手册](../../runbooks/context-management.md)
4. [维护与上游同步手册](../../maintenance.md)
