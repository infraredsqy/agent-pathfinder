# ============================================================================
# zcode-spawn.ps1 — queue one node session into ZCode's own cron scheduler (used by pipeline dispatch)
# ============================================================================
# Called only by conduct_spawn.py (the pipeline reaches it through `state-manage.py next --spawn` and
# `dispatch <cycle>`). It and dsh-spawn.ps1 are two implementations of one place: that one opens a DSH
# session directly over the DSH HTTP API, this one writes a row into the `automations` table of
# `~/.zcode/v2/tasks-index.sqlite` and leaves it to the desktop's own scheduler (one round every 20
# seconds) to open the session.
#
# Why take this detour: only a session the GUI itself created enters its task list (that list's source
# is a projection seeded once in the agent host process, and a local workspace has no re-read entry),
# and watching progress, calling a stop and letting work through all lean on that list. For the detail
# see zcode-dispatch.py's module docstring.
#
# The shell (parameter defaults, the prompt check, the write and the clear of the degradation marker)
# is shared with `dsh-spawn.ps1`: `spawn-common.ps1`; this file keeps only the ZCode half (find the
# dispatcher, queue that row, read the result back, the final line). The marker's shape and landing
# place live in `marker.py` (ZCode's own library path goes into the marker's `extra`).
#
# Usage: pwsh -File zcode-spawn.ps1 -CycleId <id> -PromptFile <path> -MarkerPath <path>
#                                  [-WorkspacePath <dir>] [-PythonExe <path>]
#                                  [-At <when>] [-Mode <mode>] [-Model <pick>] [-NoVerify]
#   -CycleId       mandatory. The cycle id (the same as state-manage.py's cycle id).
#   -PromptFile    mandatory. The whole prompt of the node session (its first line is the skill call
#                  line); without it the script fails and exits.
#   -MarkerPath    mandatory. The degradation marker's full path (`conduct_spawn.argv_for` takes it
#                  from marker.py).
#   -WorkspacePath optional. The child session's working directory, and the grouping key in the GUI;
#                  defaults to the current directory.
#   -PythonExe     optional. The python that runs zcode-dispatch.py; defaults to the python on PATH
#                  (writing the marker does not go through it).
#   -At            optional. When to run: `+30m` / `+2h` / a local clock time (such as
#                  `2026-10-07T23:00`). Use it to queue a node into the free window at night;
#                  default = immediately. The env var `DSH_CONDUCT_SPAWN_AT` means the same (the
#                  pipeline's argv does not carry this parameter).
#   -Mode          optional. The session's permission tier: `yolo` (default, full access) / `build` /
#                  `edit` / `plan`. **Do not change it to a tier that asks questions**: a working
#                  session stops and waits for an answer the first time it changes a file, and nobody is
#                  there at that moment. The env var `DSH_CONDUCT_SPAWN_MODE` means the same.
#   -Model         optional. Which model the session runs: `provider/model$level` (the exact string from
#                  the GUI's model picker). The default comes from the switch in zcode-dispatch.py:
#                  among `MODEL_CHOICES`'s two keys (`glm` = the built-in Zhipu account's
#                  GLM-5.3-Flash / `opencode` = OpenCode Go (Chat)'s deepseek-v4.1-flash), whichever
#                  `DEFAULT_MODEL` points at. To change models systematically: change the key on the
#                  `DEFAULT_MODEL` line, do not hard-code a second copy elsewhere. Writing `workspace`
#                  = follow that workspace's preference, that is, whatever you last picked in the
#                  interface -- do not use it for background nodes: those sessions' spend lands on
#                  whatever quota was selected in your interface at the time. Legal ids: `modelOrder`
#                  in `~/.zcode/v2/provider_config.json` and the model list in the built-in catalogue
#                  (account-type providers' model names are case-sensitive). The env var
#                  `DSH_CONDUCT_SPAWN_MODEL` means the same and beats the default; the host process
#                  snapshots it at startup, so changing the env var needs a host restart to take effect.
#                  Note: once it is set it covers the switch above, and to let the switch decide you have
#                  to clear it first (including the persistent copy under HKCU\Environment).
#   -NoVerify      optional. Return as soon as that row is written, without waiting for the desktop to
#                  create the session. For drills and scheduling; the pipeline does not pass it, because
#                  "queued successfully" and "the session really exists" are two different things -- if
#                  it cannot wait it writes the marker down.
#
# Dependencies: pwsh 7+, python 3 on PATH, the ZCode desktop running. When the desktop is closed, a
# queued row is only seen once it starts, and if by then it is more than 5 minutes overdue the scheduler
# skips it as a misfire -- zcode-dispatch.py says a word on stderr in that case.
#
# The shared contract with dsh-spawn.ps1 (do not change):
#   1) on success the last line is `spawned: ...`;
#   2) any failure writes the degradation marker and does not clear it;
#   3) on success it clears this line's own stale marker (marker present = the chain is broken right
#      now); one file per line, with parallel=2 this line's success does not wipe another line's
#      broken-chain mark.
# The final line is printed by this file itself (the two hosts' final lines differ); items 2) and 3)
# are guaranteed by the one shell in `spawn-common.ps1` -- this half's own "no python found" beat also
# writes the degradation marker per 2) and exits.
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

# -- working directory and degradation marker: settle them before any call, so a failure can write to
# disk --
# ZCode's library path goes only into the marker's extra, for a human to read; the real library path is
# computed by zcode-dispatch.py (it honours one env var, and the sandbox points `ZCODE_DATA_BASE_DIR`
# at a temp library).
$dataBase = if ($env:ZCODE_DATA_BASE_DIR) { $env:ZCODE_DATA_BASE_DIR } else { $env:USERPROFILE }
$dbPath = Join-Path $dataBase '.zcode\v2\tasks-index.sqlite'
Initialize-Spawner -CycleId $CycleId -PromptFile $PromptFile -WorkspacePath $WorkspacePath `
                   -MarkerPath $MarkerPath -PythonExe $PythonExe -Extra "db=$dbPath"

# -- the prompt is the whole of this call: with no prompt there is no session to queue, and without it
# the script fails and exits --
Assert-PromptFile

$python = $script:SpawnPython
if (-not $python) {
    # The chain breaks at this beat: write the degradation marker per contract 2) and exit -- the marker
    # is written by the shell itself, which needs no python.
    Write-CallbackFailureMarker -Phase 'find-python' `
        -Reason "no python: zcode-dispatch.py cannot run; put it on PATH, or name one with -PythonExe."
    Write-Warning "zcode-dispatch.py needs python: put it on PATH, or name one with -PythonExe."
    throw "python not found (zcode-dispatch.py needs it)"
}

$dispatcher = Join-Path $PSScriptRoot 'zcode-dispatch.py'
if (-not (Test-Path $dispatcher)) {
    Write-CallbackFailureMarker -Phase 'find-dispatcher' -Reason "cannot find $dispatcher"
    throw "dispatcher not found: $dispatcher"
}

# -- queue a row; `--at` / `--mode` / `--model` honour only explicit parameters and env vars (the
# pipeline's argv does not carry them) --
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

# Success path: the first line on stdout is the automation id, and when verification ran the second
# line is `session: <id>` (warnings go to stderr and do not match these two shapes even if they mix in)
$automationId = ($lines | Where-Object { "$_" -match '^automation-' } | Select-Object -Last 1)
if (-not $automationId) {
    $detail = "zcode-dispatch.py printed no automation id: " + (($lines | ForEach-Object { "$_" }) -join ' | ')
    Write-CallbackFailureMarker -Phase 'read-result' -Reason $detail
    throw $detail
}
$sessionId = ''
# `sess_` is the shape the desktop used to mint; since v3.16 host-created ids read `session_...`,
# so the match stops after the shared prefix.
$sessionLine = ($lines | Where-Object { "$_" -match '^session:\s*sess' } | Select-Object -Last 1)
if ($sessionLine) { $sessionId = ("$sessionLine" -replace '^session:\s*', '').Trim() }

# Dispatch succeeded: clear the degradation marker left by an earlier beat (marker present = the chain
# is broken right now).
Clear-CallbackFailureMarker

if ($sessionId) {
    Write-Output "spawned: automation $automationId (session $sessionId, node session, prompt=$($script:SpawnPromptFile))"
} else {
    Write-Output "spawned: automation $automationId (node session, prompt=$($script:SpawnPromptFile))"
}