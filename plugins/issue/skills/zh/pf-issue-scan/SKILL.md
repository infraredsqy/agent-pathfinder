---
name: pf-issue-scan
description: 扫项目代码找还没入库的债，写进 issue 池，并核对已有条目还在不在。
disable-model-invocation: true
---

# pf-issue-scan

> 绑哪个插件：`issue` —— 这个技能随那个插件一起装、一起卸；插件不在，这个技能也不在
> 谁跑我：用户（`/pf-issue-scan`）／指挥者（管线节点 spec 的 `method: ` 行点名）
> 我解决什么：扫代码找新债 + 核对旧债还在不在，只入池
> 我派不派 subagent：派，一层（scan）；执行者读到这份，停手回报

扫描只干两件事：找新债写进池子、确认旧债还在不在。**不修、不和用户讨论、不判 `decision`**——新条目一律留 `unknown`，判定晚一步由 `/pf-issue-discuss` 做（扫描那一刻判的，等真动手时早 stale 了）。

池子的工具与口径见 [`../shared/pf/pool.md`](../shared/pf/pool.md)。

## 准备

1. 读项目 CLAUDE.md / docs/：模块怎么分、架构约定是什么。这是拆扫描面和写 prompt 的依据
2. 摸清改动面：代码根目录、文件量级、哪些目录是工具／测试／资产
3. 调 `status` 与 `list` 拿**在位条目**清单：这一轮要交给 scan 做存活核对

## 派扫描

按**模块 × 维度**拆面（带写工具的 subagent，发现要落文件），一个面只盯一份。维度：重复代码／职责越界／结构问题。模块只有一两个时，自己裁量要不要合并维度。

面拆完之后按进行中上限收起批来：`pf config get max_agents` 是上限，**一个 subagent 可以装几个面**——怎么并、什么时候分批，见 [`../shared/pf/subagents.md`](../shared/pf/subagents.md)。

每个 prompt 必须给：

- 角色卡：`<本技能目录>/scan.md`（绝对路径）——角色、输出格式、禁止项都在里面
- 项目架构约定（从文档提炼）、分配给它的文件清单（装了几份就列几份的文件）
- 在位条目清单（做存活核对）
- 输出路径：`.pf/meta/scan/<module-slug>.json`（一个 subagent 装几份就写几份文件，一份一个文件）

全批回来了再往下走。

## 合并入库

1. read 所有 `.pf/meta/scan/*.json`
2. **去重合并**：`files` 有交集，且（`functions` 有交集，或 description 语义相近）→ 合成一条，留描述更详细的那份，吸收另一份的 `files` / `functions`
3. `add --from-file <合并后的>.json --auto-id`；跨多文件、要设计新组件的那类另挂 `--plan-path .pf/issues/specs/<id>.md`
4. **存活核对**：汇总各 scan 的 `alive_ids` → `mark <ids>`（只累加 check_count 并报 candidates，**永不改状态**）。结掉一条只走逐条 `resolve <id> --reason`，且只在该条有 scan 明确的「三层定位找不到」报告时动手；刚入库的条目不在名单里是正常的
5. 清掉 `.pf/meta/scan/` 下的临时文件
6. 汇报：新增 N 条（按严重度分档）、核对结果、candidates 名单

## 边界

- **只能由用户或指挥者跑**：这份技能要派 subagent；被派来的执行者读到它，停手回报
- 不判 `decision`、不修代码、不改 spec、不进图——挑选与排序是 DAG 的事
- 撞见「该进图的新功能」记一句给用户，别自己建节点，那是 `/pf-route` 的活
- 大规模重构信号单独立一条，阈值与判据见 [`../shared/pf/pool.md`](../shared/pf/pool.md)

## 反模式

开工前读 [`./anti-patterns.md`](./anti-patterns.md) 对照自查。
