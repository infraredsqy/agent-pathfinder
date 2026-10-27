# 词汇表 · 节点 / cycle / 交接单

> 这张表是唯一权威，不另写第二份。
>
> 三条规矩：
>
> 1. **一个轴一套词。** 同一个词形不许出现在两个轴上。
> 2. **异义必分，同义可留。** 跨轴复用只在两处意思完全相同时才允许 —— 全表只有 `completed` 一个。
> 3. **派生词带 `-ing`，盘上的词不带。** 看到 `running` / `ready` 就知道它是算出来的，不在档案里。

## 一、轴 ① 节点的状态

写在各节点自己的档案里，**只有三个词**。

| 词 | 中文 | 含义 |
|---|---|---|
| `todo` | 待办 | 在局中、未交付。**正被某条线做着的节点，状态仍然是 `todo`** |
| `shelved` | 等待处理 | 出局、等人救。不 ready、不被推荐，下游整棵子树停在它后面 |
| `completed` | 已成 | 已交付，终态 |

动词是**搁置**：把一个节点搁置 → 标 `shelved`；放回去用 `pf dag revive`。

> 「被某条线做着的节点还是 `todo`」是这个轴唯一容易反直觉的地方，也是它必须如此的原因：
> 「谁在做它」记在**持有关系**（轴 ②）里，节点自己的档案只回答「交付了没有」。两件事各占一个字段。

## 二、轴 ② cycle 的状态与持有

写在 cycle 自己的记录里。

| 词 | 中文 | 含义 |
|---|---|---|
| `active` / `inactive` | 在服务 / 不在服务 | 这条线该不该往前推。工具自己**永远只写 `inactive`**，`active` 只在人把线放回服务时写 |
| `holding` | 持有 | 这条线认领了谁。一条线可以同时持有几个节点（宽度是 `parallel`） |
| `policies.when_shelved` | 搁置时怎么办 | 自己的节点没交付（于是被搁置）时：`stop`（缺省，停下等人）还是 `continue`（接着挑下一个）。**两种取值下节点都标 `shelved`** |
| `policies.parallel` | 同时持有几个节点 | 正整数 |
| `policies.spawner` | 会话由谁开 | `dsh` / `zcode` |

这三个名字**不在解析链上**：`policies.` 是它们唯一的落点，写它们、改它们的门是 `pf cycle policy`
（`pf config` 不认这三个名字）。读它们的步骤也只看这条记录 —— 宽的项目层、全局层写这三个名字没人
读，所以工具不许往那儿写（链本身、以及哪些旋钮落在哪些层，见
[`./conduct-state.md`](./conduct-state.md) 的「设置」一节）。

## 三、轴 ③ 交接单上的判决

干活的会话自己写。**判决词永远不出现在节点状态里。**

| 词 | 中文 | 含义 |
|---|---|---|
| `pending` | 还没结论 | 重试留下的记号。**永远当红灯读**：留着它，读者就一直在等 |
| `completed` | 交付了 | |
| `failed` | 没交付 | 需求做不到、或 spec 自相矛盾 |
| `stopped` | 没交付 | 自己主动停手不做的 |

判决 `failed` / `stopped` 落到节点上 = 把这个节点标 `shelved`；判决在交接单上写的还是它自己那个词。

## 四、轴 ④ 派生标签

读的时候算出来，**从不落盘**。`ready` 与 `running` 互斥。

| 词 | 中文 | 判定 |
|---|---|---|
| `running` | 在手 | 节点是 `todo`，**且**它在 cycle 的 `holding` 里 |
| `ready` | 可开工 | 节点是 `todo`、每个依赖都 `completed`、**且没有任何线持有它** |

判定 `ready` 的三条缺一不可。少了第一条，被搁置的节点会装成可开工；少了第三条，别人手里正做的节点会装成可开工。

> 边界：桌面 Automation 那张表的列名里也有一个 `running`（`tools/conduct/spawn/zcode-dispatch.py` 的
> `COLUMNS`）。那是别人 schema 的列，说的是那个进程在跑，与本表无关，别去改它。

## 五、可视化上的读数

四个读数（已成 / 在手 / 等待处理 / 待办）就是轴 ① 加轴 ④ 的四个词，不多不少。会出问题
的只有「等待处理」：**它为 0 时不画这一列，不为 0 时排在最左** —— 这个位置就是「先看这里」。

## 六、表外的写法

写进档案的只有表上的词：命令行里出现表外的写法，工具当场把它指出来，不替你折算。

## 七、译成英文时用的词

中英两份技能是同一份东西的两种写法，词得是一套。三条规矩：

1. **机器词照抄代码里的写法**：上面四节的词、命令与旗标、字段名与键、`.pf/` 路径与文件名、
   `P0`–`P3`、`method: ` 那一行、反模式 id。清单不在这里 —— 在代码与 `--help` 里；拿不准就跑一遍工具，
   看它印什么。
2. **下表照抄，不许另起同义词。** 同一个轴两套词，就是一根轴裂成两根。
3. **表里没有的词，译者按句子选**，但一份技能里用到底 —— 同一份里两个词比选错一个词更坏。

| 中文 | 英文 | 出处 |
|---|---|---|
| 节点 | node | |
| cycle / 线 | cycle / line | |
| 交接单 | the note | 工具散文里也叫 the ticket / the signal，同一个记录 |
| 记录目录 | the ledger | |
| 本地档 / 提交档 | local / commit | `.pf/` 这个记录目录自己的档：项目仓库 track 它就是提交档，没 track 就是本地档 |
| 判决 | the status | 轴 ③ 那个字段 |
| 持有 | hold / holding | 轴 ② |
| 搁置 | shelve / shelved | 轴 ① |
| 交付 | completed | 轴 ① |
| 就绪 | ready | 轴 ④ |
| 终态 | a terminal status | |
| 派发 | dispatch | |
| 会话 | session | |
| 收口 / 收尾 | close out | |
| 正本 | the one source | |
| 出厂默认 | factory default | |
| 维护通道 | maintenance channel | 出厂的两个插件：各占收尾时机、各插一个节点，各量一条债 |
| 插件 | plugin | 一个文件夹：声明 + 实现；文件夹名就是它的身份 |
| 声明 | declaration | 插件文件夹里那份数据：占哪个时机、认哪些名字 |
| 实现 | implementation | 插件文件夹里的代码：时机到了被叫一次 |
| 技能 | skill | 方法名点到的那份 `SKILL.md`；绑插件的技能住在插件的 `skills/<语言>/<方法名>/` 里，随它装、卸 |
| 旋钮 | knob | 一个设置的名字 |
| 旋钮的种类 | the knob's kind | 数值 / 整数 / 词 / 开关（`number` / `integer` / `word` / `switch`） |
| 时机 | timing | 收尾 / 挑选 / 派发（`close` / `pick` / `dispatch`） |
| 动作 | action | 插入 / 拦下（`insert` / `block`） |
| 度量 | metric | 一条债：一个读数、一条线、一枚去重标签、一个方法名 |
| 去重标签 | dedupe tag | 节点上的 tag，回答「这个度量是不是已经有一个在局中的节点」 |
| 方法名 | method name | spec 的 `method: ` 行点名的技能 |
| 过滤集 | filter set | |
| 重试 | retry | |
| 降级标记 | degradation marker | |
| 落定 | settling | |
| 宿主 | the host | |
| 主树 | the main tree | |
| 探针 | probe | |
| 读数 | reading | |
| 判据 | criterion | |
| 完成判定 | completion rule | |
| 阈值 / 线 | threshold / the line | 一个旋钮：读到的数到了它，债就到顶 |
| 范围 | scope | |
| 池子 | the pool | |
| 条目（池子里的） | issue | 错题本里的条目是 entry |
| 债 | debt | |
| 加权和 | weighted sum | |
| 严重度 | severity | 机器词是 `P0`–`P3` |
| 测试档 | test tier | 取值 `full` / `minimal` |
| worktree 档 | worktree mode | 旋钮：一个节点的活在哪棵树里做 |
| 讨论节点 | discussion node | |
| 验证边界 | verification boundary | |
| 错题本 / 反模式 | anti-patterns | 一份东西的两个叫法，英文合成一个 |
| 批（一次交付） | approve / approved | 机器词是动词 `approve` |

代码没定过、但每份技能都会撞上的高频词，本表选一个，别另造：

| 中文 | 英文 |
|---|---|
| 指挥者 | the conductor |
| 用户 / 人工 | the user / a human |
| 方法技能 / 角色卡 | method skill / role card |
| 拍板 | the call |
| 裁定 | the conductor's verdict |
| 落笔 | write it down |
| 这一趟 / 一轮 | a run / a round |
| 快档 / 慢档 | the fast lane / the slow lane |
| 自证 | prove it yourself |
| 移档 | move a test |
| 合回 | merge back |
| 清债 | clearing the debt |
| 人机面 | the pool's human face |
| 骨架 | the skeleton |
| 联调 / 实机测试 | integration / on-device testing |
| 分批 | batch |
| 上限 / 余量 | the cap / headroom |
| 验收 / 总验收 | acceptance / final acceptance |
| 两个入口 / 唯一入口 | the two ways in / the one way in |

每份 `SKILL.md` 开头那三行抬头，七份用同一套写法：

| 中文 | 英文 |
|---|---|
| 谁跑我 | Who runs me |
| 我解决什么 | What I am for |
| 我派不派 subagent | Do I dispatch subagents |