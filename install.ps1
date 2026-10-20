# agent-pathfinder (pf) install: link skills into ~/.agents/skills, put `pf` on PATH, self-check.
#
#   pwsh -File install.ps1                 install (idempotent: re-running repairs it)
#   pwsh -File install.ps1 -Lang zh        install the Chinese skills (the default is en)
#   pwsh -File install.ps1 -TakeShared      take over ~/.agents/skills/shared (a link elsewhere)
#   pwsh -File install.ps1 -Uninstall       remove what this repository linked in
#
# The skills come in one language per install: what gets linked is `skills\<lang>\pf-*`, because a
# method name has exactly one card (`~/.agents/skills/<method>/SKILL.md`). `-Lang` beats the choice
# recorded in `~/.pf/lang.txt`, which beats the default; the record is what keeps a re-run -- the
# repair -- from silently switching the language a user picked.
#
# Skills are linked as junctions: editing a file in this repository takes effect at once, with
# nothing to sync. `pf` sits on PATH exactly once: a re-run first removes another checkout's
# `tools\bin` (recognized by its own signature and the one two levels up), then appends its own --
# it resolves the tool tree's location by itself, so the repository can move anywhere; move it,
# re-run this script. That appended entry is also recorded in ~/.pf/install.txt: when the old
# directory is off the disk, the record is what gets removed. -Uninstall leaves PATH alone
# (something else may still be using it): after you clear PATH, re-run this script once and the
# recorded entry is swapped out.

[CmdletBinding()]
param(
    [string]$Lang,
    [switch]$TakeShared,
    [switch]$Uninstall,
    [switch]$SkipPath
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$binDir = Join-Path $repo 'tools\bin'
$pfPy = Join-Path $binDir 'pf.py'
$skillsDir = Join-Path $repo 'skills'
# What a fresh install gets when neither -Lang nor the record says.
$defaultLang = 'en'
$skillsRoot = Join-Path $HOME '.agents\skills'
$sharedRoot = Join-Path $skillsRoot 'shared'
$sharedPf = Join-Path $sharedRoot 'pf'
$markerFile = Join-Path $HOME '.pf\install.txt'
$langFile = Join-Path $HOME '.pf\lang.txt'

function Say([string]$what, [string]$detail = '') { Write-Host ("  {0,-16}{1}" -f $what, $detail) }

function Get-SkillLangs([string]$checkout) {
    # What a checkout offers: `skills\<lang>\shared\pf`, one level down. This is also the signature
    # that tells an agent-pathfinder checkout from any other directory that has a `bin`.
    $dir = Join-Path $checkout 'skills'
    if (-not (Test-Path -LiteralPath $dir)) { return @() }
    return @(Get-ChildItem -LiteralPath $dir -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'shared\pf') } | ForEach-Object { $_.Name })
}

function Test-Reparse([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    return ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

function Test-PfBin([string]$dir) {
    # An agent-pathfinder checkout's `tools\bin`: the two signatures are `pf.py` and a skill tree
    # two levels up. Recognizing it is what lets another checkout's PATH entry be removed.
    if (-not $dir) { return $false }
    $checkout = Split-Path -Parent (Split-Path -Parent $dir)
    if (-not $checkout) { return $false }
    return (Test-Path -LiteralPath (Join-Path $dir 'pf.py')) -and (@(Get-SkillLangs $checkout).Count -gt 0)
}

function New-Junction([string]$link, [string]$target) {
    if (Test-Reparse $link) { cmd /c rmdir "$link" | Out-Null }
    elseif (Test-Path -LiteralPath $link) {
        throw "$link already exists and is a real directory. Check inside it, delete it, re-run."
    }
    cmd /c mklink /J "$link" "$target" | Out-Null
    if (-not (Test-Reparse $link)) { throw "the junction was not made: $link" }
}

if (-not (Test-Path -LiteralPath $pfPy)) { throw "cannot find $pfPy -- run this from the repository root." }

# Which language this run installs: `-Lang` > the record > the default. The record is a file of
# its own because `install.txt` holds the PATH entry verbatim and is compared against PATH.
$skillLangs = @(Get-SkillLangs $repo)
if (-not $Lang) {
    $Lang = if (Test-Path -LiteralPath $langFile) { (Get-Content -LiteralPath $langFile -Raw -Encoding UTF8).Trim() } else { $defaultLang }
}
if ($skillLangs -notcontains $Lang) {
    throw "no skills in language '$Lang' here (this checkout has: $($skillLangs -join ', '))"
}
$langSkills = Join-Path $skillsDir $Lang
$repoSharedPf = Join-Path $langSkills 'shared\pf'
if (-not (Test-Path -LiteralPath $repoSharedPf)) { throw "cannot find $repoSharedPf -- run this from the repository root." }
if (-not (Test-Path -LiteralPath $skillsRoot)) { New-Item -ItemType Directory -Force -Path $skillsRoot | Out-Null }

# ── 1. skill junctions ────────────────────────────────────────────────────────
$skills = Get-ChildItem -LiteralPath $langSkills -Directory | Where-Object { $_.Name -like 'pf-*' }
foreach ($skill in $skills) {
    $link = Join-Path $skillsRoot $skill.Name
    if ($Uninstall) {
        if (Test-Reparse $link) { cmd /c rmdir "$link" | Out-Null; Say 'removed' $skill.Name }
    } else {
        New-Junction $link $skill.FullName
        Say 'linked' "$($skill.Name)  ->  $($skill.FullName)"
    }
}
if (-not $Uninstall) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $langFile) | Out-Null
    Set-Content -LiteralPath $langFile -Value $Lang -Encoding UTF8
}

# ── 2. shared/pf: `../shared/pf/…` in skill bodies has to land here ───────────
if ($Uninstall) {
    if (Test-Reparse $sharedPf) { cmd /c rmdir "$sharedPf" | Out-Null; Say 'removed' 'shared/pf' }
} elseif (Test-Reparse $sharedRoot) {
    $target = (Get-Item -LiteralPath $sharedRoot -Force).Target
    if (-not $TakeShared) {
        Say 'skipped' "shared is one link pointing at $target"
        Write-Host '    → `../shared/pf/…` in skill bodies would not land here. To make it a real directory'
        Write-Host '      (holding this repository''s pf, plus what has to stay put), re-run with -TakeShared.'
    } else {
        $oldTarget = (Get-Item -LiteralPath $sharedRoot -Force).Target
        $keep = @()
        if ($oldTarget -and (Test-Path -LiteralPath $oldTarget)) {
            $keep = Get-ChildItem -LiteralPath $oldTarget -Force | Where-Object { $_.Name -ne 'pf' }
        }
        cmd /c rmdir "$sharedRoot" | Out-Null
        New-Item -ItemType Directory -Force -Path $sharedRoot | Out-Null
        foreach ($item in $keep) {
            $link = Join-Path $sharedRoot $item.Name
            if ($item.PSIsContainer) { New-Junction $link $item.FullName } else { Copy-Item -LiteralPath $item.FullName -Destination $link }
            Say 'kept' "$($item.Name)  ->  $($item.FullName)"
        }
        Say 'took over' "shared is now a real directory (it pointed at: $oldTarget)"
    }
} elseif (Test-Path -LiteralPath $sharedRoot) {
    Say 'already a dir' $sharedRoot
}
if (-not $Uninstall -and (Test-Path -LiteralPath $sharedRoot) -and -not (Test-Reparse $sharedRoot)) {
    New-Junction $sharedPf $repoSharedPf
    Say 'linked' "shared/pf  ->  $repoSharedPf"
}

# ── 3. PATH ───────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Say 'note' 'PATH is left for you to clean (something else may still be using it)'
} elseif (-not $SkipPath) {
    $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
    $parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
    $last = if (Test-Path -LiteralPath $markerFile) { (Get-Content -LiteralPath $markerFile -Raw -Encoding UTF8).Trim() } else { '' }
    $had = $parts -contains $binDir
    # The entry the last install added, and another checkout's bin: both come off, this one stays.
    $others = @($parts | Where-Object { $_ -ne $binDir -and ($_ -eq $last -or (Test-PfBin $_)) })
    if ($others.Count) {
        Say 'PATH' ("removed " + ($others -join '; '))
        $parts = @($parts | Where-Object { $others -notcontains $_ })
    }
    if ($had) { Say 'PATH' 'already in there' } else { $parts += $binDir; Say 'PATH' "appended $binDir (open a new terminal to see it)" }
    [Environment]::SetEnvironmentVariable('PATH', ($parts -join ';'), 'User')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $markerFile) | Out-Null
    Set-Content -LiteralPath $markerFile -Value $binDir -Encoding UTF8
}

# ── 4. self-check ───────────────────────────────────────────────────────────
# Whether the install holds is judged on two sides: this script's half (skill junctions, PATH)
# and the tool tree's half (tool files, the Python version, the ledger, the merge driver). `pf
# doctor` answers for the tool half, so it is asked once here; its red verdict is this script's
# exit code. It reads the layout by the project root it resolved itself (a worktree resolves
# to the main tree).
$verdict = 0
Write-Host ''
Say 'note' "skill language: $Lang"
Say 'note' ("pf skills visible here: " + ((Get-ChildItem -LiteralPath $skillsRoot -Directory | Where-Object { $_.Name -like 'pf-*' } | Measure-Object).Count))
if (Test-Path -LiteralPath $sharedPf) { Say 'note' 'shared/pf is in place' } else { Say 'warning' 'shared/pf is not in place yet (section 2)' }
if (-not $Uninstall) {
    Write-Host ''
    Push-Location -LiteralPath $repo
    try { & (Join-Path $binDir 'pf.cmd') doctor; $verdict = $LASTEXITCODE } finally { Pop-Location }
    if ($verdict -ne 0) { Write-Warning 'the tool tree half did not pass (see above): deal with it, then re-run this script.' }
}
Write-Host "  Next: open a new terminal and run"
Write-Host "    pf version        # check that PATH is in effect"
Write-Host "    pf doctor         # health-check your project (the ledger, the merge driver)"
exit $verdict