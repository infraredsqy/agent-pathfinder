---
name: pf-issue-repair
description: 把 issue 池里的债修到线下：按改动域分批、每批 Fix→Review、批过即提交；一条条目怎么处置只看「谁还开着板子」。
disable-model-invocation: true
---

# pf-issue-repair

> 绑哪个插件：`issue` —— 这个技能随那个插件一起装、一起卸；插件不在，这个技能也不在
> 谁跑我：指挥者——管线节点的指挥者，或用户自己的会话
> 我解决什么：把池子里能修的条目修到线下（一个度量 / 一批，范围由叫你来的那份东西定）
> 我派不派 subagent：派，只派一层（Fix / Review）。被派来的执行者读到这份，停手回报，别照它干

取材只有一处：池子。工具与口径见 [`../shared/pf/pool.md`](../shared/pf/pool.md)。**范围不归你定**——管线节点的 spec、「为什么被插进来」那几行、或用户那句话，谁叫你来的谁定；别自己扩到全库。判据过了就停手。

## 一条条目怎么处置

判据只有一条：**这条债的板子还开着吗**。别按「改到了哪一层」「体量大不大」「归哪条线」选处置——那三样都不是判据。

| 板子 | 这条的样子 | 处置 |
|---|---|---|
| **已经拍过** | `reason` / `suggestion` 里把路与规则写全了、或挂着 `plan_path`；`decision=autofix` 说的就是这个 | **做掉它**（走「跑」那一节），完了 `resolve` |
| **还开着** | 没人拍过板、只有用户能选；或照解法做下来发现它行不通 | `update --decision external`，reason 写两条路与代价 |
| **不是债** | 查下来站不住 | `reject`，reason 写为什么不修 |

出口就这三个：押后（`acknowledge`）是池子人机面（`pf-issue-discuss`）管，这里不做。

两条硬规矩：

- **已给解法的条目不许转 `external`**——解法还成立就做掉它；这一趟真排不下的留着不动（权重照算、汇报里点名）。
- **读数按实报。** 没到线就报没到线、点名谁挡着；在管线里跑时读数没到线写 `failed`、把读到的数写进 `-s`，别写 `completed`。

## 取材

1. 先读**标准**：spec 的「完成判定」写的是什么——一个度量的读数（`pf issue weight -v`），还是「这批条目全部走到终态」
2. 取出这一轮要动的条目：**actionable**（三档口径见 pool.md；只加 `--status open` 会把押后的漏掉）**且 `decision=autofix`**，按严重度排（要收窄就加 `--severity` / `--module`）
3. 处置还没定的先定：`decision` 还是 `unknown` 的照上表定处置——能修的改判 `autofix` 并排进这一轮；取舍在用户的改判 `external`。`decision=external` 的不修
4. `reason` / `suggestion` 已经写清怎么修的照它做，不重新讨论；挂 `plan_path` 的先 read 计划

## 跑

循环与指挥者那几条不做，照 `/pf-conduct` 走，这里不抄第二份。本流程只多三条：

- **分批按改动域，批宽受进行中上限管**：同一模块的条目成一批；确需跨模块的改动族整批显式声明。批内的改动能被同一次验证覆盖。每批 Fix 加 Review 是 2 个进行中，几批一起派之前先按 [`../shared/pf/subagents.md`](../shared/pf/subagents.md) 数一遍
- **每批 Fix → Review**：Fix 用 `~/.agents/skills/pf-conduct/subagent-prompts/fix.md`，Review 用同目录的 `review.md`（只读）；prompt 照 pf-conduct「Fix」那一节给
- **批过即 commit**：整批判 no issue 才提交。不在管线里跑时没有节点收尾那一步，提交就落在这里

`critical > 0` → 派新的一轮 Fix，修完回 Review；`minor` 且明确无害 → 记成新条目留待后续同域批次，其余照修。

干活里撞见的新债照旧记进池子（`add`，口径见 [`../shared/pf/pool.md`](../shared/pf/pool.md)），随后**按上面那张表当场定处置**：同批同域能收的收掉，收不动的照实落定。新记的条目照实计入读数——「它是刚发现的」顶不掉这个数。

## 落定

一条一条落定，不留半开（哪条怎么处置见上面的表）：

```pwsh
pf issue resolve <id> --reason "<改在哪、验证读数>"
pf issue update  <id> --decision external --reason "<两条路与代价>"
pf issue reject  <id> --reason "<为什么不修>"
```

**只有用户能拍的**，一条都不许自己定：逐条写进汇报的「留给人」一节，连同两条路与代价。

## 汇报

- **分批构成**：每批含哪些条目、哪个改动域、哪一次提交
- **落定表**：resolve / external / reject 各几条，逐条一行
- **标准读数**：spec 要的那个数现在是多少（`weight -v` 的实测值；没到线就照实说，并点名谁挡着）
- **留给人**：逐条写清取舍在哪、两条路各要什么代价
- **已知未解决项**：这一趟没做掉的条目单列一行（哪条、为什么没做掉、谁开下一轮能接着做），没有就写「无」

## 反模式

开工前读 [`./anti-patterns.md`](./anti-patterns.md) 对照自查。
