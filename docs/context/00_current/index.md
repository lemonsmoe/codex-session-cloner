# 项目当前状态

## 项目定位

AI CLI Kit 是一个本地工具箱，当前包含：

- Codex Session Toolkit：管理 Codex 会话、provider 切换、Bundle 迁移和 Desktop 可见性。
- CC Clean：清理 Claude Code 本地状态。

本分支的长期定制重点是 Codex 会话在 OpenAI official、right_code、Redcode、DeepSeek 等 provider 之间稳定迁移，同时确保 Desktop 可发现、可恢复且不会产生失控的重复谱系。

## 当前阶段

- 当前分支：`codex/new-arch-migration`
- 上游：`origin/main`，仓库为 `goodnightzsj/codex-session-cloner`
- 个人远端：`lemonsmoe`，仓库为 `lemonsmoe/codex-session-cloner`
- 当前基线已经同步近期上游架构，并保留本地 provider 切换与 Desktop 修复能力。
- Codex TUI 已恢复为官方风格的 Logo、功能域、方向键导航和内置浏览器。

## 领域入口

| 责任 | 首读文档 |
|---|---|
| provider 识别、CcSwitch、OpenAI official | [provider-switching.md](domains/provider-switching.md) |
| clone、谱系、去重、单会话历史 | [session-lifecycle.md](domains/session-lifecycle.md) |
| index、SQLite、global state、侧边栏可见性 | [desktop-visibility.md](domains/desktop-visibility.md) |
| TUI、CLI、快捷键、交互输出 | [tui-cli.md](domains/tui-cli.md) |
| 测试、同步上游、提交与发布 | [maintenance-release.md](domains/maintenance-release.md) |

## 当前风险

- 真实 `~/.codex` 数据具有破坏性风险，任何写操作前必须 dry-run；删除操作必须备份或明确说明不提供恢复。
- Codex Desktop 的状态结构可能随版本变化，`session_index.jsonl`、最新 `state_*.sqlite` 和 `.codex-global-state.json` 必须作为一个整体验证。
- provider 名称不一定代表真实后端；`custom` 配置必须结合 CcSwitch provider、base URL 和认证方式识别。
- 全量测试中的 Claude 目录 mtime 缓存用例可能受文件系统时间分辨率影响，失败时应单独复跑确认。

## 建议阅读顺序

1. 本文件。
2. 与任务最相关的一个领域文档。
3. [项目协作运行手册](../../runbooks/context-management.md)。
4. 只有领域文档信息不足时，再读取近期事件或交接快照。

## 活跃决策

- [DEC-001：采用分层项目上下文协议](../01_decisions/records/DEC-001-context-protocol.md)
- [DEC-002：真实会话数据采用 dry-run 与备份优先](../01_decisions/records/DEC-002-session-safety.md)
- [DEC-003：会话修复逻辑集中在稳定模块边界](../01_decisions/records/DEC-003-module-boundaries.md)

## 最近交接

- [HOFF-2026-06-09-codex-toolkit-baseline](../03_handoffs/HOFF-2026-06-09-codex-toolkit-baseline.md)
