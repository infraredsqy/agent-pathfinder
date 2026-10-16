# ============================================================================
# dsh-spawn.ps1 — 用 DSH HTTP API 新开一个 session（管线派发用）
# ============================================================================
# 只由 conduct_spawn.py 调用（管线经 `state-manage.py next --spawn` 与 `dispatch <cycle>` 走它），
# 不直接给人用。只开一类会话：
#   带 -PromptFile：开一个**干活**的节点 session（拿一个 DAG 节点去做的那个 pf-conduct），prompt 就是那个文件的全文。
# `-PromptFile` 是**必填**：没有 prompt 就没有会话可开，不给就报错退出，绝不发任何手势。
# 旧 session 一律不复活——每次都是新开一个会话。
#
# Usage: pwsh -File dsh-spawn.ps1 -CycleId <id> -PromptFile <path>
#                                  [-WorkspacePath <dir>] [-WebUrl <url>] [-Token <t>]
#   -CycleId       必填。cycle 标识（同 state-manage.py 的 cycle id）。
#   -PromptFile    必填。节点 session 的 prompt 全文（第一行是 skill 调用行）；缺了直接报错退出。
#   -WorkspacePath 可选。子会话的工作目录（cwd）；缺省用当前目录。
#   -WebUrl        可选。DSH web 基址；缺省取 $env:DSH_BASE / $env:DSH_API / $env:DSH_WEB_URL，
#                  再退到 http://127.0.0.1:3080。
#   -Token         可选。显式进程 launch token（仅用于排障；正常路径不需要）。
#
# 依赖：pwsh 7+；DSH web 默认在 http://127.0.0.1:3080。
#
# 鉴权（2026-09-20 重做，原实现已失效）：
#   旧实现从 ~/.dsh/web-run.log 抓进程 launch token，再走 GET /?token=… 换 cookie。
#   两个问题：① launch token 每进程新生成，日志不随每次启动更新，重启后必然 401；
#   ② /api 走的是 cookie 鉴权路径，launch token 只是换 cookie 的门票。
#   现改为直接用**持久签名密钥**自签一个 browser-session cookie：
#   密钥存在 ~/.dsh/.credentials.yaml 的 client-connection/browser-session 记录里，
#   跨重启有效；cookie 有效期 30 天（与 dsh 默认 cookieMaxAgeDays 一致）。
#   于是续跑不再依赖任何一次性的进程态，contribute 子会话也不再需要用户介入。
#
# —— 契约（2026-09 按 0.1.5-rc.2 实测校准，仍然成立）——
#   ① 端点用斜杠形式：session/create、session/prompt（点号写法回 not found）
#   ② 信封：{type:"client-request", rpcId, method, payload:{args:{request:{…}}}}
#      端点参数放在 args 下的具名键里（这两个端点都叫 request）
#   ③ 无鉴权时回 401 "dsh web authentication required"；鉴权通过但方法名不存在回 404
#
# 链断了怎么办（2026-09-20 加固）：
#   整条链的下一棒全押在这一段 POST 上：**一次 401 或网络抖动就足以让这条线静默躺下**，
#   表面上还像在跑（旧实现没有重试）。所以每个 API 调用最多重试 3 次（指数退避 2s / 4s）；
#   全部失败时把原因写进 .pf/meta/CONDUCT-CALLBACK-FAILED-<cycle>.json —— 一个显眼的降级标记，
#   下一次人工 /pf-conduct 能看见"这里断过"。派发成功时该标记会被清掉（它存在 = 链现在是断的）。
#   按线分文件：并行=2 时两条线并排跑，本线成功只清本线的标记，不抹别线的断链记号。
# ============================================================================

param(
    [Parameter(Mandatory = $true)][string]$CycleId,
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [string]$WorkspacePath,
    [string]$WebUrl,
    [string]$Token
)

$ErrorActionPreference = "Stop"

# —— API 根：-WebUrl > DSH_BASE > DSH_API（剥掉结尾 /api） > DSH_WEB_URL > 默认 ——
$base = if ($WebUrl) { $WebUrl.TrimEnd('/') }
        elseif ($env:DSH_BASE) { ($env:DSH_BASE).TrimEnd('/') }
        elseif ($env:DSH_API) { (($env:DSH_API) -replace '/api/?$', '').TrimEnd('/') }
        elseif ($env:DSH_WEB_URL) { ($env:DSH_WEB_URL).TrimEnd('/') }
        else { "http://127.0.0.1:3080" }

# —— 工作目录与降级标记：在任何网络调用之前先定下来，失败时才写得了盘 ——
$target = if ($WorkspacePath) { $WorkspacePath } else { (Get-Location).Path }
$script:MarkerPath = Join-Path $target ('.pf\meta\CONDUCT-CALLBACK-FAILED-' + $CycleId + '.json')

$script:MaxAttempts = 3
$script:RetryBaseDelaySec = 2
$script:MarkerWritten = $false

function Write-CallbackFailureMarker {
    param(
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Reason,
        [int]$Attempts = 0
    )
    if ($script:MarkerWritten) { return }
    $script:MarkerWritten = $true
    try {
        $dir = Split-Path -Parent $script:MarkerPath
        if (-not (Test-Path $dir)) { $null = New-Item -ItemType Directory -Force -Path $dir }
        $record = [ordered]@{
            cycle_id    = $CycleId
            phase       = $Phase
            attempts    = $Attempts
            reason      = $Reason
            workspace   = $target
            api_base    = $base
            detected_at = [DateTimeOffset]::UtcNow.ToString('o')
            note        = ('conductor 的下一棒断在这里：子 session 已写完 signal，但没有新的 conduct ' +
                           'session 被开起来，管线不会再自己往前走。手动跑 state-manage.py next ' +
                           '接管；确认无碍后删掉本文件。')
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

# —— 鉴权：自签 browser-session cookie（跨重启有效）——
function ConvertTo-B64Url([byte[]]$Bytes) {
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}
function ConvertFrom-B64Url([string]$Value) {
    $t = $Value.Replace('-', '+').Replace('_', '/')
    switch ($t.Length % 4) { 2 { $t += '==' } 3 { $t += '=' } }
    return , ([Convert]::FromBase64String($t))
}

function New-BrowserSessionCookie {
    param([Parameter(Mandatory)][string]$Authority)

    # 1) 取持久签名密钥（32 字节，base64url 存于凭据文件）
    $credPath = Join-Path $env:USERPROFILE '.dsh\.credentials.yaml'
    if (-not (Test-Path $credPath)) {
        throw "找不到凭据文件 $credPath —— 确认 dsh web 至少成功启动过一次"
    }
    $cred = Get-Content $credPath -Raw
    if ($cred -notmatch 'client-connection/browser-session:[\s\S]*?secret:\s*([A-Za-z0-9_\-]+)') {
        throw "凭据文件里没有 client-connection/browser-session 记录 —— 确认 dsh web 正在运行"
    }
    $secretBytes = ConvertFrom-B64Url $Matches[1]
    if ($secretBytes.Length -ne 32) {
        throw "签名密钥长度异常（$($secretBytes.Length) 字节，应为 32）"
    }

    # 2) cookie 名 = dsh-auth- + base64url(sha256(authority))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $name = 'dsh-auth-' + (ConvertTo-B64Url ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Authority))))

    # 3) cookie 值 = v1.<base64url(json payload)>.<base64url(hmac_sha256(secret, body))>
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $payload = [ordered]@{
        version   = 1
        authority = $Authority
        issuedAt  = $now
        expiresAt = $now + (30L * 24 * 3600 * 1000)
    }
    $body = ConvertTo-B64Url ([Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress)))
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($secretBytes)
    $sig = ConvertTo-B64Url ($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($body)))

    return "$name=v1.$body.$sig"
}

$authority = ([uri]$base).Authority
$cookieHeader = $null
try {
    $cookieHeader = if ($Token) {
        # 排障路径：显式给了 launch token 就先换 cookie，再取回签名 cookie 供本次调用使用
        $null = Invoke-WebRequest -Uri "$base/?token=$Token" -SessionVariable sess -MaximumRedirection 5 -TimeoutSec 20
        ($sess.Cookies.GetCookies([uri]$base) | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join '; '
    } else {
        New-BrowserSessionCookie -Authority $authority
    }
}
catch {
    # 签 cookie 失败和 POST 失败一样致命：链一样断在这里，所以一样留标记。
    Write-CallbackFailureMarker -Phase 'auth' -Reason $_.Exception.Message
    throw
}

$script:RpcCounter = 0

function Invoke-DshApi {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][AllowEmptyCollection()]$Request
    )
    $script:RpcCounter++
    # 参数名避开 PowerShell 的自动变量 $Args；payload 必须恰好含一个普通对象 args，
    # 端点参数放在 args 下的具名键里（session/create 与 session/prompt 都叫 request）。
    $envelope = @{
        type    = "client-request"
        rpcId   = "cb$($script:RpcCounter)-$PID"
        method  = $Method
        payload = @{ args = @{ request = $Request } }
    }
    $bodyJson = $envelope | ConvertTo-Json -Depth 30 -Compress
    for ($attempt = 1; $attempt -le $script:MaxAttempts; $attempt++) {
        try {
            $resp = Invoke-RestMethod -Uri "$base/api/$Method" -Method Post `
                -ContentType "application/json; charset=utf-8" -Body $bodyJson `
                -Headers @{ Cookie = $cookieHeader } -TimeoutSec 30
            $result = $resp.result
            if (-not $result.ok) {
                throw "API $Method returned error: $($result.error | ConvertTo-Json -Depth 10 -Compress)"
            }
            return $result.value
        }
        catch {
            $detail = $_.ErrorDetails.Message
            if (-not $detail) { $detail = $_.Exception.Message }
            if ($attempt -ge $script:MaxAttempts) {
                # 有界重试用尽：把原因落到磁盘上（只写第一次，后面的调用不再覆盖它）。
                Write-CallbackFailureMarker -Phase "POST /api/$Method" -Reason $detail -Attempts $attempt
                throw "API $Method failed after $attempt attempt(s): $detail"
            }
            $delay = $script:RetryBaseDelaySec * [math]::Pow(2, $attempt - 1)
            Write-Warning ("API {0} failed (attempt {1}/{2}): {3} -- retrying in {4}s" -f `
                $Method, $attempt, $script:MaxAttempts, $detail, $delay)
            Start-Sleep -Seconds $delay
        }
    }
}

# —— 新开 conduct session：先按目录拿工作区，再只带 workspaceId 建会话 ——
# 只传 cwd 也能建出会话（落盘位置正确），但**不会挂进工作区分组**，在 GUI 里会散落在
# 「未分组」区，用户看不到管线跑到哪了。所以先用 workspace/create（幂等：同一目录返回
# 既有工作区）取 workspaceId，再只带它建会话——端点明确拒绝 workspaceId 与 cwd 同时出现，
# 而只给 workspaceId 时，会话的 cwd 即该工作区绑定的目录。
# （$target 已在前面按 -WorkspacePath / 当前目录定好，降级标记也用它。）
$workspace = (Invoke-DshApi -Method "workspace/create" -Request @{ path = $target }).workspace
$created = Invoke-DshApi -Method "session/create" -Request @{ workspaceId = $workspace.workspaceId }
$sid = $created.sessionId

# —— 发送 prompt（mode:"queue"）——
# 只发 -PromptFile 的全文（节点 session 的 prompt，第一行是 skill 调用）。没给就是调用方的缺陷：
# 报错退出，不建任何手势（会话一律由 prompt 定义，没有第二种开法）。
if (-not (Test-Path $PromptFile)) {
    Write-CallbackFailureMarker -Phase 'read-prompt' -Reason "找不到 prompt 文件 $PromptFile"
    throw "prompt file not found: $PromptFile (-PromptFile is required)"
}
$text = [System.IO.File]::ReadAllText($PromptFile, [System.Text.Encoding]::UTF8)
$what = "node session (prompt=$PromptFile)"
Invoke-DshApi -Method "session/prompt" -Request @{
    requestId = [guid]::NewGuid().ToString()
    sessionId = $sid
    mode      = "queue"
    content   = @(@{ type = "text"; text = $text })
} | Out-Null

# 派发成功：清掉上一次留下的降级标记（标记存在 = 链现在是断的；链一旦续上它就没有意义了，
# 留着只会让下一次人工 /pf-conduct 读到一条早就修好的旧事故）。
if (Test-Path $script:MarkerPath) {
    # 清也先改名再删：标记从「在」到「不在」是一步同卷改名，并发读者撞不见中间态；改名失败
    # （正被别的进程读着）就退回原地删——清不掉不许挡派发成功。
    $stale = "$script:MarkerPath.clearing-$PID"
    Move-Item -Path $script:MarkerPath -Destination $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $script:MarkerPath -Force -ErrorAction SilentlyContinue
    Write-Output "cleared a stale callback failure marker: $script:MarkerPath"
}

Write-Output "spawned: session $sid ($what)"
