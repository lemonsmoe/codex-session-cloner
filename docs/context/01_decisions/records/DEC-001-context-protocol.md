---
id: DEC-001
status: accepted
domain: maintenance-release
decided_at: 2026-06-09T17:55:53+08:00
recorded_at: 2026-06-09T17:55:53+08:00
decision_owner: 项目维护者
supersedes: []
---

# DEC-001 采用分层项目上下文协议

## Context

项目经历了多轮 provider 修复、上游同步和 agent 交接，仅依赖聊天历史会导致当前事实、历史事件和执行规范混在一起。

## Decision

- 使用 `docs/context/` 保存 current、decision、event 和 handoff 四层上下文。
- 使用 `docs/runbooks/context-management.md` 保存 agent 执行流程。
- 新 agent 默认先读 current index，再只读相关领域文档。
- 并行实现任务必须使用独立分支和 worktree。

## Consequences

- 当前态文档保持精简，历史事实通过 append-only event 审计。
- 普通工作 agent 只更新自己涉及的事件和领域文件。
- 主 agent 在阶段收口、合并或交接时维护宏观索引。

## Revision Rules

协议变化时创建新的决策记录，并将本记录标记为 superseded，不删除历史。
