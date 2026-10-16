# conduct 状态与推进契约

> 这份是 conduct 的**设计出处**：状态怎么存、指针怎么走、谁是唯一写者、有哪些不变量。
> [`../skills/pf-conduct/SKILL.md`](../skills/pf-conduct/SKILL.md) 是给 conductor 读的操作手册，从这份派生。
> 派发与鉴权见 [`./conduct-dsh-dispatch.md`](./conduct-dsh-dispatch.md)。
>
> agent 运行时不读这份 —— conductor 一律通过 `pf`
> 读写状态，从不直接碰 JSON。

## 0. 管线是什么

一条 **cycle** 是一条执行线。它握着 DAG 节点往前走（默认一次一格，`parallel` 策略可以开几格）：挑一个交给一个会话，会话报回来之后记下这一步，再挑下一个 —— 或者停。

```
cycle -> node -> （会话自己的备料/执行/复审/修复）-> 下一个节点 或 停
```

节点内部怎么分工归那个会话自己管：那些是它指挥的 subagent，管线看不见，也不写进 `.pf/`。管线留下的只有三样 —— cycle 的身份、它此刻拿着的节点、它走过的路径。

pf-route 不在管线里 —— 它回到人面前，负责把想法变成图。

## 1. 三个角色

| 角色 | 是谁 | 手里那点权 |
|---|---|---|
| 干活 | `pf-conduct` | 只干活、只交事实。**能单独执行**；内部自己指挥 subagent |
| 记账 | `state-manage.py` / `conduct_pipeline.py` | **唯一能写状态的一方**。算得出来的判断都在这里算 |
| 判断 | 人 | 只剩算不出来的：读自己的话、决定要不要现在停（`instruction` / `--stop` / `--node`）|

**能写成规则的判断，不该再花一次会话重新做一遍。**

## 2. 一条 cycle 的形状

```
pf-conduct ──► pf-conduct ──► pf-conduct ──► …
       └── 同一个会话：handoff.py 写条子 → next --after 结算并接棒 ──┘
```

- 一个节点一次只归一条 cycle：cycle 把它写进自己的 `holding` 就是占了位，别的线挑不到它；同时握几格由这条线的 `parallel` 策略定（缺省 1）。
- 一个项目可以并行两条 cycle（各挑各的 tag 范围），但裸的 `next` 就分不出该跟哪条，必须 `--cycle` 点名（口径见「命令面」）。
- 一条线在不在服务中写在 cycle 的 `status` 上（`active` / `inactive`）：不在服务中的线不被驱动，但它钉着的节点仍然收得掉（见「两道门」）。
- 管线只消费图，**永不造节点**。

## 3. 状态文件

```
.pf/
  state.json                    cycles[]：id / title / status / holding[] / path[] /
                                tags(可选) / policies(可选) / created_at / updated_at
  signals/<cycle>/<node>.json   一个节点一张条子
  instructions.json             只许追加的指令记录
  meta/
    launch/<cycle>/<node>/prompt.txt  派发用的 prompt（派出去之后删掉这个目录）
    CONDUCT-CALLBACK-FAILED-<cycle>.json  链条断了的显眼标记
```

**一条 cycle 只有这一份记录**：身份、此刻拿着的节点、走过的路径全在 `state.json` 里，没有第二个指针文件要跟它同步。手改它会和工具分叉，下一个 conductor 跟着错 —— 所以状态一律过 `state-manage.py`。

### 3.1 cycle 记录

记录字段：`id` / `title` / `status`（`active | inactive`）/ `holding[]` / `path[]` / `tags`（这条线的选材范围，它一生里每一次挑选都用这一份；词表与判据见 [`./conduct-node-tags.md`](./conduct-node-tags.md)）/ `policies`（可选）/ `created_at` / `updated_at`。

- `holding` 是这条线此刻握着的那组节点，每项 `{"id": "<uuid>", "since": "<iso>"}`、按认领先后排，可为空；上限是这条线的 `parallel` 策略（缺省 1）。**写进去就是认领**：一个节点只许被一条线握着，别的线挑不到它。节点一收尾就从 `holding` 里去掉，线里别的节点不动。
- `path` 是走过的节点，**每收一次尾追加一步**，按走的顺序：同一个节点重做过就是那两步（先后两个判决各占一行），读它读的是**最新一步**。每步的 `result` 记的是**条子上的判决**（`completed` / `failed` / `stopped`）；节点状态走另一套词。
- `policies` 是这条线自己的策略（可选，不给就不出现这个键）。四样东西住同一格：`when_shelved` —— 节点收尾却没交付（它于是变成 `shelved`）时，这条线是停下等人（`stop`）还是接着挑下一个（`continue`）；缺省 `stop`（缺省值落在保守那一边），`continue` 也照样把节点标 `shelved`（它的下游照旧停着）。`parallel` —— 这条线同时能握几个节点：正整数，缺省 1，`off` 取消、非正整数拒收。两条维护阈值 —— 数值，`off` 取消（见「维护通道」）。`spawner` —— 这条线的会话由哪份 spawner 开：`dsh` / `zcode`，`off` 清除；没配的线落在选路的下一级（`--spawner` > 线策略 > 环境变量 > 宿主自认 > `dsh`，见派发文档），行为与这个键存在之前一字不变。都走 `pf cycle policy <id> <key> <value>`，`cycle current` 的 `Policy:` 行把生效的那一份回显出来。
- `status` 只有两个词：`active` = 这条线该往前推，`inactive` = 不要推。**停下来的原因不写在状态里**（它写在停手那一刻的 `reason` 里）。图里一时没有就绪节点不等于这条线死了：图会长出新的活，要不要接着推由人说了算 —— 人不说，它就不动。

### 3.2 条子（signal）

条子落在 `.pf/signals/<cycle>/<node>.json`——按 cycle + 节点取值：命令里就这两样，路径里也是这两样。字段：`cycle` / `node` / `status` / `summary`（一行总结）/ `outputs` / `timestamp`。
- `completed` / `failed` / `stopped` 是终态，也是**条子上的判决**：`completed` = 会话说交付了，`failed` / `stopped` = 说没交付。判决与图上的节点状态是两套词：判决 `failed` / `stopped` ⇒ 收尾把那个节点标成 `shelved`。条子在盘上 = 这个节点的活做完了，只剩收尾。`running` 由 `cycle retry` 为一次重试写下，**永远当红灯读**；`cycle retry --drop` 则是连条子都不留 —— 重试真正起步的那一步（红灯留着，读者就永远在等）。
- `outputs` 是干活那个会话自己的话。`deliverable` / `acceptance` / `touched` / `reading` 四键是 `pf handoff --form <cycle>` 印出来的条子骨架点名要的，人回头看这个节点做了什么就看它；多写的键**原样存下，工具不读也不查**。**一处例外**：`reading` 键会被收尾那一步读走，用来判维护阈值（见「维护通道」）——没有它，那条通道对这个节点就是不生效。这个键里只剩**这条线配了阈值、又只有干活的会话测得到**的那一个数：`test_seconds`（这条线配了 `test_seconds_limit` 才报）；池子加权和由 issue 工具自己算（`pf issue weight`），**不进条子**。没测就把整个键删掉：填 0 会让一条真实的债看起来已经还了。
- 节点的报告按约定落在 `.pf/nodes/<节点 uuid>/` 的根层（`spec.md` / `report.md` / `review-{N}.md` / `fix-{N}.md` / `run.md`）；这个节点的临时物落 `.pf/nodes/<节点 uuid>/` 底下的子目录（`temp/` 等）。**这只是一条约定**：管线不读、也不判这些文件，工作会话怎么分自己的活它一概不管。
- 条子写不进去时，`handoff.py` 落 `CONDUCT-CALLBACK-FAILED-<cycle>.json` 并以非零码退出。

### 3.3 派发用的 prompt

`.pf/meta/launch/<cycle>/<node_id>/prompt.txt`。**一块：第一行是调用行，后面什么都没有。**

1. 调用行（第一行，硬约束）：`/pf-conduct <node_id> [pipeline]`。`[pipeline]` 表示这格是管线派进来的。节点的 `mode` 不在行里——它在节点自己的记录里（`pf dag info` 读它），会话照它决定全程问不问人；收尾停不停由工具在结算时执行。

节点标题、节点 `spec.md` 的路径、收尾那两条命令都不写进 prompt —— 会话自己用 `info` / `open` 读，条子骨架由 `pf handoff --form <cycle>` 按这条线的策略现印。

**prompt 里没有用户的指令**（见「指令怎么到会话手里」），也没有对这个节点内部怎么分工的任何要求。会话派出去之后这个目录只剩一枚 `started` 记号（prompt 被会话拿走、claim 撤掉）：哪一格的会话已经开过了，就看它 —— 派发与 `--spawn` 见到它直接跳过，重开那一格走 `cycle retry --drop`。派失败时不删那份 prompt —— 它正是人要手动重试的东西。

### 3.4 指令账（instructions.json）

一条指令：`id` / `text`（用户原话）/ `scope` / `target` / `state` / `cycle` 与 `node` 绑定 / `said_by`、`at` 与 resolve 标记。

- **两个维度**：`scope`（这条指令从哪儿读得到）× `target`（这一栏的取值名说的是送达范围：`stage` = 每个干活会话都该看到，`boundary` = 只在收尾那一步读，喂给工具的停手判据）。
- **`scope` 记的是这条指令的位置**：`global` 任何位置都读得到；`cycle` 只在记它的那条 cycle 还 `active` 时读得到；`node` 只在记它的那个节点还没 `completed` 时读得到。`cycle` / `node` 两个绑定由 `instruction add` 从当前状态自己填，填不出来时用 `--cycle <id>` / `--node <id>` 点名（两条 active cycle 并排就是这种时候）；两样都没有的 `cycle` / `node` 条目**拒收** —— 没有绑定的条目会被读成人人可见、永不失效，那正是 `scope` 要避开的结果。
- **看得见与否现算，不落笔**：`scope` + 绑定的 cycle/节点 + DAG 当前状态，是纯推导 —— 出了范围就是读不到，账上那份还是 `live`（`live` 始终只有一条意思：声明过、没被撤）。所以没有「范围一结束就把它记成 done」的步骤：那等于把「得有人在那一刻跑一下」重新塞回来，而它买到的东西是零。
- **缺 `scope` 的条目读成 `global`**：读不出来的条目绝不能读成「这里没有约束」。
- **默认取安全方向：`global` + `stage`。** 错判的代价不对称 —— 把一条真约束当窄的，它会静悄悄蒸发；把「我要吃饭了」当 global，它下一次还会拦你一下，当场纠正。**错误要往「吵」的方向犯。**
- **状态只决定「要不要影响下一步」，不决定「要不要留着」。** 兑现的、被覆盖的全都留着当解释：「管线为什么在这儿停了？」—— 因为那时候你说了一句要吃饭。
- `instruction resolve` 只剩两件事：人撤约束（`withdrawn`），和给 `global` 条目记一笔（`applied` = 这一步已兑现 / `n/a` = 本项目用不到）—— 两种都让它留在账上继续生效。`cycle` / `node` 条目不需要任何 resolve，范围一过它自己就不念了；`done` / `superseded` 只用来手工整理账。**`global` 的条目永不被干活会话标 `done`。**
- `stop_after_nodes` 是可选的机器字段：用户说「跑够三个就停」时填上，工具自己就算得出该停。
- 维护通道的两条线是**这条线自己的策略**（`cycle.policies`，与 `tags` 同类）：`cycle policy <id> test_seconds_limit 15` 写、`off` 取消，`cycle current` 的 `Policy:` 行把它打出来（见「维护通道」）。一条线不配时那条通道对它整个不生效。

### 3.5 指令怎么到会话手里：现读

**派发 prompt 里没有指令**，一行都没有。指令由每个会话自己读：

```pwsh
pf instruction list --project <项目根>
```

- **谁读、什么时候读**：conductor 和它派出去的每个 subagent，**起手第一批命令里**就这一条。读完再动手。
- **读到的是「从我现在站的位置看得见什么」**：`global` 一律给；`cycle` 条目只在它是当前这条线时给；`node` 条目只在它是当前这个节点时给。位置读不出来时退回按绑定对象的状态判；仍然判不出来就给。**方向取吵**：视图窄一寸，那条约束对读它的会话就等于不存在。
- **两条 active cycle 并排时**用 `--node <你手上的节点 id>` 说一句你站在哪条线上。不说，`cycle` 条目两边都不念，默认视图补一句解套的话 —— 沉默必须能被读成「说一句你在哪条线上」。
- **`--scope global|cycle|node` / `--all` / `--live`** 是收窄用的：它们给的是账目视图（带 id 和标记），供人翻账、排查；默认那条命令给的是「你现在该看什么」。`--json` 是同一份答案的机器可读形式，空的时候是 `[]`。
- **失败的形状**：忘跑这一条，状态里**不会**留下任何痕迹。唯一看得见的地方是那个会话自己的 transcript。兜底只有一条，也是最省事的一条 —— **起手就跑**。
- **为什么不做注入**：prompt 里拼一份，就是同一件事的第二个副本，而且是更旧的那一份（拼的时候读一次，跑到中间用户又加了一条，prompt 里那份不会变）；它还会让约束**看起来**已经送到，而会话其实一个字没读。现读只有一个读者、一个时刻、一份答案。

### 3.6 记录的落点与合并

**位置就是分类**：`.pf/` 底下那几份记录（图 / cycle / 指令 / 条子 / 节点目录）进版本控制，工具自用的 `meta/`、`worktrees/`、`nodes/<id>/temp/` 由 `.pf/.gitignore` 挡在外面。分类看它落在哪个子目录下面，不看文件名。

| 落点 | 内容 |
|---|---|
| `.pf/ideas.json` | 图 |
| `.pf/state.json` | cycle 记录 |
| `.pf/instructions.json` | 指令记录 |
| `.pf/signals/<cycle>/<node>.json` | 条子 |
| `.pf/nodes/<节点 uuid>/` | 节点**根层**的报告群：`spec.md` / `report.md` / `review-{N}.md` / `fix-{N}.md` / `run.md` |
| `.pf/nodes/<节点 uuid>/<子目录>/` | 这个节点的生成史：`temp/`、`phaseN/`、`materials/` 等 |
| `.pf/meta/launch/`、`.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json` | 派发的临时物与断链标记 |
| `.pf/meta/sessions/<节点 uuid>.json` | 会话便条：这一格的讨论场与执行场 |
| `.pf/meta/ledger.lock` | 三个工具共用的那把写锁 |

- **会话 id 只在本机有意义，所以它不落在记录里**：一场对话活在开它的那台机器上，写进共享 JSON 就是一个在别的机器上必然打不开的死值，而且与「那场被删了」分不出来。便条因此住 `meta/sessions/`，两份清单按角色分开写：`discussion`（这一格是在哪场对话里讨论出来的，`dag-manage add` 写）与 `run`（哪几场会话跑过它，派发时由管线写，一次一场）。每条带 `host`（`dsh` / `zcode`：ZCode 那类会话 DSH 面板打不开）与 `at`；从老记录导进来的带 `source: "ledger"`，因为它可能指着写记录那台机器上的会话。形状与理由的正本在 `tools/session-note.py`。
- **老记录里的 `dsh_session` 键原样留着、没人再读它**：`pf session import` 把它（连同老 spec 头部的 `> discussion:`）折进便条一次，记录一个字节都不改。

- 记录只有一个根：`.pf/`，`meta/` 与 `nodes/` 都从它算出来（带 `--project` 时按项目根算）。两处封装各持一份（`conduct_state/store.py` 与 `conduct_pipeline_impl/store.py`），测试用 `bind_paths(ledger)` 注入这一个根。
- **认领（`holding`）留在 `.pf/state.json`**：它既是这条线此刻握着的节点组，也是跨机的认领标记 —— 收尾那条 `next --after` 会校验它，另一台机器 pull 之后看得见哪些节点已经被领走。
- **三份记录 JSON 要合并**：两台机器／多人在同一分支各自提交。驱动名 `merge.pf`，实现 `tools/conduct/ledger-merge.py`，按条目（节点 uuid / cycle id / 指令 id）做三方合并。`.pf/.gitattributes` 给这三份声明 `merge=pf`（模式相对 `.pf/`，所以提交档与自成仓库的本地档共用同一份）；**注册一次就够，`init` 已经顺手做了**：

  ```pwsh
  pf register            # 换机器、搬动 checkout 之后重跑一次
  ```

  它登记的命令是 `pf merge-ledger %O %A %B %P`——不带绝对路径，所以 checkout 搬到哪儿、换哪台机器，项目里已经登记的这一行都照样有效。`pf` 还不在 PATH 上时先登记绝对形式，补好 PATH 再跑一次 `pf register` 就会换过来。手写等价于：

  ```pwsh
  git config merge.pf.driver "pf merge-ledger %O %A %B %P"
  ```

  git 交给驱动的前三个参数是它自己的临时文件，仓库内路径只在 `%P` 里——驱动按它分派。

- **驱动怎么裁**：条目只一侧有 → 取该侧；图里的节点删除会传播（base 有、一侧删了、另一侧没动 → 删；另一侧改过 → 举旗）。同一条目两侧都改 → 图的状态按偏序取高（`todo < shelved < completed`）、依赖两侧都只加边时取并集、其余字段取 `updated_at` 较新的一侧；cycle 的 `path` 与指令账的 `applied` 按追加序取并集；认领（`holding`）按普通字段裁 —— 只一侧改过取该侧，两侧都改举旗；cycle 拼写与状态别名走工具自己的表。
- **举旗的情形**（驱动一个字都不写，git 把那个文件标冲突、工作区留 ours，交给人）：一边删边、另一边还在改这条边表；同一个 id 两侧各新增一份而内容不同；同一字段两侧改成不同的值；指令的解除两侧都没有 `resolved_at` 可比；这次合并新造出悬挂依赖（节点被删、另一边新增的边指着它）。
- **rebase 下 ours/theirs 是反的**：`git pull --rebase` 撞冲突时，驱动报的 `ours=` 是远端（你 rebase 到的那侧）、`theirs=` 是正在被重放的本地提交，别按 merge 的直觉读。脱身：改好账本文件 → `git add` → `git rebase --continue`；不想继续就 `git rebase --abort`。

- **节奏**：粒度到节点——每个节点**开始时 pull**、收尾提交后 push，**推完才派发**。

## 4. 动作表（`next` 返回的 action 全集）

| action | 什么时候 | 谁接 |
|---|---|---|
| `dispatch` | 手里的节点已钉住、prompt 已落盘 | `--spawn` 已经派出去；或收尾推完之后跑 `pf dispatch <cycle>` |
| `open_node` | 工具自己在钉下一个（或几个）节点 | 到不了调用方：命令跑完接着返回那个 `dispatch` |
| `maintenance_due` | 维护度量越了线，工具刚往图里插了一个维护节点并把它钉住 | 到不了调用方：走法与 `dispatch` 一模一样（prompt 已落盘、提交推送后照样 `dispatch <cycle>`），`apply` 跑完接着返回新节点的那个 `dispatch` |
| `stop_pipeline` | 没活了 / 有人叫停 / 这条线不在服务中 / 这一格收完而手里还有别的节点在飞（线照走，停的是本会话）| 收摊，把 `reason`（`attention` 有内容就一起念）告诉用户 |
| `wait` | 还没到能动手的时候 | 读 `note` / `close_with`，**绝不轮询** |
| `ask_user` | 管线自己判不了的两件事：点名的线不存在；线在、此刻不在服务中 | 问人。不在服务那条带着 `hint` —— 把它放回服务的那条命令（渲染和 `--json` 都打）|
| `no_cycle` | **一条 cycle 都没有** | 开 cycle 要人的意图 |
| `fail_hard` | 状态坏了 / 链走不下去 | 把 `reason` 和 `options` 摆给用户 |

动作全集以 `conduct_pipeline.ACTIONS` 为准（`pf next --help` 的尾巴打着它，测试钉住它的键集）。

`dispatch` 的动作带一个 `nodes` 数组，每项是一格的一份派发：`node_id` / `node_title` / `mode` / `prompt` / `prompt_text`。单节点的线是恰好一项（`node_id` / `prompt` / `prompt_text` 这几个单数字段也照旧回填），多节点的线一项一格，各有自己的 prompt 文件与自己的会话。

`open_node` 与 `maintenance_due` 是工具自己的两步：`apply()` 把它们跑完就**重规划**，调用方拿到的是它们各自那一格 `dispatch`。两者都到不了调用方手里。`maintenance_due` 是维护通道那一格 —— prompt 写好、可以派发，被钉住的是刚插进来的维护节点。

`no_cycle` 严格等于「一条 cycle 都没有」：线都在、只是没一条在服务中时报的是 `ask_user`（点出这些线，附上把线放回服务的那条命令）。把这两种读成同一种，读的人会去开一条新的，而真正该做的是把停住的那条放回服务。

**裸 `next` 撞上多条 `active` 线按调用错误处理**：工具按 exit 2 拒掉，报错里列出候选线并指 `--cycle` —— 「跟哪条线」属于调用方式的事，`ask_user` 只留给管线真判不了的状态。

三种 `wait`，看 `note`：

- **条子已在盘上、节点还没收尾**：`note` 和 `close_with` 给出收尾那条命令（`pf next --after <node_id> --json`），派发排在提交与推送之后（`dispatch <cycle>`）。手里还有别的节点时，`note` 把它们点名列出，各自的条子到了照常收。这是稳定状态，问多少遍答案一样，读它不消费任何东西。
- **条子还是 `running`**：`note` 里写着解套三条（`cycle retry --drop`，然后 `cycle hold`，然后 `next --spawn`）；手里还有别的节点时一并列出（它们照跑）。
- **手里的每一格都开过会话了**（`started` 记号在盘上，条子还没有）：`note` 说清在等它们的条子，派发名单里没有它们，`dispatch <cycle>` 也跳过它们 —— 一格都剩不下时它拒绝并把重开那一格的路念出来。

**`commands` 不在任何返回里**：`apply()` 跑完就把这个字段摘掉，调用方没有东西可以重复执行。`next --plan` 是只读预览：一个字节都不写，本来还要机械记账的动作会带一句 `settling: true`，预览不可执行。

## 5. 收尾那一次调用

收尾被一条约束钉死：**派发是最后一件事**。下一个会话跟收尾会话在同一棵工作树上开机，它一上来就 pull —— 树没提交干净，它要么 pull 不动、要么读到半截。顺序因此是：关节点 → 提交 → 推 → 派发。

干活那个会话按顺序跑两条命令（命令原文在 `pf-conduct` 的 SKILL.md「收尾」那节；条子骨架由 `pf handoff --form <cycle>` 现印）：

```pwsh
pf handoff <cycle> <node> completed -s "<一行总结>" -o '{…}'
pf next --after <node_id> --json
```

**第一条只写条子**：报告与收尾分成两条命令是有意的 —— 会话报完还能想一想，中间那个状态（条子在盘上、节点还没收）本身是稳定状态，链断在半路时看得见。这条命令**不开任何会话，也不替谁收尾**；节点已经收过尾、或这个节点不在它手里时，它一个字节都不写、落 `CONDUCT-CALLBACK-FAILED-<cycle>.json` 并非零退出。

**第二条关掉这个节点**，一件事一气做完：读这个节点的条子取结论 → 记下这一步、把 DAG 节点标掉 → 把这一格空出的位置补满（`parallel` 减去还握着的格数），没得补就停下（**手里还握着别的节点时不停这条线**：那几格还在跑，接力棒在它们自己的收尾上）→ 把手里的每个节点各写一份 prompt 到 `.pf/meta/launch/<cycle>/<node_id>/prompt.txt`。它结算的只是这个节点，线里别的节点不动。会话只读结论，不读中间状态。它**不拉会话**：`--spawn` 是「顺手派出去」的旧写法（仍在，一次到位），收尾那条链走的是「先提交、推完再派发」。

**推完才派发**：`pf dispatch <cycle>` 读完上面那些 prompt 文件，把这条线此刻握着的每个还没交过条子、也还没开过会话的节点各开成一个会话（每格一份 prompt；已经开过的看 `started` 记号，跳过它 —— 同一个节点上第二个会话收不回来。一格都剩不下时它报错，并把重开那一格的那条命令念出来）；`action` 是 `stop_pipeline` 时不调它。开关会话这件事由 `dsh-spawn.ps1` 这个原始件干，`dispatch` 负责把参数与时机对准。

### 5.1 两道门

`--after` 是会话收自己那个节点的门，别的 `next` 是驱动下一步的门 —— 两道门对「线在不在服务中」的态度不一样：

- **收尾那道门（`next --after <node>`）不看这条线的状态。** 节点自己就说明了它属于哪条线（按 `holding` 反查；两条线都拿着它才算歧义），而一条停下来的线如果连自己钉着的节点都收不掉，那个节点就永远钉在那儿了。
- **收尾那道门认 worktree 的 FORK 记录**：fork 档下每个节点在自己的 worktree 里做、收尾合回主树。`.pf/meta/FORK-<短id>.json` 还在（说明这个节点走过 fork），worktree 目录与出口分支就必须都已删除——合回清理没做完就拒绝结算，什么都没写；两样都清了才放行并摘掉记录。没有记录的节点原样通过，连 git 都不问。
- **worktree 档位**：项目文档一行声明（`worktree 档：off / fork / reuse`），没写＝git 项目 `fork`、非 git 项目 `off`。`off` 不 fork，主树直做——没有 FORK 记录，上面的门原样通过。`reuse` 未实现，方向：节点落点用预先开好的 worktree 或独立 clone（独立 clone 合回多一步 fetch），发目录前先把池目录快进到主树现头（同库增量，开销小）；收尾只标记空闲、不删，收尾门判据从「删干净」改成「已合回且空闲」；池目录的占用是原子 claim（per-dir 占位文件，并发领取只有一个成功），收尾释放，死领用扫清；池子路径每台机器 clone 后在 `.pf/` 注册一次、不进项目文档，没注册就停、不退回 `fork`。
- **线上不是 `active` 时，这道门只做记账**：记录这一步、标了 DAG 节点，不挑下一个、不派发，**线的状态一个字都不写**（它保持人给它的那个）。`reason` 把这件事说清楚，末尾给出放回服务的那条命令 `pf cycle status <cycle> active`。`--continue` 和 `--node` 都撬不动它：点名的那个节点不会被打开，`reason` 里点名说出来。
- **别的 `next` 一概不驱动一条不是 `active` 的线**：不挑人、不派发，回 `ask_user`，`hint` 里是那条把它放回服务的命令（渲染和 `--json` 都打）。
- **裸 `next` 时线都在、只是没一条在服务中**：回 `ask_user`，点名这些线并附上那条把它们放回服务的命令；`no_cycle` 留给「一条 cycle 都没有」那种情形（见「动作表」）。

### 5.2 结论怎么读

- **结论从条子读**：`completed` = 会话说交付了，`failed` / `stopped` = 说没交付。工具照这条结论把 DAG 节点标掉，不再追问第二个会话 —— **做没做成以盘上这条记录为准**。
- **判决留在条子上，图上只放节点状态**：条子 `failed` / `stopped` ⇒ 这个节点标 `shelved`（出局等人，它的下游整棵子树停在那里等人），路径那一格照旧写条子用过的那个词。`failed` 只活在条子上，图上没有这个状态。
- **没交付之后这条线走不走，只看它自己的 `when_shelved` 策略**（`policies.when_shelved`，缺省 `stop`，见「cycle 记录」）：`stop` = 标 `shelved` 然后停手等人；`continue` = 标 `shelved`，然后照一个已交付的节点那样接着走（下面那条停手判据 + 挑下一个）。**两种取值下节点都标 `shelved`**，`continue` 说的是「这条线不等人」。
- **挑下一个节点**：图里分数最高的就绪节点（补格时按同一排序往下取满 `parallel`）。排序只有 `dag-manage` 一处实现（工具调 `pf dag list -n 0` 拿全量排序，自己滤出就绪集合，**不重写打分**）。收尾时这个节点**读作已完成**：它的 `pf dag status` 命令在这之后才跑，把它当已完成才看得见它下游的节点；`list --ready` 是给人浏览的视图，没法告诉它「把这一个节点当已完成」，所以这一步不外包给那个过滤器。别的 cycle 拿着的节点挑不到；被这条线过滤集挡住的节点也挑不到。
- **`--node <id>` 是机主点名**：就绪的照走（名字落在过滤集外用也在 `note` 里说清），不就绪 / 不存在就停下并说明。

### 5.3 停手判据：只看节点自己的 mode

**这条线跑不跑下去，只看这个节点自己的 `mode`，三档各一个答案。** `auto` 与 `manual` 的节点收尾后都接着走 —— `auto` 没有人闸；`manual` 的人闸在格子的会话里：整格就是与用户来回推进，标成完成即是用户放行（「你继续吧」），收尾那一拍不再设第二道闸。`checkpoint` 的人闸就在收尾这一拍：整轮循环跑完之后停一次，等机主说「继续」。

`checkpoint` 停下来时 `reason` 里带着接着走的那两条命令（`cycle status <cycle> active` 然后 `next --spawn`）：停手照旧把线转成 `inactive`，没有那一条解不开。

`boundary` 指令里的 `stop_after_nodes` 上限只能**收紧**这条规则：`--continue` 放宽不了它，`--stop` 只是更早停。

### 5.4 停下来怎么落地

**停手就是一句话：`cycle status <cycle> inactive`。**（手里还握着别的节点时不写这一句 —— 那条线没停。）一条写死的命令，没有 paused / completed 之分 —— 那条岔路问的是「图上还有没有活」，而图会长大、活会变多，一条停下来的线该不该接着跑是人说了算的事。

**工具永远不写 `active`**：cycle 建出来时是 `active`（`cycle create` / `cycle start`），之后只有人的 `cycle status <id> active` 能让它回到服务中。收尾那道门在线上不是 `active` 时连这一条都不跑（见「两道门」）。

**`--spawn`** 让这次调用一条到底：结算完是 `dispatch` 就顺手把下一个节点的会话拉起来。

## 6. 不变量

每条都花过一个 bug：

1. 一条 cycle 同时握的节点数上限是它自己的 `parallel` 策略（正整数，缺省 1）。**认领跨线唯一**：一个节点只许被一条线握着，别的线挑不到它 —— 往同一条线上再添一位不是抢，同一个 id 重 pin 也放行。
2. 终态条子就是「这个节点做完了」的全部；`running` 的条子永远当红灯读。重试之前先 `cycle retry` 把条子换掉。
3. 活做完了但还没收尾的节点是一个**稳定状态**：`next` 被问多少遍都答同一个 `wait` + 同一条收尾命令，不写任何东西。
4. 收一个节点 ≠ 跑完这条 cycle：收尾记下这一步、标掉 DAG 节点，而做这件事的是干活会话自己的 `next --after`。
5. cycle 是关于哪个节点的，在它被钉住时就记下了，不是等报上来 —— 所以跑到一半改 DAG 不会把这一步带偏。
6. `commands` 在工具里跑掉，并从返回的动作里摘掉：调用方手里没有可以再跑一遍的一串命令。
7. `apply()` 只有真的动了指针 / 条子 / 路径 / cycle 状态才报成功，否则抛错。
8. 每个子进程都有超时上限（`CMD_TIMEOUT`）—— 挂住的子进程必须响，不能挂一整夜。
9. 指令只能**收紧**停的规则，不能放宽：`checkpoint` 的节点，无论指令说什么、无论收尾调用要什么，都在这个节点结束时停下；`auto` 与 `manual` 的节点能接着走 —— `manual` 的放行发生在格子的对话里（用户明确说「标成完成，继续」）。节点自己的 mode 是这条判断唯一的一处。
10. 节点的结论是**粗的**，而且由干活那个会话自己写：`completed` 是「交付了」，`failed` / `stopped` 是「没交付」。这三个词是**条子上的判决**；图上的节点只有 `todo` / `shelved` / `completed`。
11. **cycle 的过滤集只决定「挑谁」。** 正在跑的节点照跑完；被挡住的节点不改状态、不标 `shelved`、不动依赖边（挡住跟依赖没满足是两回事）。排序仍然只有 `dag-manage` 一处实现，`conduct_pipeline` 只按过滤集删行。
12. **派生值不落盘。** `ready` = 节点自己是 `todo`、每个依赖都 `completed`、且没有任何 cycle 的 `holding` 点着它；`running` = 节点是 `todo`、且它在某条 cycle 的 `holding` 里。两个都是读的时候算出来的标签。账上只有 `todo` / `shelved` / `completed` 三个词：`dag-manage` 只读自己那张表（它读不到 `state.json`），这个 join 长在读得懂两份记录的读者身上（`conduct_state` 的 `cycle current`、以及以后的可视化）。
13. **认领归线，节点状态只放节点自己的事。** 谁在跑这个节点写在 cycle 的 `holding` 上，节点自己照旧是 `todo`；条子的判决是另一套词，`failed` / `stopped` 落到图上是 `shelved`。两件事各占一个字段，谁也不解释谁。

## 7. 命令面

命令与参数以 `pf --help` / `pf dag --help` 为准，这里只留四句口径：

- **唯一入口是 `next`**：裸 `next` 跟唯一 `active` 的线；多条并排时必须 `--cycle <id>` 点名，不点就拒收（exit 2，报错列出候选线）。`--plan` 只读，一个字节都不写；`--after <node>` 是干活会话收自己那个节点的门（见「收尾那一次调用」）；`--spawn` 在结果是 `dispatch` 时顺手把会话拉起来。
- **收尾是两条命令**（原文在 `pf-conduct` 的 SKILL.md「收尾」那节）：`handoff.py <cycle> <node> completed -s "<一行总结>" -o '{…}'` 只写条子（`-o` 的骨架由 `pf handoff --form <cycle>` 现印）；`pf next --after <node_id> --json` 结算、补格、写下一格的 prompt。提交推送之后才 `dispatch <cycle>`——它把这条线握着的每一格各开一个会话。
- **worker 会话不读第一条命令的输出**：让会话解读工具的回执，它会自己把管线再走一遍。
- **会话便条只读着用**：`pf session show <node>`（一条便条，`--json` 给机器）与 `pf session list --json`（本机全部便条，按节点 id 索引）是它唯一的读法，`pf dag info` 的同名字段读的是同一处；便条审的是本机（别的机器上那场对话不在，读到空是正常读数），`pf session import [--dry-run]` 是把老记录折进便条的一次性动作，每台机器各跑一次，它不写记录。

## 8. 恢复

**重跑一个节点**：条子先回卷，再重新钉住，再推。没交付的节点落在 `shelved`（出局等人），它不再就绪、也不让下游就绪，所以先把它放回 `todo`：

```pwsh
pf dag revive <node_id>
pf cycle retry <cycle> <node> --drop --summary "重试"
pf cycle hold <cycle> <node>
pf next --cycle <cycle> --spawn --json
```

- **重做走的就是这条路，判决一步一步追加**：`path` 上原来那步 `failed` 留着，这一次的判决接在它后面 —— `pf handoff` 的门槛是「这条线此刻握着这个节点」，它不看历史（`cycle show` 读到的是最新一步）。
- `cycle retry --drop` 把条子拿掉：不拿掉它，下一个读者还当这个节点做完了 —— 而重写出来的 `running` 自己也是红灯，留着 `next` 就永远答 `wait`。它同时抹掉那一格的 `started` 记号（会话已经挂了，那一格要重开；记号留着，派发会把这一格跳过）。
- 不在服务中的线（`inactive`）：收尾那道门照旧结算它自己钉着的节点（只做记账，不写线的状态），要接着挑下一个节点就先 `cycle status <id> active`。
- 条子还是 `running` 那种卡住，解套就是上面那三条（`cycle retry --drop` + `cycle hold` + `next --spawn`）：工具的 `wait` 会把它们原文念给你。

**链条断了**：`.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json` 存在 = 这条链现在是断的。它由 `handoff.py`（条子写不进去 / 管线拒收条子 / 管线推不动）和 spawner 写下（DSH：`dsh-spawn.ps1` 的 cookie 或 API 调用三次重试用尽；ZCode：`zcode-spawn.ps1` 排不进队列，或等不到会话建出来），字段是 `cycle_id` / `phase` / `attempts` / `reason` / `workspace` / `detected_at` / `note`，外加一个指认"断在哪条通路"的定位字段（DSH 是 `api_base`，ZCode 是 `db`）。人工接管：先看 `reason`，按 `next --cycle <id> --spawn` 把这条线捞回来，确认无碍后删掉本文件（spawn 成功时工具自己也会清掉上一次留下的标记）。

**出局等人的节点（`shelved`）**：`pf dag revive <node_id>` 放回 `todo`（`shelved` → `todo`）。stop 动作的 `attention` 里 `kind: node_shelved` 会点名它并给出这条命令；`pf dag status <id> todo` 是同一个动作的另一种写法。

**仍然没有 watchdog**：子会话自己挂死（不写条子）时没有任何自动检测，链条会静静停在那个节点上等人。这是已知缺口，人工 `/pf-conduct` 能捞回来。

## 9. 开发与验证

```pwsh
cd $env:USERPROFILE\agent-pathfinder\tools
python -m unittest discover -s conduct/tests -t .        # 或者直接跑那个文件（自带并行 runner）
```

**单条用例必须在 5 秒内跑完**，超 10 秒算缺陷（用户明确要求）；判红的口径落在单条用例上，整套带并行 runner 跑，全量套件的实际耗时照常报出来。慢的原因几乎总是子进程启动（Windows 上起一个 Python 解释器要 150~250ms，几百次就是几十秒），所以：

- 用例之间各自一个临时项目，按 CPU 核数并行跑 —— 这些用例的时间几乎全花在等子进程上。
- **能进程内调用就进程内调用**：`conduct_pipeline.py` 用 importlib 按路径加载后直接 `apply()` / `record()`，只有「被测对象就是 CLI」的用例才真的调 `state-manage.py`。
- 测试初始化能用写文件搞定的（DAG 的 `ideas.json`、`state.json` 的骨架）就不起子进程。

## 10. 维护通道

维护是**项目级**的债：测试套件的墙钟、issue 池的积压。它们不归某一条线，也没有边——所以图的挑选永远会把它们压到底。这条通道给它们一个入口：**在收尾那一拍，按这条线自己配的线把维护节点插进图里**。

**两个度量，两个来源。** 测试时长由干活会话在收尾报进条子（`reading` 键），池子加权和由 issue 工具自己算：

| 度量（读数键） | 谁报 | 怎么算 |
|---|---|---|
| `test_seconds` 测试套件的全量墙钟秒数 | 收尾那次全量验证的会话（**这条线配了 `test_seconds_limit` 才报**） | 按项目文档认定的那套命令实测；没跑就把这格删掉，别估 |
| `issue_pool_weight` issue 池加权和 | 工具自己算：`pf issue weight` | 口径与动作不在这份文档里：见 `skills/shared/pf/pool.md`；加／查条目看 `pf issue --help` |

**判定在收尾那一拍。** `next --after <node>` 结算时读这两个数、比对阈值、决定插不插。它读的是**这一次收尾手里正好有的两样东西**：这个节点的条子，和刚刚挑出来的下一个节点 —— 挑人先算（它本来就要算），判定紧跟其后，两样都不到别处去问（池子那个数由它自己当场跑 issue 工具算，不向会话要）。往上走一步，判定发生在**能算的地方**（工具），会话只交那个只有它测得到的读数，从不拍板要不要维护。

**一个度量同时只开一个维护节点。** 插入前先查图上有没有这个度量未了结的维护节点 —— 按节点 tag `maint:<读数键>` 来认（如 `maint:test_seconds`），只有 `todo` 的算未了结：有就跳过这个度量，`completed` / `shelved` 都不拦。

**插入 = 改图。** 把刚收掉的节点与它原本挑中的下一个节点之间那条边拆成两条边，中间放进维护节点：

```
旧节点 ──► 下一个节点        旧节点 ──► 维护节点 ──► 下一个节点
```

它成为旧节点的直接下游、原下游全体变成它的下游，urgency 与 importance 由图的算法自己算出来——于是它就落在"现在最该做"的位置上。**排序一处都不改。** 被挑中的下一个节点未必与旧节点有边，那这条边本来就不存在，拆它是一个 no-op；两条新边照连。

这一次改图是**收尾那一发命令**里的几条，顺序固定：收尾自己的命令（`cycle close` / `pf dag status`）**先跑**，然后才 `add` 建节点 → `undepend` 拆旧边 → 两条 `depend` → `cycle hold` 把维护节点添进这条线的手里。拆旧边必须排在新边前面：旧边还在时，两条新边会绕出一个环，而环里那张图它拒收。`cycle hold` 只是往手里添一位，线里别的格不动；这条线不转 inactive —— 新节点插完就派发。新节点的 uuid 由 `pf dag add` 自己印在 stdout 上，工具从那里读回来 —— 那是它唯一出现的地方。**spec 走模板。** 维护节点的 spec 由工具生成：为什么被插进来、这次的范围、怎么干（一行 `方法：<技能名>`，指到那一类债的方法卡；跑法正文只在那份卡里）、以及一节机械可判的完成判定（把那个度量拉回线下，附可跑的命令）。判断类的活（"目录该怎么分"这类给不出判据的）永远走不进这条通道。模板里不写项目自己的函数名、文件路径、行号 —— 那些是这个节点自己的备料，模板猜一个就是骗一个。`add` 把这份文本带进图里（节点行上的 description），收尾那一发同时把它写成 `.pf/nodes/<新节点>/spec.md` —— 会话读的是那个文件，而 `--plan` 一个字节都不写。

**领域限定。** 两个度量都归代码这一支，只有 tag 含 `coding` 的 cycle 会因它们插入。别的线读得到同一个数，但不动、也不等——所以被挡住的只有 coding 路径。

**两条线是这条线自己的策略。** 它们是 `cycle.policies` 上的两个数值键（`test_seconds_limit` / `issue_pool_weight_limit`），跟 `tags`、`when_shelved` 同一格：`cycle policy <cycle> test_seconds_limit 15` 写、`off` 取消，`pf cycle current` 的 `Policy:` 行把它打出来。**一条线不配时，这条通道对它整个不生效**——收尾的返回值与它出现之前逐字节相同，这就是它的回归底线。一条不碰代码的线身上根本没有这两个键，所以它不会被卷进代码债里来。

**读数由条子骨架点名要。** `reading` 是 `pf handoff --form <cycle>` 印出来的骨架里第四个 `outputs` 键（见「条子」）：这条线配了 `test_seconds_limit` 时才现印出这一格，名字写明、值留成占位符；池子那格由工具自己算，不向会话要。阈值不注入会话，读数得从会话手里要，而条子骨架是唯一够得着那个会话的通道。没测就把整个键删掉 —— 缺一个数读作「这一趟不知道」，而 0 读作「已经量到零了」，那会让一条真实的债看起来还清了。

**判定用什么比。** 读到的数**大于或等于**线就算越线：线是还能接受的最后一个数，读到它就意味着债到顶了。一个度量一根线，过了线就插节点；两个度量同时越线时按表里的先后取第一个，一次只为一个度量插一个节点 —— 谁越线谁自己进来一趟。

**两半的落定方式不同。** 测试时长下次跑测试自己就刷新了；issue 池那条靠维护节点自己落定（出口与动作见 `pf-issue-repair`，工具与口径见 `skills/shared/pf/pool.md`）。维护节点做完照常收尾，接着走下一个，不停下来等人回话。
