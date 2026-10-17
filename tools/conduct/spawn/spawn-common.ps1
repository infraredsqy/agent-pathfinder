# ============================================================================
# spawn-common.ps1 — 两份 spawner 共用的那层外壳（dot-source 它，不直接跑）
# ============================================================================
# dsh-spawn.ps1 与 zcode-spawn.ps1 的差别只有中间那一段（走 DSH HTTP API / 往 ZCode 的 cron
# 表排一行）；外壳里逐行同构的部分收在这一份正本：工作目录的缺省、prompt 文件校验、降级标记
# 的写入与清除、失败时怎么说。留在各自脚本里的是宿主差异：param 块（zcode 多
# -At/-Mode/-Model/-NoVerify）、中间那段、末行 `spawned:`。
#
# 降级标记的**形状与落点**有一处正本：同目录的 `marker.py`（python 侧与条子那一拍用它）。这一层
# 按同一份合同**自己写**，是一条例外，理由是物理上的：spawner 跑起来的那台机器、那个时刻可能
# 没有 python，而 PowerShell 没法 import 一个 python 模块 —— 这条写入恰恰是链断掉之后唯一留给人
# 的入口，它不能因为解释器不在而消失。代价是形状有两份实现，所以 `conduct/tests/cases_cli.py` 的 `TestTheSpawnerShell`
# 拿一条等价性用例把两边钉住：同一组键、同一句人话、同落点（`-MarkerPath`）、二次失败不覆盖、
# 清后不在。改形状时两处一起改，用例当场判红。
#
# 清标记不走这条路：删文件没有形状可言，而成功那一拍不该因为清不掉而失败。
#
# `-MarkerPath` 必给：标记的落点由调用方算（`conduct_spawn.argv_for` 从 `marker.py` 取），
# spawner 不自拼这个路径 —— 记录目录一改名，自拼的那几处会安静地写到工具不读的位置。
#
# 这里的 `$script:` 指的是**调用方脚本的作用域**：dot-source 让两份脚本与本层共用一个 script
# 作用域，spawner 就是这样把自己的状态交给这些函数（`Initialize-Spawner` 写、别的函数读）。
#
# 本文件要能被 Windows PowerShell 5.1 读进来（装机上的 spawner 与排障都可能是它），所以语法停在
# 5.1 那一套；文件本身是 UTF-8 with BOM，无 BOM 时 5.1 按控制台代码页读，中文会变成乱码。
# ============================================================================

$script:SpawnCommonDir = $PSScriptRoot

#: 标记自己写下的那句人话 —— 与 `marker.py` 的 `DEFAULT_NOTE` 一字不差，等价性用例盯着这两份。
$script:SpawnMarkerNote = ('链条断在这里：本拍没能把下一棒开起来，管线不会再自己往前走。手动接管是 ' +
                           '`pf next --after <node_id>`（收尾那一拍）或 `pf dispatch <cycle>`（派发那一拍）；' +
                           '确认无碍后删掉本文件。')

function ConvertTo-SpawnExtra {
    # `KEY=VALUE` 那一串（调用方递宿主事实的写法）解析成一份对象：dsh 递 api_base，zcode 递 db。
    param([string[]]$Pairs = @())
    $extra = @{}
    foreach ($pair in $Pairs) {
        if (-not $pair) { continue }
        $text = "$pair"
        $at = $text.IndexOf('=')
        if ($at -lt 0) { $key = $text; $value = '' }
        else { $key = $text.Substring(0, $at); $value = $text.Substring($at + 1) }
        if ($key.Trim()) { $extra[$key.Trim()] = $value }
    }
    return $extra
}

function Move-SpawnMarkerIntoPlace {
    # 临时文件 + 同卷改名，改名那一步有界重试：读方此刻正开着目标文件时 Windows 会拒，而这条
    # 写入恰恰是不许丢的那条（与 `tools/shared/jsonio.py` 的 `replace_with_retry` 同一条理由、
    # 同一份次数）。
    param(
        [Parameter(Mandatory = $true)][string]$From,
        [Parameter(Mandatory = $true)][string]$To,
        [int]$Attempts = 4
    )
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Move-Item -Path $From -Destination $To -Force -ErrorAction Stop
            return
        }
        catch {
            if ($i -ge $Attempts) { throw }
            Start-Sleep -Milliseconds 50
        }
    }
}

function Initialize-Spawner {
    param(
        [Parameter(Mandatory = $true)][string]$CycleId,
        [Parameter(Mandatory = $true)][string]$PromptFile,
        [string]$WorkspacePath,
        [string]$MarkerPath,
        [string]$PythonExe,
        [string[]]$Extra = @()
    )
    $script:SpawnCycle = $CycleId
    $script:SpawnPromptFile = $PromptFile
    $script:SpawnTarget = if ($WorkspacePath) { $WorkspacePath } else { (Get-Location).Path }
    if (-not $MarkerPath) {
        throw ("no -MarkerPath: 降级标记的落点由调用方给（conduct_spawn.argv_for 从 marker.py " +
               "取），spawner 不自拼这个路径")
    }
    $script:SpawnMarkerPath = $MarkerPath
    $script:SpawnExtra = @($Extra)
    $script:SpawnMarkerWritten = $false
    # `-PythonExe` 是调用方自己的解释器（argv_for 递下来），zcode 的中段用它跑 zcode-dispatch.py。
    # 写标记不走它：见文件头那条例外。DSH 那一半拿到这个参数也不用它。
    $script:SpawnPython = if ($PythonExe) { $PythonExe }
                          else { (Get-Command python -ErrorAction SilentlyContinue).Source }
}

function Assert-PromptFile {
    # 没有 prompt 就没有会话可开：写标记、当场报错退出，绝不发任何手势。
    # （两份脚本各自决定在哪一拍问这一句：zcode 在碰库之前，dsh 在建会话之后。）
    if (-not (Test-Path $script:SpawnPromptFile)) {
        Write-CallbackFailureMarker -Phase 'read-prompt' -Reason "找不到 prompt 文件 $script:SpawnPromptFile"
        throw "prompt file not found: $script:SpawnPromptFile (-PromptFile is required)"
    }
}

function Write-CallbackFailureMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Phase,
        [Parameter(Mandatory = $true)][string]$Reason,
        [int]$Attempts = 0
    )
    if ($script:SpawnMarkerWritten) { return }        # 一次失败只落一条：第一次的那条是根因
    $script:SpawnMarkerWritten = $true
    try {
        $record = [ordered]@{
            cycle_id    = $script:SpawnCycle
            phase       = $Phase
            attempts    = $Attempts
            reason      = $Reason
            workspace   = $script:SpawnTarget
            extra       = (ConvertTo-SpawnExtra $script:SpawnExtra)
            detected_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz')
            note        = $script:SpawnMarkerNote
        }
        $dir = Split-Path -Parent $script:SpawnMarkerPath
        if ($dir -and -not (Test-Path $dir)) { $null = New-Item -ItemType Directory -Force -Path $dir }
        if (Test-Path -LiteralPath $script:SpawnMarkerPath -PathType Container) {
            # 落点被一个同名目录占着时，Move-Item 会把临时文件搬进那个目录里、还当成成功 ——
            # 「标记在盘上」就成了半句谎话。这里当场认失败，交给下面那条 catch 说话。
            throw "落点是目录：$($script:SpawnMarkerPath)"
        }
        # 不带 BOM 写出：这份 JSON 是给工具读的，BOM 会让 json.load(encoding='utf-8') 炸。
        # 临时文件只在这一拍里有意义：改名成功它就不在了，重试耗尽也得由 finally 收走 ——
        # 标记目录里留一份 `.tmp-<pid>` 残渣，下一个人会当成半截事故来查。
        $tmp = "$($script:SpawnMarkerPath).tmp-$PID"
        try {
            [System.IO.File]::WriteAllText($tmp, ($record | ConvertTo-Json -Depth 5),
                                           (New-Object System.Text.UTF8Encoding($false)))
            Move-SpawnMarkerIntoPlace -From $tmp -To $script:SpawnMarkerPath
        }
        finally {
            if (Test-Path $tmp) { Remove-Item -Path $tmp -Force -ErrorAction SilentlyContinue }
        }
        Write-Warning "conduct 派发失败，已写下降级标记：$($script:SpawnMarkerPath)"
    }
    catch {
        # 写标记失败不许把派发失败变成更响的失败：警告一声，让调用方照原样退出。
        Write-Warning "写降级标记失败（$($script:SpawnMarkerPath)）：$($_.Exception.Message)"
    }
}

function Clear-CallbackFailureMarker {
    # 派发成功：清掉上一次留下的降级标记（标记存在 = 链现在是断的；链一旦续上它就没有意义了，
    # 留着只会让下一次人工 /pf-conduct 读到一条早就修好的旧事故）。
    # 清也先改名再删：标记从「在」到「不在」是一步同卷改名，并发读者撞不见中间态；改名失败
    # （正被别的进程读着）就退回原地删——清不掉不许挡派发成功。
    if (-not (Test-Path $script:SpawnMarkerPath)) { return }
    $stale = "$($script:SpawnMarkerPath).clearing-$PID"
    Move-Item -Path $script:SpawnMarkerPath -Destination $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $script:SpawnMarkerPath -Force -ErrorAction SilentlyContinue
    Write-Output "cleared a stale callback failure marker: $($script:SpawnMarkerPath)"
}