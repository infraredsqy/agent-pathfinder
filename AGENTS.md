# agent-pathfinder —— pf：agent 执行管线的工具与技能仓

> 本文件（AGENTS.md / CLAUDE.md）不得超过 100 行。

动手前必读：[`tools/conduct/AGENTS.md`](tools/conduct/AGENTS.md) —— conduct 工具的开发纪律（派发通路是实弹）。

- docs/ —— 这套工具的契约正本：状态与推进、派发与鉴权、节点 tag 分流（改 conduct 行为前看）
- skills/ —— pf-* 技能本体与 shared 公共卡（`install.ps1` 用 junction 挂进 `~/.agents/skills`）
- tools/ —— `pf` 命令与实现：`bin/` 入口、`conduct/` 管线、`dag/` 图、`issue/` 池
- install.ps1 —— 装机与自检：挂技能、`pf` 上 PATH；幂等，重跑即修复
- .gitignore —— 工作区杂物的黑名单

## 文档纪律

- 文档只写现状，口径跟代码与测试走（mode 的人闸位置、调用行的长相、`ask_user` 的种类这类，
  以 `tools/conduct` 的行为为准）；改了行为，同一发里同步改文档。
- 不写历史、不写「以前叫 X」；条数、耗时这类读数现跑现填，别抄进散文。
- **文档不抄代码**：字段名、命令与旗标、常量、文件清单、词表这类东西只在代码（或 `--help`）里有一
  份；文档写的是设计口径、判据与「为什么这么定」。同一件事在一份文档里也不讲第二遍。
- 引用只写「哪份文件 · 哪个小节」的名字，不写章节号、行号 —— 代码注释里一条都不引文档。
