---
id: DEC-002
status: accepted
domain: session-lifecycle
decided_at: 2026-05-14T00:00:00+08:00
recorded_at: 2026-06-09T17:55:53+08:00
decision_owner: 项目维护者
supersedes: []
---

# DEC-002 真实会话数据采用 dry-run 与备份优先

## Context

Codex Desktop 可见性涉及 rollout、index、SQLite 和 global state。错误写入可能使全部 provider 都暂时无法发现旧会话。

## Decision

- 修改真实 `~/.codex` 前先运行 dry-run。
- switch、repair、dedupe 等可逆操作在写入前备份受影响文件。
- 不主动运行真实 dedupe 或删除真实 session，除非用户明确要求。
- `clean-archived` 不创建备份，必须明确提示并要求 `--yes`。
- 任何日志、测试和文档不得输出 auth token 或 API key。

## Consequences

- 工具操作更保守，但故障时可通过 `restore-backup` 回退。
- 代码测试使用临时目录，真实数据验证与单元测试分离。

## Revision Rules

只有在提供同等或更强恢复保障时，才能放宽该决策。
