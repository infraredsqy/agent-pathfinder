# ============================================================================
# spawn-common.ps1 — the shell both spawners share (dot-source it, never run it)
# ============================================================================
# dsh-spawn.ps1 and zcode-spawn.ps1 differ only in the middle section (talk to the DSH HTTP API /
# queue a row in ZCode's cron table); the part of the shell that is line-for-line isomorphic lives in
# this one source: the default working directory, the prompt file check, the write and the clear of
# the degradation marker, and what to say on failure. What stays in each script is the host
# difference: the param block (zcode adds -At/-Mode/-Model/-NoVerify), the middle section, the final
# `spawned:` line.
#
# The degradation marker's **shape and landing place** have one source: `marker.py` next to this file
# (the python side and the note step use it). This layer **writes its own** against the same contract,
# and that is an exception for a physical reason: the machine and the moment the spawner runs on may
# have no python, and PowerShell cannot import a python module -- and this write is exactly the only
# way in left to a human after the chain breaks, so it must not disappear because the interpreter is
# absent. The price is two implementations of the shape, so `conduct/tests/cases_spawn_shell.py`'s
# `TestTheSpawnerShell` pins the two sides together with one equivalence case: the same key set, the
# same sentence, the same moment shape, the same landing place (`-MarkerPath`), a second failure that
# does not overwrite, and gone after the clear. Change the shape and change both; the case goes red on
# the spot.
#
# Clearing the marker does not take this road: deleting a file has no shape to speak of, and the
# success beat must not fail because the clear failed.
#
# `-MarkerPath` is mandatory: the caller computes where the marker lands (`conduct_spawn.argv_for`
# takes it from `marker.py`), and the spawner does not assemble that path itself -- rename the ledger
# and the places that assemble it would quietly write where no tool reads.
#
# `$script:` here means **the calling script's scope**: dot-sourcing gives both scripts and this layer
# one script scope, which is how a spawner hands its state to these functions (`Initialize-Spawner`
# writes, the other functions read).
#
# This file has to be readable by Windows PowerShell 5.1 (the spawner on an installed machine and
# troubleshooting may both be it), so the syntax stops at the 5.1 set; the file itself stays UTF-8
# with BOM, because 5.1 reads a BOM-less file in the console codepage
# (`cases_suite.py`'s `TestShippedPowerShellCarriesItsBom` requires the BOM).
# ============================================================================

$script:SpawnCommonDir = $PSScriptRoot

#: the sentence the marker writes itself -- character for character `marker.py`'s `DEFAULT_NOTE`; the
#: equivalence case watches these two files.
$script:SpawnMarkerNote = ('The chain broke here: this step could not start the next one, and the pipeline will not move on by ' +
                           'itself. To take over by hand use `pf next --after <node_id>` (the closing step) or ' +
                           '`pf dispatch <cycle>` (the dispatch step); once you have checked that nothing is wrong, ' +
                           'delete this file.')

function ConvertTo-SpawnExtra {
    # Parse the `KEY=VALUE` list (how a caller passes host facts) into an object: dsh passes
    # `api_base`, zcode passes `db`.
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
    # A temp file plus a same-volume rename, with a bounded retry on the rename: Windows refuses while
    # a reader has the target file open at this moment, and this is exactly the write that must not be
    # lost (the same reason and the same count as `tools/shared/jsonio.py`'s `replace_with_retry`).
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
        throw ("no -MarkerPath: the caller gives where the degradation marker lands " +
               "(conduct_spawn.argv_for takes it from marker.py); the spawner does not assemble " +
               "that path itself")
    }
    $script:SpawnMarkerPath = $MarkerPath
    $script:SpawnExtra = @($Extra)
    $script:SpawnMarkerWritten = $false
    # `-PythonExe` is the caller's own interpreter (argv_for passes it down); zcode's middle
    # section uses it to run zcode-dispatch.py. Writing the marker does not go through it: see the
    # exception in the file header. The DSH half takes this parameter and does not use it either.
    $script:SpawnPython = if ($PythonExe) { $PythonExe }
                          else { (Get-Command python -ErrorAction SilentlyContinue).Source }
}

function Assert-PromptFile {
    # With no prompt there is no session to open: write the marker, fail on the spot, never send
    # a gesture. (Each of the two scripts decides which beat asks this: zcode before it touches the
    # db, dsh after it creates the session.)
    if (-not (Test-Path $script:SpawnPromptFile)) {
        Write-CallbackFailureMarker -Phase 'read-prompt' -Reason "prompt file not found: $script:SpawnPromptFile"
        throw "prompt file not found: $script:SpawnPromptFile (-PromptFile is required)"
    }
}

function Write-CallbackFailureMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Phase,
        [Parameter(Mandatory = $true)][string]$Reason,
        [int]$Attempts = 0
    )
    if ($script:SpawnMarkerWritten) { return }        # one failure lands one entry only: the first one is the root cause
    $script:SpawnMarkerWritten = $true
    try {
        # `detected_at` has exactly the same shape as clock.py's microseconds: UTC, seconds out to 6
        # decimal places, a literal `Z`. The 6 places are a **shape** requirement: the microseconds are
        # guaranteed by the rounding rule here and do not fall into how that family of date specifiers
        # treats trailing zeros -- the ticks of the `0.9999999` second span divided by 10 is a
        # ...999.9 number, and PowerShell's `[int]` cast rounds it to 7 places (1000000), where Floor
        # truncates.
        # `:` in a .NET custom format is a time separator placeholder that follows CurrentCulture, so
        # the date half goes through the invariant culture and does not follow the machine's locale.
        $now = [DateTime]::UtcNow
        $micros = [int][Math]::Floor(($now.Ticks % [TimeSpan]::TicksPerSecond) / 10)
        $stamp = [string]::Format([Globalization.CultureInfo]::InvariantCulture,
                                  '{0:yyyy-MM-ddTHH:mm:ss}.{1:D6}Z', $now, $micros)
        $record = [ordered]@{
            cycle_id    = $script:SpawnCycle
            phase       = $Phase
            attempts    = $Attempts
            reason      = $Reason
            workspace   = $script:SpawnTarget
            extra       = (ConvertTo-SpawnExtra $script:SpawnExtra)
            detected_at = $stamp
            note        = $script:SpawnMarkerNote
        }
        $dir = Split-Path -Parent $script:SpawnMarkerPath
        if ($dir -and -not (Test-Path $dir)) { $null = New-Item -ItemType Directory -Force -Path $dir }
        if (Test-Path -LiteralPath $script:SpawnMarkerPath -PathType Container) {
            # When a directory of the same name occupies the landing place, Move-Item moves the temp
            # file into that directory and still calls it a success -- and "the marker is on disk"
            # becomes half a lie. Fail on the spot here and let the catch below speak.
            throw "the landing place is a directory: $($script:SpawnMarkerPath)"
        }
        # Written without a BOM: this JSON is for a tool to read, and a BOM makes
        # json.load(encoding='utf-8') blow up.
        # The temp file only means anything within this beat: a successful rename and it is gone, and
        # a retry that runs out still has to be collected by the finally -- leave a `.tmp-<pid>` scrap
        # in the marker directory and the next person reads it as half an accident.
        $tmp = "$($script:SpawnMarkerPath).tmp-$PID"
        try {
            [System.IO.File]::WriteAllText($tmp, ($record | ConvertTo-Json -Depth 5),
                                           (New-Object System.Text.UTF8Encoding($false)))
            Move-SpawnMarkerIntoPlace -From $tmp -To $script:SpawnMarkerPath
        }
        finally {
            if (Test-Path $tmp) { Remove-Item -Path $tmp -Force -ErrorAction SilentlyContinue }
        }
        Write-Warning "conduct dispatch failed; the degradation marker was written: $($script:SpawnMarkerPath)"
    }
    catch {
        # A failed marker write must not turn a failed dispatch into a louder failure: warn once and
        # let the caller exit as it was.
        Write-Warning "could not write the degradation marker ($($script:SpawnMarkerPath)): $($_.Exception.Message)"
    }
}

function Clear-CallbackFailureMarker {
    # Dispatch succeeded: clear the degradation marker left by an earlier beat (marker present =
    # the chain is broken right now; once the chain is joined again it means nothing, and keeping it
    # only makes the next manual /pf-conduct read a long-fixed old accident).
    # The clear also renames before deleting: marker from "there" to "not there" is one same-volume
    # rename, so a concurrent reader never catches the in-between state; if the rename fails (another
    # process is reading it) it falls back to deleting in place -- a clear that fails must not block a
    # successful dispatch.
    if (-not (Test-Path $script:SpawnMarkerPath)) { return }
    $stale = "$($script:SpawnMarkerPath).clearing-$PID"
    Move-Item -Path $script:SpawnMarkerPath -Destination $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $stale -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $script:SpawnMarkerPath -Force -ErrorAction SilentlyContinue
    Write-Output "cleared a stale callback failure marker: $($script:SpawnMarkerPath)"
}