---
domain: tui-cli
current_status: stable
last_event_id: EVT-2026-06-09-1755-context-baseline
last_event_time: 2026-06-09T17:55:53+08:00
last_recorded_at: 2026-06-09T17:55:53+08:00
owners:
  - 主 agent
active_branches:
  - codex/new-arch-migration
---

# tui-cli

## Current State

- 无参入口进入官方风格 TUI：Logo、三个功能域、方向键与 Enter 导航。
- 三个功能域是 `Session / Browse`、`Bundle / Transfer`、`Repair / Maintenance`。
- TUI 内置会话浏览器、Bundle 浏览器、详情页、筛选和批量导入选择。
- Repair 数字快捷键顺序固定：
  - `1` switch provider
  - `2` restore backup
  - `3` repair Desktop
  - `4` repair dry-run
  - `5` dedupe
  - `6` dedupe dry-run
  - `7` promote session
  - `8` repair session history
  - `9` clone provider
- `s` 为 switch dry-run，`r` 为 clone dry-run；危险删除操作保留二次确认。
- TUI 使用共享 `ScreenModeDecision`，兼容主屏/备用屏和 hub 嵌套。
- 矮终端会折叠中间内容并保留底部导航提示。

## Active Work

- 暂无未完成实现。
- 新命令需同时评估 CLI、TUI action notes、快捷键冲突和 packaging smoke 测试。

## Blockers

- Windows legacy console 的 VT 支持不稳定，必须保留无 VT 的 clear-and-print fallback。
- 终端视觉验收不能完全由单元测试代替，至少需要渲染快照或人工打开检查。

## Next Actions

- 修改 TUI 后运行 `test_packaging_smoke.py`、`test_top_level_dispatch.py` 和 `py_compile`。
- 不要再次用简化数字列表替换官方分区交互。

## Key Paths

- `src/ai_cli_kit/codex/tui/app.py`
- `src/ai_cli_kit/codex/tui/terminal.py`
- `src/ai_cli_kit/codex/cli.py`
- `src/ai_cli_kit/codex/commands.py`
- `tests/test_packaging_smoke.py`
- `tests/test_top_level_dispatch.py`

## Recent Event Links

- [EVT-2026-06-09-1755-context-baseline](../../02_events/2026/06/EVT-2026-06-09-1755-context-baseline.md)
