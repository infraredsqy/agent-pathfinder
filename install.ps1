# agent-pathfinder (pf) install: link skills into ~/.agents/skills, put `pf` on PATH, self-check.
#
#   pwsh -File install.ps1                 install (idempotent: re-running repairs it)
#   pwsh -File install.ps1 -Lang zh        install the Chinese skills (the default is en)
#   pwsh -File install.ps1 -TakeShared      take over ~/.agents/skills/shared (a link elsewhere)
#   pwsh -File install.ps1 -Uninstall       remove what this repository linked in
#
# The skills come in one language per install: what gets linked is `skills\<lang>\pf-*`, because a
# method name has exactly one card (`~/.agents/skills/<method>/SKILL.md`). A card bound to a feature is
# not in the skills tree at all: it lives in the feature's own folder (`tools\conduct\features\<name>\
# cards\<lang>\<method>`) and is linked from there, so removing the feature folder takes its card with
# it. A method the chosen language has no card for is borrowed from another language's folder and the
# run says which one; a method no language ships is said missing, never skipped in silence -- the link
# is what a session reads, so a language choice may not quietly delete it.
# `-Lang` beats the choice recorded in `~/.pf/lang.txt`, which beats the default; the record is
# what keeps a re-run -- the repair -- from silently switching the language a user picked.
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

# ── 2. feature cards: a card bound to a feature lives in the feature's own folder ──
# `<feature>/cards/<lang>/<method>` is where a method's card is; this step is what turns one of those
# folders into the `~/.agents/skills/<method>` path a session reads. The install's language is the
# first pick, and a method it has no folder for is taken from another language's folder -- a card in
# the wrong language is readable, a link that is not there is not -- with one line saying which
# language it came from. A method no language ships at all is said missing for the same reason: the
# run may not report success while a node's method has nothing to read. Removing a feature folder is
# how its card goes away: the link is dead from that moment on, and the next run of this script takes
# the dead link off -- that is all "installed and removed with it" means. Which links are this step's
# to reconcile is answered by the link's own shape (`Test-FeatureCard`), never by where the checkout
# sits at the moment: a repository that moves keeps its card links attributable, and a repair then
# re-points them at the new place instead of leaving them dead behind it.
$featuresRoot = Join-Path $repo 'tools\conduct\features'

function Get-CardFolders([string]$featureDir) {
    # Every card one feature ships: `cards\<lang>\<method>`, in language order. Which of them gets
    # linked is decided below, not here: a filter here is what would silently turn a missing language
    # into a missing link.
    $root = Join-Path $featureDir 'cards'
    $found = @()
    if (-not (Test-Path -LiteralPath $root)) { return $found }
    foreach ($langDir in Get-ChildItem -LiteralPath $root -Directory) {
        if ($langDir.Name -like '.*' -or $langDir.Name -like '__*') { continue }
        foreach ($method in Get-ChildItem -LiteralPath $langDir.FullName -Directory) {
            $found += [pscustomobject]@{ Method = $method.Name; Folder = $method.FullName
                                         Feature = (Split-Path -Leaf $featureDir)
                                         Language = $langDir.Name }
        }
    }
    return $found
}

function Get-DeclaredMethods([string]$featureDir) {
    # The methods a declaration points a node at (`metrics.<key>.method`): the names the install owes a
    # readable card. Only the caller says what to do when no card answers one.
    $file = Join-Path $featureDir 'feature.json'
    if (-not (Test-Path -LiteralPath $file)) { return @() }
    try { $decl = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return @() }
    $found = @()
    if ($decl.metrics) {
        foreach ($metric in $decl.metrics.PSObject.Properties) {
            $method = $metric.Value.method
            if ($method -is [string] -and $method) { $found += $method }
        }
    }
    return $found
}

function Test-FeatureCard([string]$target) {
    # Is this link pointing at a feature's card -- `<checkout>\tools\conduct\features\<name>\cards\
    # <lang>\<method>`? The shape is the signature, the same way `Test-PfBin` recognizes a `tools\bin`
    # by its own. Nothing else makes a link of that shape, and one method name has exactly one card, so
    # the card links in the skills tree are this script's to keep in shape -- and recognizing them by
    # shape is what keeps them attributable once the repository has moved, where asking for this
    # checkout's own path would leave every one of them dead and nameless.
    if (-not $target) { return $false }
    $parts = @($target.TrimEnd('\') -split '\\')
    if ($parts.Count -lt 7) { return $false }
    $tail = $parts[($parts.Count - 7)..($parts.Count - 1)]
    return ($tail[0] -eq 'tools') -and ($tail[1] -eq 'conduct') -and ($tail[2] -eq 'features') -and ($tail[4] -eq 'cards')
}

function Test-UnderFeatures([string]$target) {
    # A card link pointing into THIS checkout's features tree: what `-Uninstall` takes off, because
    # uninstalling one checkout is not the moment to clear another one's links. The repair does not ask
    # this -- it reconciles every card link there, whoever made it (see `Test-FeatureCard`).
    if (-not $target) { return $false }
    $root = $featuresRoot.TrimEnd('\') + '\'
    return $target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
}

$featureFolders = @()
if (Test-Path -LiteralPath $featuresRoot) {
    $featureFolders = @(Get-ChildItem -LiteralPath $featuresRoot -Directory | Where-Object {
        $_.Name -notlike '.*' -and $_.Name -notlike '__*' })
}
$allCards = @()
$declared = @()
foreach ($feature in $featureFolders) {
    $allCards += Get-CardFolders $feature.FullName
    $declared += Get-DeclaredMethods $feature.FullName
}

$cards = @($allCards | Where-Object { $_.Language -eq $Lang })
$spare = @{}
foreach ($card in $allCards) {
    if ($card.Language -eq $Lang) { continue }
    $key = $card.Method.ToLowerInvariant()
    if (-not $spare.ContainsKey($key)) { $spare[$key] = $card }
}
foreach ($key in @($spare.Keys | Sort-Object)) {
    if (@($cards | Where-Object { $_.Method.ToLowerInvariant() -eq $key }).Count) { continue }
    $borrowed = $spare[$key]
    $cards += $borrowed
    if (-not $Uninstall) {
        Say 'borrowed' ("$($borrowed.Method)  from $($borrowed.Language)  (this install's language, $Lang, ships no card for it)")
    }
}
$shipped = @{}
foreach ($card in $allCards) { $shipped[$card.Method.ToLowerInvariant()] = $true }
if (-not $Uninstall) {
    foreach ($method in @($declared | Sort-Object -Unique)) {
        if ($shipped.ContainsKey($method.ToLowerInvariant())) { continue }
        Say 'missing' ("$method  (no language ships this card; a node naming it would land on nothing)")
    }
}

$wanted = @{}
foreach ($card in $cards) {
    if (-not $wanted.ContainsKey($card.Method.ToLowerInvariant())) {
        $wanted[$card.Method.ToLowerInvariant()] = $card
    }
}

if (Test-Path -LiteralPath $skillsRoot) {
    foreach ($link in Get-ChildItem -LiteralPath $skillsRoot -Directory -Force) {
        if (-not (Test-Reparse $link.FullName)) { continue }
        $target = (Get-Item -LiteralPath $link.FullName -Force).Target
        if (-not (Test-FeatureCard $target)) { continue }
        if ($Uninstall) {
            if (-not (Test-UnderFeatures $target)) { continue }
            cmd /c rmdir "$($link.FullName)" | Out-Null
            Say 'removed' $link.Name
        } elseif (-not $wanted.ContainsKey($link.Name.ToLowerInvariant())) {
            cmd /c rmdir "$($link.FullName)" | Out-Null
            Say 'unlinked' "$($link.Name)  (its feature is gone, or this run installs another language)"
        }
    }
}

if (-not $Uninstall) {
    foreach ($card in $cards) {
        $key = $card.Method.ToLowerInvariant()
        if ($wanted[$key].Folder -ne $card.Folder) {
            Say 'skipped' "$($card.Method)  (a second feature, $($card.Feature), ships the same card)"
            continue
        }
        New-Junction (Join-Path $skillsRoot $card.Method) $card.Folder
        Say 'linked' "$($card.Method)  ->  $($card.Folder)"
    }
}

# ── 3. shared/pf: `../shared/pf/…` in skill bodies has to land here ───────────
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

# ── 4. PATH ───────────────────────────────────────────────────────────────────
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

# ── 5. self-check ───────────────────────────────────────────────────────────
# Whether the install holds is judged on two sides: this script's half (skill junctions, PATH)
# and the tool tree's half (tool files, the Python version, the ledger, the merge driver). `pf
# doctor` answers for the tool half, so it is asked once here; its red verdict is this script's
# exit code. It reads the layout by the project root it resolved itself (a worktree resolves
# to the main tree).
$verdict = 0
Write-Host ''
Say 'note' "skill language: $Lang"
Say 'note' ("pf skills visible here: " + (@(Get-ChildItem -LiteralPath $skillsRoot -Directory | Where-Object {
    $_.Name -like 'pf-*' -and -not (Test-FeatureCard (Get-Item -LiteralPath $_.FullName -Force).Target) }).Count))
$cardLinks = @(Get-ChildItem -LiteralPath $skillsRoot -Directory -Force -ErrorAction SilentlyContinue | Where-Object {
    (Test-Reparse $_.FullName) -and (Test-FeatureCard (Get-Item -LiteralPath $_.FullName -Force).Target) })
$readableCards = @($cardLinks | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') })
Say 'note' ("feature cards visible here: " + $readableCards.Count)
if ($readableCards.Count -lt $cardLinks.Count) {
    # A link the repair left in place with nothing behind it: the method's folder is there and its
    # `SKILL.md` is not. That is what a session would land on, and the doctor says the same thing
    # from the declaration's side, so the count alone is not allowed to stand for "all good".
    $empty = @($cardLinks | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md')) })
    Say 'warning' ("a card link with nothing to read behind it: " + ($empty.Name -join ', '))
}
if (Test-Path -LiteralPath $sharedPf) { Say 'note' 'shared/pf is in place' } else { Say 'warning' 'shared/pf is not in place yet (its own section above)' }
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