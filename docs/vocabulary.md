# 词汇表 · 卡片 / 线路 / 条子

> 这张表是唯一权威。面板、skill 文本都引用它，不另写第二份。
>
> 三条规矩：
>
> 1. **一个轴一套词。** 同一个词形不许出现在两个轴上。
> 2. **异义必分，同义可留。** 跨轴复用只在两处意思完全相同时才允许 —— 全表只有 `completed` 一个。
> 3. **派生词带 `-ing`，盘上的词不带。** 看到 `running` / `ready` 就知道它是算出来的，不在档案里。

## 一、轴 ① 卡片的状态

写在卡片自己的档案里（`ideas.json` 的 `nodes[].status`），**只有三个词**。

| 词 | 中文 | 含义 | 不再是 |
|---|---|---|---|
| `todo` | 待办 | 在局中、未交付。**正被某条线做着的卡，状态仍然是 `todo`** | |
| `shelved` | 等待处理 | 出局、等人救。不 ready、不被推荐，下游整棵子树停在它后面 | 旧写法 `held` / `hold` / `failed` |
| `completed` | 已成 | 已交付，终态 | |

动词是**搁置**：把一个节点搁置 → 标 `shelved`。放回去：`pf dag revive <id>`。

> 「被某条线做着的卡还是 `todo`」是这个轴唯一容易反直觉的地方，也是它必须如此的原因：
> 「谁在做它」记在**持有关系**（轴 ②）里，卡片自己的档案只回答「交付了没有」。两件事各占一个字段。

## 二、轴 ② 线路的状态与持有

写在 `state.json` 的 `cycles[]` 里。

| 词 | 中文 | 含义 |
|---|---|---|
| `active` / `inactive` | 在服务 / 不在服务 | 这条线该不该往前推。工具自己**永远只写 `inactive`**，`active` 只在人把线放回服务时写 |
| `holding` | 持有 | 这条线认领了谁：`[{id, since}]`。一条线可以同时持有几格（宽度是 `parallel`） |
| `policies.when_shelved` | 搁置时怎么办 | 自己的节点没交付（于是被搁置）时：`stop`（缺省，停下等人）还是 `continue`（接着挑下一个）。**两种取值下节点都标 `shelved`** |
| `policies.parallel` | 同时持有几格 | 正整数 |
| `policies.spawner` | 会话由谁开 | `dsh` / `zcode` |

遗留的老写法一律读作 `inactive`（不再接受）：`paused` / `completed` / `failed`（作线路状态时）。

## 三、轴 ③ 条子上的判决

干活的会话自己写，写在 `signals/<cycle>/<node>.json` 里。**判决词永远不出现在卡片状态里。**

| 词 | 中文 | 含义 |
|---|---|---|
| `pending` | 还没结论 | 重试留下的记号。**永远当红灯读**：留着它，读者就一直在等 |
| `completed` | 交付了 | |
| `failed` | 没交付 | 需求做不到、或 spec 自相矛盾 |
| `stopped` | 没交付 | 自己主动停手不做的 |

判决 `failed` / `stopped` 落到卡片上 = 把这张卡标 `shelved`；判决在条子上写的还是它自己那个词。
（老写法 `running` 作条子状态时读作 `pending`，不再接受。）

## 四、轴 ④ 派生标签

读的时候算出来，**从不落盘**。`ready` 与 `running` 互斥。

| 词 | 中文 | 判定 |
|---|---|---|
| `running` | 在手 | 卡是 `todo`，**且**它在某条线的 `holding` 里 |
| `ready` | 可开工 | 卡是 `todo`、每个依赖都 `completed`、**且没有任何线持有它** |

判定 `ready` 的三条缺一不可。少了第一条，被搁置的卡会装成可开工；少了第三条，别人手里正做的卡会装成可开工。

> 边界：桌面 Automation 那张表的列名里也有一个 `running`（`tools/conduct/spawn/zcode-dispatch.py` 的
> `COLUMNS`）。那是别人 schema 的列，说的是那个进程在跑，与本表无关，别去改它。

## 五、可视化上的读数

四个读数正好对应轴 ① 加轴 ④，一格不多一格不少：

| 读数 | 取哪个词 |
|---|---|
| 已成 | 状态 `completed` |
| 在手 | 派生 `running` |
| **等待处理** | 状态 `shelved` —— **为 0 时不画这一列；不为 0 时排在最左** |
| 待办 | 状态 `todo`，且不在任何线的 `holding` 里 |

「等待处理」只有出问题的时候才存在，所以它出现的位置就是「先看这里」的位置。

## 六、表外的写法

档案或命令行里出现下面任何一个词，工具直接把那个词指出来，不替你折成表上的词：

```
held  hold  seed  active  paused  completed  failed  running
on_held  node_held  autopilot  engineering
```

其中 `active` / `completed` / `failed` / `running` 各自在自己那个轴上都合法 —— 这里说的是它们站到
别的轴上时不算数。写回档案的只有表上的词。

## 七、自查

改完随便哪一处，这三条 grep 应该都空：

```
grep -rn "'held'\|\"held\"\|_held_ids\|on_held\|node_held" tools/ skills/
grep -rn "status.*\bheld\b" tools/ skills/
grep -rn "'running'" tools/ skills/     # 只许出现在派生标签那一处
```

中英对照（同一张表，给以后的双语 skill 用）：

| English | 中文 | 轴 |
|---|---|---|
| `todo` | 待办 | ① 卡片状态 |
| `shelved` | 等待处理 | ① 卡片状态 |
| `completed` | 已成 / 交付了 | ① 卡片状态、③ 条子判决 |
| `holding` | 持有 | ② 线路 |
| `when_shelved` | 搁置时怎么办 | ② 线路策略 |
| `pending` | 还没结论 | ③ 条子判决 |
| `failed` / `stopped` | 没交付 | ③ 条子判决 |
| `running` | 在手 | ④ 派生标签 |
| `ready` | 可开工 | ④ 派生标签 |
| `revive` | 放回去 | 命令 |