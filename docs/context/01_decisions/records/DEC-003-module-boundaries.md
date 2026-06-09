---
id: DEC-003
status: accepted
domain: desktop-visibility
decided_at: 2026-05-15T00:00:00+08:00
recorded_at: 2026-06-09T17:55:53+08:00
decision_owner: 项目维护者
supersedes: []
---

# DEC-003 会话修复逻辑集中在稳定模块边界

## Context

早期命令各自写 index、SQLite 和 global state，容易漏字段并在上游同步时产生大面积冲突。

## Decision

- `stores/session_files.py` 负责 rollout 解析、identity、标题和预览。
- `stores/desktop_state.py` 负责 Desktop state、workspace、permissions 和 SQLite threads。
- `services/dedupe.py` 负责谱系图、代表选择和内容安全判断。
- `services/repair.py`、`promote.py`、`switching.py` 只做流程编排。
- 新命令优先复用 stores/service helper，不复制状态写入代码。

## Consequences

- Desktop 新字段只需在少数稳定模块适配。
- service 更容易测试，命令和 TUI 保持薄层。

## Revision Rules

模块职责发生实质变化时新增决策并更新领域文档。
