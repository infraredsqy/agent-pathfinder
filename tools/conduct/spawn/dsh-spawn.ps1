# ============================================================================
# dsh-spawn.ps1 — 用 DSH HTTP API 新开一个 session（管线派发用）
# ============================================================================
# 只由 conduct_spawn.py 调用（管线经 `state-manage.py next --spawn` 与 `dispatch <cycle>` 走它），
# 不直接给人用。只开一类会话：
#   带 -PromptFile：开一个**干活**的节点 session（拿一个 DAG 节点去做的那个 pf-conduct），prompt 就是那个文件的全文。
# `-PromptFile` 是**必填**：没有 prompt 就没有会话可开，不给就报错退出，绝不发任何手势。
# 旧 session 一律不复活——每次都是新开一个会话。
#
# 外壳（参数缺省、prompt 校验、降级标记的写与清）与 `zcode-spawn.ps1` 共用同一份：
# `spawn-common.ps1`，本文件只留 DSH 这一半（鉴权、HTTP 调用、末行）。标记的形状与落点在
# `marker.py`。
#
# Usage: pwsh -File dsh-spawn.ps1 -CycleId <id> -PromptFile <path> -MarkerPath <path>
#                                  [-WorkspacePath <dir>] [-PythonExe <path>] [-WebUrl <url>] [-Token <t>]
#   -CycleId       必填。cycle 标识（同 state-manage.py 的 cycle id）。
#   -PromptFile    必填。节点 session 的 prompt 全文（第一行是 skill 调用行）；缺了直接报错退出。
#   -MarkerPath    必填。降级标记的完整路径（`conduct_spawn.argv_for` 从 marker.py 取）。
#   -WorkspacePath 可选。子会话的工作目录（cwd）；缺省用当前目录。
#   -PythonExe     可选。共用参数面里的那一个（ZCode 那一半用它跑 zcode-dispatch.py）；写标记
#                  不经过 python（见 `spawn-common.ps1` 头部），DSH 这一半拿到它也不用。
#   -WebUrl        可选。DSH web 基址；缺省取 $env:DSH_BASE / $env:DSH_API / $env:DSH_WEB_URL，
#                  再退到 http://127.0.0.1:3080。
#   -Token         可选。显式进程 launch token（仅用于排障；正常路径不需要）。
#
# 依赖：pwsh 7+；DSH web 默认在 http://127.0.0.1:3080。
#
# 鉴权：
#   直接用**持久签名密钥**自签一个 browser-session cookie：密钥存在
#   ~/.dsh/.credentials.yaml 的 client-connection/browser-session 记录里，跨重启有效；
#   cookie 有效期 30 天（与 dsh 默认 cookieMaxAgeDays 一致）。/api 走的是 cookie 鉴权路径，
#   所以续跑不依赖任何一次性的进程态，contribute 子会话不需要用户介入。
#
# —— 契约（实测校准）——
#   ① 端点用斜杠形式：session/create、session/prompt（点号写法回 not found）
#   ② 信封：{type:"client-request", rpcId, method, payload:{args:{request:{…}}}}
#      端点参数放在 args 下的具名键里（这两个端点都叫 request）
#   ③ 无鉴权时回 401 "dsh web authentication required"；鉴权通过但方法名不存在回 404
#
# 链断了怎么办：
#   整条链的下一棒全押在这一段 POST 上：**一次 401 或网络抖动就足以让这条线静默躺下**，
#   表面上还像在跑。所以每个 API 调用最多重试 3 次（指数退避 2s / 4s）；
#   全部失败时写下降级标记（`spawn-common.ps1` + `marker.py`：一个显眼的、按线命名的文件），
#   下一次人工 /pf-conduct 能看见"这里断过"。派发成功时该标记会被清掉（它存在 = 链现在是断的）。
#   按线分文件：并行=2 时两条线并排跑，本线成功只清本线的标记，不抹别线的断链记号。
# ============================================================================

param(
    [Parameter(Mandatory = $true)][string]$CycleId,
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [string]$MarkerPath,
    [string]$WorkspacePath,
    [string]$PythonExe,
    [string]$WebUrl,
    [string]$Token
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'spawn-common.ps1')

# —— API 根：-WebUrl > DSH_BASE > DSH_API（剥掉结尾 /api） > DSH_WEB_URL > 默认 ——
$base = if ($WebUrl) { $WebUrl.TrimEnd('/') }
        elseif ($env:DSH_BASE) { ($env:DSH_BASE).TrimEnd('/') }
        elseif ($env:DSH_API) { (($env:DSH_API) -replace '/api/?$', '').TrimEnd('/') }
        elseif ($env:DSH_WEB_URL) { ($env:DSH_WEB_URL).TrimEnd('/') }
        else { "http://127.0.0.1:3080" }

# —— 工作目录与降级标记：在任何网络调用之前先定下来，失败时才写得了盘 ——
Initialize-Spawner -CycleId $CycleId -PromptFile $PromptFile -WorkspacePath $WorkspacePath `
                   -MarkerPath $MarkerPath -PythonExe $PythonExe -Extra "api_base=$base"

$script:MaxAttempts = 3
$script:RetryBaseDelaySec = 2

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

try {
    $authority = ([uri]$base).Authority
}
catch {
    # 基址本身不成形：链在这一拍就断了，和签 cookie / POST 失败同一类事故 —— 一样留标记再退出。
    Write-CallbackFailureMarker -Phase 'auth' -Reason $_.Exception.Message
    throw
}
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
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Request
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
# （$script:SpawnTarget 已在前面按 -WorkspacePath / 当前目录定好，降级标记也用它。）
$workspace = (Invoke-DshApi -Method "workspace/create" -Request @{ path = $script:SpawnTarget }).workspace
$created = Invoke-DshApi -Method "session/create" -Request @{ workspaceId = $workspace.workspaceId }
$sid = $created.sessionId

# —— 发送 prompt（mode:"queue"）——
# 只发 -PromptFile 的全文（节点 session 的 prompt，第一行是 skill 调用）。没给就是调用方的缺陷：
# 报错退出，不建任何手势（会话一律由 prompt 定义，没有第二种开法）。
Assert-PromptFile
$text = [System.IO.File]::ReadAllText($script:SpawnPromptFile, [System.Text.Encoding]::UTF8)
$what = "node session (prompt=$($script:SpawnPromptFile))"
Invoke-DshApi -Method "session/prompt" -Request @{
    requestId = [guid]::NewGuid().ToString()
    sessionId = $sid
    mode      = "queue"
    content   = @(@{ type = "text"; text = $text })
} | Out-Null

# 派发成功：清掉上一次留下的降级标记（标记存在 = 链现在是断的）。
Clear-CallbackFailureMarker

Write-Output "spawned: session $sid ($what)"