# ============================================================================
# dsh-spawn.ps1 — open a new session over the DSH HTTP API (used by pipeline dispatch)
# ============================================================================
# Called only by conduct_spawn.py (the pipeline reaches it through `state-manage.py next --spawn` and
# `dispatch <cycle>`), never used by a human directly. It opens one kind of session:
#   with -PromptFile: opens a **working** node session (the pf-conduct that takes one DAG node on), and
#   the prompt is the whole of that file.
# `-PromptFile` is **mandatory**: with no prompt there is no session to open, and without it the script
# fails and exits, never sending a gesture.
# An old session is never revived -- a new session is opened every time.
#
# The shell (parameter defaults, the prompt check, the write and the clear of the degradation marker)
# is shared with `zcode-spawn.ps1`: `spawn-common.ps1`; this file keeps only the DSH half
# (authentication, the HTTP calls, the final line). The marker's shape and landing place live in
# `marker.py`.
#
# Usage: pwsh -File dsh-spawn.ps1 -CycleId <id> -PromptFile <path> -MarkerPath <path>
#                                  [-WorkspacePath <dir>] [-PythonExe <path>] [-WebUrl <url>] [-Token <t>]
#   -CycleId       mandatory. The cycle id (the same as state-manage.py's cycle id).
#   -PromptFile    mandatory. The whole prompt of the node session (its first line is the skill call
#                  line); without it the script fails and exits.
#   -MarkerPath    mandatory. The degradation marker's full path (`conduct_spawn.argv_for` takes it
#                  from marker.py).
#   -WorkspacePath optional. The child session's working directory (cwd); defaults to the current
#                  directory.
#   -PythonExe     optional. The one parameter on the shared surface (the ZCode half uses it to run
#                  zcode-dispatch.py); writing the marker does not go through python (see the head of
#                  `spawn-common.ps1`), and the DSH half takes it without using it either.
#   -WebUrl        optional. The DSH web base URL; defaults to $env:DSH_BASE / $env:DSH_API /
#                  $env:DSH_WEB_URL, then falls back to http://127.0.0.1:3080.
#   -Token         optional. An explicit process launch token (for troubleshooting only; the normal
#                  path does not need it).
#
# Dependencies: pwsh 7+; DSH web serves http://127.0.0.1:3080 by default.
#
# Authentication:
#   Sign a browser-session cookie directly with the **persistent signing key**: the key lives in the
#   client-connection/browser-session record of ~/.dsh/.credentials.yaml and stays valid across
#   restarts; the cookie is valid for 30 days (matching dsh's default cookieMaxAgeDays). /api goes
#   through the cookie authentication path, so carrying on depends on no one-off process state and
#   contribute child sessions need no user involvement.
#
# -- Contract (calibrated against a live run) --
#   1) Endpoints use the slash form: session/create, session/prompt (the dot form answers not found)
#   2) Envelope: {type:"client-request", rpcId, method, payload:{args:{request:{...}}}}
#      the endpoint parameters sit in named keys under args (both of these endpoints call it request)
#   3) With no authentication it answers 401 "dsh web authentication required"; when authentication
#      passes but the method name does not exist it answers 404
#
# What to do when the chain breaks:
#   the whole chain's next step rests on this one POST: **a single 401 or a network blip is enough to
#   make this line lie down silently**, while on the surface it still looks like it is running. So
#   every API call retries at most 3 times (exponential backoff 2s / 4s); when all of them fail it
#   writes the degradation marker (`spawn-common.ps1` + `marker.py`: one conspicuous file named after
#   the line), so the next manual /pf-conduct can see "this broke here". On a successful dispatch that
#   marker is cleared (its presence = the chain is broken right now).
#   One file per line: with parallel=2 two lines run side by side, and this line's success clears only
#   this line's marker, never another line's broken-chain mark.
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

# -- API root: -WebUrl > DSH_BASE > DSH_API (strip the trailing /api) > DSH_WEB_URL > the default --
$base = if ($WebUrl) { $WebUrl.TrimEnd('/') }
        elseif ($env:DSH_BASE) { ($env:DSH_BASE).TrimEnd('/') }
        elseif ($env:DSH_API) { (($env:DSH_API) -replace '/api/?$', '').TrimEnd('/') }
        elseif ($env:DSH_WEB_URL) { ($env:DSH_WEB_URL).TrimEnd('/') }
        else { "http://127.0.0.1:3080" }

# -- working directory and degradation marker: settle them before any network call, so a failure can
# still write to disk --
Initialize-Spawner -CycleId $CycleId -PromptFile $PromptFile -WorkspacePath $WorkspacePath `
                   -MarkerPath $MarkerPath -PythonExe $PythonExe -Extra "api_base=$base"

$script:MaxAttempts = 3
$script:RetryBaseDelaySec = 2

# -- authentication: sign our own browser-session cookie (valid across restarts) --
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

    # 1) take the persistent signing key (32 bytes, base64url, in the credentials file)
    $credPath = Join-Path $env:USERPROFILE '.dsh\.credentials.yaml'
    if (-not (Test-Path $credPath)) {
        throw "credentials file not found at $credPath -- check that dsh web has started successfully at least once"
    }
    $cred = Get-Content $credPath -Raw
    if ($cred -notmatch 'client-connection/browser-session:[\s\S]*?secret:\s*([A-Za-z0-9_\-]+)') {
        throw "the credentials file has no client-connection/browser-session record -- check that dsh web is running"
    }
    $secretBytes = ConvertFrom-B64Url $Matches[1]
    if ($secretBytes.Length -ne 32) {
        throw "the signing key has an odd length ($($secretBytes.Length) bytes, expected 32)"
    }

    # 2) cookie name = dsh-auth- + base64url(sha256(authority))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $name = 'dsh-auth-' + (ConvertTo-B64Url ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Authority))))

    # 3) cookie value = v1.<base64url(json payload)>.<base64url(hmac_sha256(secret, body))>
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
    # The base URL itself is malformed: the chain breaks at this beat, the same class of accident as a
    # failed cookie signing / POST -- leave the marker the same way and exit.
    Write-CallbackFailureMarker -Phase 'auth' -Reason $_.Exception.Message
    throw
}
$cookieHeader = $null
try {
    $cookieHeader = if ($Token) {
        # Troubleshooting path: with an explicit launch token, exchange it for a cookie first, then take back
        # the signing cookie for this call
        $null = Invoke-WebRequest -Uri "$base/?token=$Token" -SessionVariable sess -MaximumRedirection 5 -TimeoutSec 20
        ($sess.Cookies.GetCookies([uri]$base) | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join '; '
    } else {
        New-BrowserSessionCookie -Authority $authority
    }
}
catch {
    # A failed cookie signing is as fatal as a failed POST: the chain breaks here the same way, so it
    # leaves the marker the same way.
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
    # The parameter name avoids PowerShell's automatic $Args variable; payload must hold exactly one
    # plain object, args, and the endpoint parameters sit in named keys under args (session/create and
    # session/prompt both call it request).
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
                # The bounded retry is used up: land the reason on disk (write the first one only; later calls do
                # not overwrite it).
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

# -- open a conduct session: take the workspace by directory first, then create the session with only
# workspaceId --
# Passing cwd alone also creates a session (it lands in the right place), but it is **not attached to
# the workspace group**, so it scatters in the GUI's "ungrouped" area and the user cannot see where the
# pipeline has got to. So take workspaceId with workspace/create first (idempotent: the same directory
# returns the existing workspace), then create the session with only that -- the endpoint explicitly
# refuses workspaceId and cwd together, and when only workspaceId is given the session's cwd is the
# directory that workspace is bound to.
# ($script:SpawnTarget was already settled above from -WorkspacePath / the current directory, and the
# degradation marker uses it too.)
$workspace = (Invoke-DshApi -Method "workspace/create" -Request @{ path = $script:SpawnTarget }).workspace
$created = Invoke-DshApi -Method "session/create" -Request @{ workspaceId = $workspace.workspaceId }
$sid = $created.sessionId

# -- send the prompt (mode:"queue") --
# Send only the full text of -PromptFile (the node session's prompt, first line the skill call).
# Not giving it is the caller's defect: fail and exit, do not build any gesture (a session is always
# defined by its prompt; there is no second way to open one).
Assert-PromptFile
$text = [System.IO.File]::ReadAllText($script:SpawnPromptFile, [System.Text.Encoding]::UTF8)
$what = "node session (prompt=$($script:SpawnPromptFile))"
Invoke-DshApi -Method "session/prompt" -Request @{
    requestId = [guid]::NewGuid().ToString()
    sessionId = $sid
    mode      = "queue"
    content   = @(@{ type = "text"; text = $text })
} | Out-Null

# Dispatch succeeded: clear the degradation marker left by an earlier beat (marker present = the chain
# is broken right now).
Clear-CallbackFailureMarker

Write-Output "spawned: session $sid ($what)"