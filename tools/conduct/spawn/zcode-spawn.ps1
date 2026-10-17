# ============================================================================
# zcode-spawn.ps1 — 把一个节点会话排进 ZCode 自己的 cron 调度器（管线派发用）
# ============================================================================
# 只由 conduct_spawn.py 调用（管线经 `state-manage.py next --spawn` 与 `dispatch <cycle>` 走它）。
# 它和 dsh-spawn.ps1 是同一处的两种实现：那边走 DSH HTTP API 直接开一个
# DSH 会话，这边往 `~/.zcode/v2/tasks-index.sqlite` 的 `automations` 表写一行，交给桌面自己的
# 调度器（每 20 秒一轮）去开会话。
#
# 为什么绕这一道：只有 GUI 自己建的会话才进它的任务列表（那份列表的来源是 agent host 进程里
# 一次性播种的投影，本地 workspace 没有重读入口），而你盯进度、叫停、放行都靠那份列表。
# 细节见 zcode-dispatch.py 的模块说明。
#
# 外壳（参数缺省、prompt 校验、降级标记的写与清）与 `dsh-spawn.ps1` 共用同一份：
# `spawn-common.ps1`，本文件只留 ZCode 这一半（找 dispatcher、排那一行、读回结果、末行）。
# 标记的形状与落点在 `marker.py`（ZCode 自己的库路径进标记的 `extra`）。
#
# Usage: pwsh -File zcode-spawn.ps1 -CycleId <id> -PromptFile <path> -MarkerPath <path>
#                                  [-WorkspacePath <dir>] [-PythonExe <path>]
#                                  [-At <when>] [-Mode <mode>] [-Model <pick>] [-NoVerify]
#   -CycleId       必填。cycle 标识（同 state-manage.py 的 cycle id）。
#   -PromptFile    必填。节点 session 的 prompt 全文（第一行是 skill 调用行）；缺了直接报错退出。
#   -MarkerPath    必填。降级标记的完整路径（`conduct_spawn.argv_for` 从 marker.py 取）。
#   -WorkspacePath 可选。子会话的工作目录，也是 GUI 里的分组键；缺省用当前目录。
#   -PythonExe     可选。跑 zcode-dispatch.py 那个 python；缺省认 PATH 上的 python（写标记
#                  不经过它）。
#   -At            可选。什么时候跑：`+30m` / `+2h` / 本地时刻（如 `2026-10-07T23:00`）。
#                  用它把节点排进夜里那口免费窗口；缺省=立刻。
#                  环境变量 `DSH_CONDUCT_SPAWN_AT` 同义（管线的 argv 不带这个参数）。
#   -Mode          可选。会话的权限档：`yolo`（缺省，完全访问）/ `build` / `edit` / `plan`。
#                  **别改成需要问的档**：干活会话第一次改文件就会停下来等人回答，而那一刻没人在。
#                  环境变量 `DSH_CONDUCT_SPAWN_MODE` 同义。
#   -Model         可选。会话跑哪个模型：`provider/model$level`（GUI 模型选择器的那串原文）。
#                  缺省由 zcode-dispatch.py 里那个开关给：`MODEL_CHOICES` 的两个键（`glm` =
#                  内置智谱账号的 GLM-5.3-Flash / `opencode` = OpenCode Go (Chat) 的
#                  deepseek-v4.1-flash）里，`DEFAULT_MODEL` 指着哪个就用哪个。
#                  系统性地换模型：改 `DEFAULT_MODEL` 那一行的键，别在别处再写死一份。
#                  写 `workspace` = 跟随该 workspace 的首选，也就是你在界面上最后选的那个 ——
#                  后台节点别用它：那批会话的消耗会落到你界面上当时选中的那份额度上。
#                  合法 id 见 `~/.zcode/v2/provider_config.json` 的 `modelOrder` 与内置目录里的
#                  模型列表（账号型 provider 的模型名有大小写）。
#                  环境变量 `DSH_CONDUCT_SPAWN_MODEL` 同义且优先于缺省；它由宿主进程在启动时
#                  快照，改完环境变量要重启宿主才生效。注意：它一旦设上就盖住上面那个开关，
#                  想让开关说了算，得先把它清掉（含 HKCU\Environment 里的持久化那份）。
#   -NoVerify      可选。写完那一行就返回，不等桌面把会话建出来。给演练和排期用；管线不带它，
#                  因为"排队成功"和"会话真的存在"是两件事 —— 等不到时它会把标记写下来。
#
# 依赖：pwsh 7+、PATH 上的 python 3、ZCode 桌面在运行。桌面关着的时候排的行会在它启动时才被看见，
# 而那时若已过期超过 5 分钟，调度器会当 misfire 跳过 —— zcode-dispatch.py 会在那种情况下往 stderr
# 提醒一句。
#
# 与 dsh-spawn.ps1 的共同契约（别改）：
#   ① 成功时最后一行是 `spawned: ...`；
#   ② 任何失败都写下降级标记，且不清除；
#   ③ 成功时清掉本线自己留下的旧标记（标记存在 = 链现在是断的）；按线分文件，并行=2 时
#      本线成功不抹别线的断链记号。
#   ① 末行由本文件自己打（两个宿主的末行不同）；② 与 ③ 由 `spawn-common.ps1` 那一份外壳保证 ——
#   这一半独有的「找不到 python」那一拍也照 ② 写下降级标记再退出。
# ============================================================================

param(
    [Parameter(Mandatory = $true)][string]$CycleId,
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [string]$MarkerPath,
    [string]$WorkspacePath,
    [string]$PythonExe,
    [string]$At,
    [string]$Mode,
    [string]$Model,
    [switch]$NoVerify
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'spawn-common.ps1')

# —— 工作目录与降级标记：在任何调用之前先定下来，失败时才写得了盘 ——
# ZCode 的库路径只进标记的 extra，给人看的；真正的库路径由 zcode-dispatch.py 算（它认同一个
# 环境变量，沙箱靠 `ZCODE_DATA_BASE_DIR` 指到临时库）。
$dataBase = if ($env:ZCODE_DATA_BASE_DIR) { $env:ZCODE_DATA_BASE_DIR } else { $env:USERPROFILE }
$dbPath = Join-Path $dataBase '.zcode\v2\tasks-index.sqlite'
Initialize-Spawner -CycleId $CycleId -PromptFile $PromptFile -WorkspacePath $WorkspacePath `
                   -MarkerPath $MarkerPath -PythonExe $PythonExe -Extra "db=$dbPath"

# —— prompt 是这次调用的全部内容：没有 prompt 就没有会话可排，不给就报错退出 ——
Assert-PromptFile

$python = $script:SpawnPython
if (-not $python) {
    # 这一拍链就断在这里：按契约②写下降级标记再退出 —— 标记由外壳自己写，不需要 python。
    Write-CallbackFailureMarker -Phase 'find-python' `
        -Reason "找不到 python：zcode-dispatch.py 跑不起来；把它放上 PATH，或用 -PythonExe 指名。"
    Write-Warning "zcode-dispatch.py 需要 python：把它放上 PATH，或用 -PythonExe 指名。"
    throw "python not found (zcode-dispatch.py needs it)"
}

$dispatcher = Join-Path $PSScriptRoot 'zcode-dispatch.py'
if (-not (Test-Path $dispatcher)) {
    Write-CallbackFailureMarker -Phase 'find-dispatcher' -Reason "找不到 $dispatcher"
    throw "dispatcher not found: $dispatcher"
}

# —— 排一行；`--at` / `--mode` / `--model` 只认显式参数与环境变量（管线的 argv 不带它们）——
$when = if ($At) { $At } else { $env:DSH_CONDUCT_SPAWN_AT }
$permissionMode = if ($Mode) { $Mode } else { $env:DSH_CONDUCT_SPAWN_MODE }
$whichModel = if ($Model) { $Model } else { $env:DSH_CONDUCT_SPAWN_MODEL }
$spawnArgs = @('-X', 'utf8', $dispatcher, '--prompt-file', $script:SpawnPromptFile,
               '--workspace', $script:SpawnTarget)
if ($when) { $spawnArgs += @('--at', $when) }
if ($permissionMode) { $spawnArgs += @('--mode', $permissionMode) }
if ($whichModel) { $spawnArgs += @('--model', $whichModel) }
if ($NoVerify) { $spawnArgs += '--no-verify' }

$lines = @(& $python @spawnArgs 2>&1)
$code = $LASTEXITCODE
if ($code -ne 0) {
    $detail = ($lines | ForEach-Object { "$_" }) -join "`n"
    if (-not $detail) { $detail = "zcode-dispatch.py exited $code with no output" }
    Write-CallbackFailureMarker -Phase 'queue-session' -Reason $detail
    throw "zcode-dispatch.py failed ($code): $detail"
}

# 成功路径：stdout 上第一行是 automation id，验证过时第二行是 `session: <id>`（warning 走 stderr，
# 混进来也不匹配这两个形状）
$automationId = ($lines | Where-Object { "$_" -match '^automation-' } | Select-Object -Last 1)
if (-not $automationId) {
    $detail = "zcode-dispatch.py printed no automation id: " + (($lines | ForEach-Object { "$_" }) -join ' | ')
    Write-CallbackFailureMarker -Phase 'read-result' -Reason $detail
    throw $detail
}
$sessionId = ''
$sessionLine = ($lines | Where-Object { "$_" -match '^session:\s*sess_' } | Select-Object -Last 1)
if ($sessionLine) { $sessionId = ("$sessionLine" -replace '^session:\s*', '').Trim() }

# 派发成功：清掉上一次留下的降级标记（标记存在 = 链现在是断的）。
Clear-CallbackFailureMarker

if ($sessionId) {
    Write-Output "spawned: automation $automationId (session $sessionId, node session, prompt=$($script:SpawnPromptFile))"
} else {
    Write-Output "spawned: automation $automationId (node session, prompt=$($script:SpawnPromptFile))"
}