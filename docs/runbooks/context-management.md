# 项目上下文与多 Agent 协作运行手册

## Purpose

本手册用于让新 agent 在不重读全部聊天历史的前提下快速理解 AI CLI Kit，尤其是 Codex Session Toolkit 的 provider 迁移和 Desktop 修复定制。

## Minimal Handoff Contract

用户交接时只需说明：

1. agent 负责的领域或任务；
2. 项目用于管理 Codex/Claude 本地工具与会话；
3. 使用 `$context-management`。

agent 应自行按本文读取上下文，不要求用户再次解释文件顺序。

## When To Use `$context-management`

- 新 agent 接手任务。
- 完成一个可提交的功能或修复。
- 形成长期架构或安全决策。
- 同步上游、阶段验收、分支合并或责任交接。
- 当前文档与代码事实出现偏差。

## Git 与 Worktree 隔离

存在多个并行实现任务时必须使用 `$git-multi-task-collaboration`：

1. 一个实现任务一个 task branch。
2. 一个 task branch 一个外部 sibling worktree。
3. accepted baseline 工作树保持干净。
4. 一个 integration agent 负责 merge 或 cherry-pick。
5. 不把两个未验收任务堆在同一脏工作树。

## Context Layout

- `docs/context/00_current/`：当前有效事实与领域入口。
- `docs/context/01_decisions/`：持续约束未来工作的决策。
- `docs/context/02_events/YYYY/MM/`：按事件时间归档的追加式审计记录。
- `docs/context/03_handoffs/`：阶段或责任交接快照。
- `docs/runbooks/`：执行方法，不承载历史流水。

## Reading Order

1. `docs/context/00_current/index.md`
2. 只读与任务相关的 `domains/<domain>.md`
3. 本运行手册
4. 领域文档信息不足时读取最近 event
5. 发生责任交接时读取 handoff

## Responsibility Mapping

| 任务 | 领域 |
|---|---|
| CcSwitch、OpenAI official、custom provider | `provider-switching` |
| clone、dedupe、历史、归档 | `session-lifecycle` |
| index、threads、workspace、侧边栏 | `desktop-visibility` |
| TUI、CLI、终端渲染 | `tui-cli` |
| 测试、Git、上游同步、发布 | `maintenance-release` |
| Claude Code 清理 | 当前 README 与 `src/ai_cli_kit/claude/`，需要长期协作时再建立独立领域 |

## Role Split

### Working Agent

- 在独立 task branch/worktree 实现。
- 追加自己的 event。
- 只更新触及的 domain。
- 不随意重写宏观 index、旧 event 或旧 handoff。

### Main Agent

- 维护 `00_current/index.md` 和 decision index。
- 在阶段验收、合并、协议变化时刷新 handoff 和 runbook。
- 负责跨领域冲突与最终集成。

### Optional Curator

- 审计领域文档是否滞后。
- 将高价值 event 提升为 current state 或 decision。
- 标记过期分支、阻塞和被替代决策。

## Minimum Update Steps

### 普通实现或修复

1. 完成代码与验证。
2. 在对应年月目录追加 event。
3. 更新触及 domain 的 `last_event_*` 和当前事实。
4. 只有长期约束变化时才写 decision。

### 阶段合并或交接

1. 更新 `maintenance-release`。
2. 必要时刷新 `00_current/index.md`。
3. 创建或更新 handoff，引用 domain、decision 和 event。
4. 不在 handoff 中复制完整历史。

## Time And Conflict Rules

- `event_time` 表示事情实际发生时间，是时间线权威字段。
- `recorded_at` 只表示文档写入时间。
- 不修改其他 agent 的旧 event；纠错时追加 event 并使用 `supersedes`。
- current state 与 event 冲突时，先按 event_time 重建事实，再更新 current。

## Codex 项目验证要求

- 改 provider、repair、clone、dedupe 时运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_codex_toolkit.ps1
```

- 改 TUI 时至少运行：

```powershell
python -m py_compile src\ai_cli_kit\codex\tui\app.py
python -m unittest discover -s tests -p "test_packaging_smoke.py"
python -m unittest discover -s tests -p "test_top_level_dispatch.py"
```

- 真实 `.codex` 验证必须先 dry-run，不得输出 auth 内容。

## Runbook Maintenance

- 只有协作协议、目录、角色或验证标准改变时更新本文件。
- 普通功能变更写 event 和 domain，不频繁改 runbook。
- 若本手册被新决策替代，应保留 Git 历史并更新 decision index。
