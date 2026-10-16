# DSH 派发与鉴权（conduct 管线用）

> `skills/pf-conduct/SKILL.md` 的「派发通路」一节留下给人看的部分（谁调它、断了看哪个文件）；
> 这份文档放**为什么这么写**、**别怎么写**，以及 cookie 自签的完整算法——供排障和搬迁时对照。
> 现成实现：`tools/conduct/spawn/dsh-spawn.ps1`——**唯一**拼它这份 argv 的地方是
> `tools/conduct/spawn/conduct_spawn.py`，管线（`pf next --spawn` 与
> `dispatch <cycle>`）经它调用。
>
> **现状提醒**：`dsh-spawn.ps1` **只开干活会话**，`-PromptFile` 必填——没有 prompt 的调用当场报错
> 退出。整条链上的每一条会话都是这样开出来的，没有第二种会话。

## 一、谁开这个会话

`conduct_pipeline_impl/writer.py` 的 `spawn_session()` 是管线**唯一**开会的口：它经 `spawn/conduct_spawn.py`
调 `dsh-spawn.ps1`，把
`dispatch` 动作的 `prompt`（`.pf/meta/launch/<cycle>/<node_id>/prompt.txt`）发出去。入口是
`pf next --spawn`——干活会话收尾时那一条；`--plan` 是只读预览，绝不 spawn。conductor
自己不手敲 API：`commands` 一律由工具跑，调用方只读 `action`。

建会话是环境专属动作（HTTP 端点、cookie 鉴权、工作区分组），所以它单独关在一个脚本里，三步：

1. `workspace/create` 拿 `workspaceId`
2. `session/create` 只用 `workspaceId` 建会话
3. `session/prompt` 发 `prompt.txt` 全文（`mode: "queue"`）

成功时那一行 `spawned: session <id> (…)` 是这条链唯一说出会话 id 的地方，管线就从它这里
把 id 落进那一格的会话便条（`.pf/meta/sessions/<节点 id>.json` 的 `run` 清单，见
[`./conduct-state.md`](./conduct-state.md) §3.6）——便条只在本机有意义，所以它不进记录。

## 二、三处实测校准（照抄旧写法会静默失败）

### 1. 端点用斜杠形式

`workspace/create`、`session/create`、`session/prompt`——点号写法（`session.create`）回 `not found`。
HTTP 层是 `POST {base}/api/{method}`，`base` 默认 `http://127.0.0.1:3080`。

### 2. 信封形状

```jsonc
{
  "type": "client-request",
  "rpcId": "<任意唯一串>",
  "method": "session/create",
  "payload": { "args": { "request": { /* 端点参数 */ } } }
}
```

`payload` 必须**恰好含一个普通对象 `args`**，端点参数放在 `args` 下的具名键里——这三个端点
都叫 `request`。响应取 `result.value`；`result.ok` 为假时 `result.error` 里有详情。
发请求的现成函数在 `dsh-spawn.ps1` 里，不必另写。

### 3. 工作区：先取 id，再只带 id 建会话

只传 `cwd` 能建出会话（落盘位置也对），但它**不进工作区分组**，在 GUI 里散落在"未分组"区，
用户看不到管线跑到哪——而"用户看得见"正是这条通路存在的理由。于是：

```pwsh
$wsId = (Invoke-Dsh workspace/create @{ path=(Get-Location).Path }).workspace.workspaceId
$sid  = (Invoke-Dsh session/create @{ workspaceId=$wsId }).sessionId
$text = Get-Content -Raw ".pf/meta/launch/<cycle_id>/<node_id>/prompt.txt"
Invoke-Dsh session/prompt @{
  requestId = [guid]::NewGuid().ToString()
  sessionId = $sid
  mode      = "queue"
  content   = @(@{ type="text"; text=$text })
} | Out-Null
```

`workspace/create` 是幂等的（同一目录返回既有工作区）；端点明确拒绝 `workspaceId` 与 `cwd`
同时出现；只给 `workspaceId` 时，会话的 `cwd` 就是该工作区绑定的目录。

## 三、鉴权：自签持久 cookie

**别再从 `~/.dsh/web-run.log` 抓进程 launch token 换 cookie。** 这个 token 每次启动 `dsh web`
都重新生成，日志却不随之更新，重启后必然 401；而 `/api` 走的是 cookie 鉴权路径，launch token
只是换 cookie 的门票。

改用凭据里的**持久签名密钥**自签：

| 项 | 值 |
|---|---|
| 密钥来源 | `~/.dsh/.credentials.yaml` 中 `client-connection/browser-session` 记录的 `secret`（base64url 编码的 32 字节） |
| cookie 名 | `dsh-auth-` + base64url(sha256(authority))，`authority` 形如 `127.0.0.1:3080` |
| cookie 值 | `v1.<base64url(payload_json)>.<base64url(hmac_sha256(secret, body))>`，`body` 即 base64url(payload_json) |
| payload | `{version:1, authority, issuedAt, expiresAt}`，毫秒时间戳 |
| 有效期 | 30 天（与 dsh 默认 `cookieMaxAgeDays` 一致），跨重启有效 |

实现见 `dsh-spawn.ps1` 的 `New-BrowserSessionCookie` / `ConvertTo-B64Url` / `ConvertFrom-B64Url`。

## 四、派发链与它的失败模式

一个节点干完跑哪两条命令、返回结果怎么读，在 [`./conduct-state.md`](./conduct-state.md) 的收尾与
恢复两节（「动作表」「收尾那一次调用」「恢复」）。spawner 这一边只添三件事：

- **旧 session 一律不复活**，每次都是新开一个。
- `dsh-spawn.ps1` 对每个 API 调用自带重试（退避递增）；全部失败时把事故写进
  `.pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json`，spawn 成功时清除。标记文件存在 = 这条链现在是断的，
  字段与人工接管的走法见 [`./conduct-state.md`](./conduct-state.md)「恢复」。
- 排查顺序：401 看 cookie 与凭据文件；404 看方法名是不是点号写法；`result.ok=false` 看
  `result.error`；会话建了但 GUI 里在"未分组"，看是不是错传了 `cwd`。改鉴权路径时务必端到端验
  一次（用一个自检 cycle id 跑通"新会话建立 → prompt 注入 → `/pf-conduct` 被加载"）。

## ZCode 那一份 spawner

同一个位置还有第二份实现：**ZCode 宿主上由 `zcode-spawn.ps1` 开会话**。它不做 HTTP 调用，而是往
`~/.zcode/v2/tasks-index.sqlite` 的 `automations` 表写一行，交给 ZCode 桌面自己的 cron 调度器
去建 —— 只有 GUI 自己建的会话才进它的任务列表，而"看得见、叫得停、进度看得见"正是这条通路
存在的理由（外部命令行开的会话永远进不去那份列表）。参数、`spawned:` 末行、降级标记三条契约与
`dsh-spawn.ps1` 完全一致，`conduct_spawn.py` 那份 argv 两边通用；通路细节与"为什么绕这一道"
写在 `zcode-dispatch.py` 的模块说明里。

**选哪一份由工具回答，不由调用方拼**，最具体的答案赢，依次是：`--spawner`（`dispatch` /
`next --spawn` 当场点名，`dsh` / `zcode` / 一条路径）> 这条线自己的 `spawner` 策略（
`pf cycle policy <cycle> spawner dsh|zcode`，落在线记录上随 `.pf/` 跨机器走，`off` 清除；
只收词、不收路径——机器本地的路径落进共享状态就是另一台机器上的死值）>
`DSH_CONDUCT_SPAWNER`（这台机器，`dsh` / `zcode` / 一条路径）> 宿主自认（DSH 往每次 shell
调用注入 `DSH_SESSION_ID`，这是它的答案；ZCode 不给 Bash 工具注任何标记，所以顺着父进程链找
`ZCode.exe`；探测答不上来就退到 `dsh`）。线没配策略、调用没带旗子，行为与线策略存在之前
一字不变。实现收在 `conduct_pipeline.py` 的 `resolve_spawner`，测试在
`tests/cases_zcode.py`（选路）与 `tests/cases_dispatch.py`（两个调用点的接线）。

**那一行带 `mode = yolo`（完全访问）**，这不是偏好：干活会话第一次改文件就会停下来等人批准，
而那一刻没人在键盘前 —— 会话建出来却在第一个 edit 上静静躺着，比排不进去更难查。要收紧就用
`--mode build|edit|plan`（环境变量 `DSH_CONDUCT_SPAWN_MODE`）。验证档位要看会话本身，不要看
任务行——`tasks.mode` 不反映真实档位，生效的那份记在 `~/.zcode/cli/db/db.sqlite` 的
`model_usage.mode`。

**模型缺省由一个开关给**：`zcode-dispatch.py` 的 `DEFAULT_MODEL` 决定没人点名时跑哪个，改一个键就是
全量换模型——点名而非跟随 workspace，理由写在它的模块说明里。要当场换用
`--model provider/model$level`（档位写全，桌面只认带 `reasoningLevel` 的全形）；环境变量
`DSH_CONDUCT_SPAWN_MODEL` 盖过开关，它是宿主进程启动时的快照，改完要重启宿主，想让开关说了算得先
清掉它——包括 `HKCU\Environment` 里持久化的那份。合法 id 用 `~/.zcode/v2/provider_config.json`
与内置目录里的模型清单对。

**验证**：`tests/cases_zcode.py`（选路、那一行的形状、两处响亮的拒绝、包装脚本的末行与标记），
沙箱靠 `ZCODE_DATA_BASE_DIR` 把 dispatcher 指向临时库，跑测试不会碰真装机、也不会开出真会话。
