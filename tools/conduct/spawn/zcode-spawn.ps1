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
# Usage: pwsh -File zcode-spawn.ps1 -CycleId <id> -PromptFile <path> [-WorkspacePath <dir>]
#                                  [-At <when>] [-Mode <mode>] [-Model <pick>] [-NoVerify]
#   -CycleId       必填。cycle 标识（同 state-manage.py 的 cycle id）。
#   -PromptFile    必填。节点 session 的 prompt 全文（第一行是 skill 调用行）；缺了直接报错退出。
#   -WorkspacePath 可选。子会话的工作目录，也是 GUI 里的分组键；缺省用当前目录。
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
#   ② 任何失败都把原因写进 .pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json，且不清除；
#   ③ 成功时清掉本线自己留下的旧标记（标记存在 = 链现在是断的）；按线分文件，并行=2 时
#      本线成功不抹别线的断链记号。
# ============================================================================

param(
    [Parameter(Mandatory = $true)][string]$CycleId,
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [string]$WorkspacePath,
    [string]$At,
    [string]$Mode,
    [string]$Model,
    [switch]$NoVerify
)

$ErrorActionPreference = "Stop"

# —— 工作目录与降级标记：在任何调用之前先定下来，失败时才写得了盘 ——
$target = if ($WorkspacePath) { $WorkspacePath } else { (Get-Location).Path }
$script:MarkerPath = Join-Path $target ('.pf\meta\CONDUCT-CALLBACK-FAILED-' + $CycleId + '.json')
# 只进标记文本，给人看的；真正的库路径由 zcode-dispatch.py 算（它认同一个环境变量）。
$dataBase = if ($env:ZCODE_DATA_BASE_DIR) { $env:ZCODE_DATA_BASE_DIR } else { $env:USERPROFILE }
$script:DbPath = Join-Path $dataBase '.zcode\v2\tasks-index.sqlite'

function Write-CallbackFailureMarker {
    param(
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Reason
    )
    if ($script:MarkerWritten) { return }
    $script:MarkerWritten = $true
    try {
        $dir = Split-Path -Parent $script:MarkerPath
        if (-not (Test-Path $dir)) { $null = New-Item -ItemType Directory -Force -Path $dir }
        $record = [ordered]@{
            cycle_id    = $CycleId
            phase       = $Phase
            attempts    = 1
            reason      = $Reason
            workspace   = $target
            db          = $script:DbPath
            detected_at = [DateTimeOffset]::UtcNow.ToString('o')
            note        = ('conductor 的下一棒断在这里：节点会话没有排进 ZCode 的任务队列，管线不会再 ' +
                           '自己往前走。手动跑 state-manage.py next 接管；确认无碍后删掉本文件。')
        }
        # 不带 BOM 写出：这份 JSON 是要被工具读的，BOM 会让 json.load(encoding='utf-8') 炸。
        # 落盘走临时文件 + 同卷改名：并发读者要么读到整份、要么读不到，不撞见半截 JSON。
        $tmp = "$script:MarkerPath.tmp-$PID"
        [System.IO.File]::WriteAllText(
            $tmp,
            ($record | ConvertTo-Json -Depth 5),
            (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -Path $tmp -Destination $script:MarkerPath -Force
        Write-Warning "conduct 派发失败，已写下降级标记：$script:MarkerPath"
    }
    catch {
        Write-Warning "写降级标记失败（$script:MarkerPath）：$($_.Exception.Message)"
    }
}
$script:MarkerWritten = $false

# —— prompt 是这次调用的全部内容：没有 prompt 就没有会话可排，不给就报错退出 ——
if (-not (Test-Path $PromptFile)) {
    Write-CallbackFailureMarker -Phase 'read-prompt' -Reason "找不到 prompt 文件 $PromptFile"
    throw "prompt file not found: $PromptFile (-PromptFile is required)"
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-CallbackFailureMarker -Phase 'find-python' -Reason 'PATH 上没有 python：zcode-dispatch.py 跑不起来'
    throw "python not found on PATH (zcode-dispatch.py needs it)"
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
$spawnArgs = @('-X', 'utf8', $dispatcher, '--prompt-file', $PromptFile, '--workspace', $target)
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

# 派发成功：清掉上一次留下的降级标记（标记存在 = 链现在是断的；链一旦续上它就没有意义了）。
if (Test-Path $script:MarkerPath) {
    # 清也先改名再删：标记从「在」到「不在」是一步同卷改名，并发读者撞不见中间态；改名失败
    # （正被别的进程读着）就退回原地删——清不掉不许挡派发成功。
    $stale = "$script:MarkerPath.clearing-$PID"
    Move-Item -Path $script:MarkerPath -Destination $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $script:MarkerPath -Force -ErrorAction SilentlyContinue
    Write-Output "cleared a stale callback failure marker: $script:MarkerPath"
}

if ($sessionId) {
    Write-Output "spawned: automation $automationId (session $sessionId, node session, prompt=$PromptFile)"
} else {
    Write-Output "spawned: automation $automationId (node session, prompt=$PromptFile)"
}
