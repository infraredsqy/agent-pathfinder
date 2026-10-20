---
name: pf-issue-discuss
description: 池子的人机面：记一条、看现状、复核哪些条目真要用户拍板，再逐批把 external 定下来。
disable-model-invocation: true
---

# pf-issue-discuss

> 谁跑我：用户（`/pf-issue-discuss`）
> 我解决什么：池子的读写与判定——收一条、看现状、定 autofix/external、把要拍板的摆到用户面前
> 我派不派 subagent：不派。要修走 `/pf-issue-repair`，要扫走 `/pf-issue-scan`

工具与口径见 [`../shared/pf/pool.md`](../shared/pf/pool.md)：命令只走 `pf issue`，动手前先 `--help`。

## 进门先认意图

| 用户说 | 做什么 |
|---|---|
| "hover 时高亮盖住了展开箭头" | **记录**：起 kebab-case id，`add --severity P3 --decision unknown --source manual --title "<原话>"`，回一句「已记：`<id>`」 |
| "池子里现在有多少" | **看现状**：`status` + `top --limit 10`，报总数与按严重度、按 source 的分档 |
| "那条修好了" | **结掉**：`search` 定位 → `resolve <id>`；不确定是哪条就摆出来让用户选 |
| "陪我过一遍 external" | **裁定**：走下面两步 |
| "把这条 bug 修了" | 单条调试转 `/trace`；同一批的债转 `/pf-issue-repair` |

记录时不要追问：一句话能记就行。用户多说了文件或模块，顺手填 `--module` / `--files` / `--description`。

## 裁定

### 一、先复核，别把伪 external 端上去

逐条看 `decision=external` 的条目：suggestion 已经明确到不需要权衡的 → `update <id> --decision autofix --reason "无需讨论：<一句话修法>"`。剩下的才是真要用户拍板的。

### 二、逐批讨论，一次 ≤4 个决策

多了会耗掉判断力，超出的下一批再摆。

- 摆给用户时说**人话**：这是什么毛病、为什么危险、有哪几条路、各自代价。函数名 / 变量名 / 行号 / 调用链留给条目本身——那是给动手的人读的机器语言。两种语言并存，不互相替代。
- 用户定了之后逐条落定：
  - **简单问题**（单一函数提取、常量替换，约 5 行变动）→ `update <id> --decision autofix --status open --reason "<用户定的内容>"`
  - **复杂问题**（跨多文件、要设计新接口）→ 先把设计写进 `.pf/issues/specs/<id>.md`，再 `update <id> --decision autofix --status open --plan-path <path> --reason "<用户定的内容>"`
  - 不修 → `reject <id> --reason "<原因>"`；押后 → `acknowledge <id> --reason "<等什么>"`（押后仍算权重，见 pool.md）
- **`--reason` 里写用户定的内容本身**（"统一报错"），不写"已讨论"——动手的人只读得到这一句。
- 每批收尾报一句：还剩几条 external。

## 边界

| 场景 | 处理 |
|---|---|
| 深聊某条的实现细节 | 建议转 `/engineering` |
| 大规模重构信号 | 单独立一条（阈值与口径见 [`../shared/pf/pool.md`](../shared/pf/pool.md)），建议转 `/engineering` |
| 要修一批 | `/pf-issue-repair` |
| 池子该刷新了 | `/pf-issue-scan` |
| 该进图的新功能 | `/pf-route` |

## 反模式

开工前读 [`./anti-patterns.md`](./anti-patterns.md) 对照自查。
