# DSH 派发与鉴权（conduct 管线用）

> [`../skills/pf-conduct/SKILL.md`](../skills/pf-conduct/SKILL.md) 的「派发通路」一节是给会话看的那一半（谁调它、断了看哪个文件）；
> 这份放**为什么这么写**、**别怎么写** —— 供排障与搬迁时对照。实现是
> `tools/conduct/spawn/dsh-spawn.ps1`，**唯一**拼它那份 argv 的地方是 `spawn/conduct_spawn.py`，
> 管线（`pf next --spawn` 与 `dispatch <cycle>`）经它调用。
>
> **`dsh-spawn.ps1` 只开干活会话**：`-PromptFile` 必填，没有 prompt 的调用当场报错退出 —— 整条链上
> 的每一条会话都是这样开出来的。

## 一、谁开这个会话

`conduct_pipeline_impl/writer.py` 的 `spawn_session()` 是管线**唯一**开会的口：它经 `spawn/conduct_spawn.py`
调 `dsh-spawn.ps1`，把 `dispatch` 动作写下的那份 prompt 发出去。入口是 `pf next --spawn`——干活会话收尾
时那一条；`--plan` 是只读预览，绝不 spawn。conductor 自己不手敲 API：`commands` 一律由工具跑，调用方
只读 `action`。

建会话是环境专属动作（HTTP 端点、cookie 鉴权、工作区分组），所以它单独关在一个脚本里。成功时那一行
`spawned: session <id> (…)` 是这条链唯一说出会话 id 的地方，管线就从它这里把 id 落进那一格的会话便条
（`run` 清单）—— 便条只在本机有意义，所以它不进记录。

## 二、三处实测校准（照抄旧写法会静默失败）

1. **端点用斜杠形式。** `workspace/create`、`session/create`、`session/prompt`——点号写法
   （`session.create`）回 `not found`。HTTP 层是 `POST {base}/api/{method}`，`base` 默认
   `http://127.0.0.1:3080`。
2. **信封形状。** `payload` 必须**恰好含一个普通对象 `args`**，端点参数放在 `args` 下的具名键里
   ——这三个端点都叫 `request`。响应取 `result.value`；`result.ok` 为假时 `result.error` 里有详情。
   发请求的现成函数在 `dsh-spawn.ps1` 里，不必另写。
3. **工作区：先取 id，再只带 id 建会话。** 只传 `cwd` 能建出会话（落盘位置也对），但它**不进工作区
   分组**，在 GUI 里散落在"未分组"区，用户看不到管线跑到哪——而"用户看得见"正是这条通路存在的
   理由。`workspace/create` 是幂等的（同一目录返回既有工作区）；端点明确拒绝 `workspaceId` 与
   `cwd` 同时出现；只给 `workspaceId` 时，会话的 `cwd` 就是该工作区绑定的目录。

## 三、鉴权：自签持久 cookie

**别再从 `~/.dsh/web-run.log` 抓进程 launch token 换 cookie。** 这个 token 每次启动 `dsh web` 都重新
生成，日志却不随之更新，重启后必然 401；而 `/api` 走的是 cookie 鉴权路径，launch token 只是换
cookie 的门票。

改用凭据里的**持久签名密钥**自签：密钥取 `~/.dsh/.credentials.yaml` 里 `client-connection/browser-session`
记录的 `secret`，签出的 cookie 有效期 30 天（与 dsh 默认 `cookieMaxAgeDays` 一致）、跨重启有效。
算法与 cookie 名在 `dsh-spawn.ps1` 的 `New-BrowserSessionCookie` 里。

## 四、派发链与它的失败模式

一个节点干完跑哪两条命令、返回结果怎么读，见 [`./conduct-state.md`](./conduct-state.md) 的收尾与恢复
两节。spawner 这一边只添三件事：

- **旧 session 一律不复活**，每次都是新开一个。
- `dsh-spawn.ps1` 对每个 API 调用自带重试（退避递增）；全部失败时把事故写进
  `.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json`，spawn 成功时清除。标记文件存在 = 这条链现在是断的，
  人工接管的走法见 [`./conduct-state.md`](./conduct-state.md)「恢复」。
- 排查顺序：401 看 cookie 与凭据文件；404 看方法名是不是点号写法；`result.ok=false` 看
  `result.error`；会话建了但 GUI 里在"未分组"，看是不是错传了 `cwd`。改鉴权路径时务必端到端验
  一次（用一个自检 cycle id 跑通"新会话建立 → prompt 注入 → `/pf-conduct` 被加载"）。

## ZCode 那一份 spawner

同一个位置还有第二份实现：**ZCode 宿主上由 `zcode-spawn.ps1` 开会话**。它不做 HTTP 调用，而是往
`~/.zcode/v2/tasks-index.sqlite` 的 `automations` 表写一行，交给 ZCode 桌面自己的 cron 调度器去建
——只有 GUI 自己建的会话才进它的任务列表，而"看得见、叫得停、进度看得见"正是这条通路存在的理由
（外部命令行开的会话永远进不去那份列表）。参数、`spawned:` 末行、降级标记三条契约与 `dsh-spawn.ps1`
完全一致，`conduct_spawn.py` 那份 argv 两边通用；通路细节与"为什么绕这一道"写在 `zcode-dispatch.py`
的模块说明里。

**选哪一份由工具回答，不由调用方拼**，最具体的答案赢：当场点名 > 这条线自己的 `spawner` 策略 >
这台机器的环境变量 > 宿主自认（探测答不上来时退到 `dsh` 那一份）。线策略**只收词、不收路径** ——
机器本地的路径落进共享状态就是另一台机器上的死值。线没配策略、调用没带旗子，行为与这个功能存在
之前一字不变。实现收在 `conduct_pipeline.py` 的 `resolve_spawner`。

**那一行带 `mode = yolo`（完全访问）**，这么定有它的理由：干活会话第一次改文件就会停下来等人批准，
而那一刻没人在键盘前 —— 会话建出来却在第一个 edit 上静静躺着，比排不进去更难查。要收紧就用
`--mode`（或环境变量）。**验证档位要看会话本身，不要看任务行** ——任务行上的档位不反映真实档位，
生效的那份记在 ZCode 自己的库里。

**模型缺省由一个开关给**：没人点名时跑哪个由 `zcode-dispatch.py` 的一个常量决定，改一个键就是全量
换模型。当场换用 `--model`；环境变量盖过那个开关，而它是宿主进程启动时的快照 —— 改完要重启宿主，
想让开关说了算得先清掉它，包括 `HKCU\Environment` 里持久化的那份。

**验证**：`tests/cases_zcode.py`（选路、那一行的形状、两处拒绝、包装脚本的末行与标记），沙箱靠
`ZCODE_DATA_BASE_DIR` 把 dispatcher 指向临时库，跑测试不碰真装机、也不开出真会话。