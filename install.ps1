# agent-pathfinder (pf) install: link skills into ~/.agents/skills, put `pf` on PATH, self-check.
#
#   pwsh -File install.ps1                 install (idempotent: re-running repairs it)
#   pwsh -File install.ps1 -Lang zh        install the Chinese skills (the default is en)
#   pwsh -File install.ps1 -TakeShared      take over ~/.agents/skills/shared (a link elsewhere)
#   pwsh -File install.ps1 -Uninstall       remove what this repository linked in
#
# The skills come in one language per install: what gets linked is `skills\<lang>\pf-*`, because a
# method name has exactly one skill (`~/.agents/skills/<method>/SKILL.md`). A skill a plugin ships is
# not in the core's skills tree: it lives in the plugin's own folder
# (`plugins\<name>\skills\<lang>\<method>`) and is linked from there, so removing the plugin folder
# takes its skills with it. A method the chosen language has no skill for is borrowed from another
# language's folder and the run says which one; a method no language ships is said missing, never
# skipped in silence -- the link is what a session reads, so a language choice may not quietly delete
# it.
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

# The two skill sets share one namespace: a name under `$skillsRoot`. They are gathered here, before
# section 1 writes a junction, because that is the only moment both are in hand with nothing on disk
# yet. A plugin skill, or a method a node's declaration names, that carries a core skill's name would
# take that junction in section 2: the core skill left unreachable under its own name, the run still
# reporting success, and every re-run repeating the theft. So a name both sides carry is refused
# before the first link is made or removed; a name only one side carries is an ordinary link, and a
# plugin method that merely starts with `pf-` is a method like any other.
$skills = Get-ChildItem -LiteralPath $langSkills -Directory | Where-Object { $_.Name -like 'pf-*' }
$pluginsRoot = Join-Path $repo 'plugins'

function Get-SkillFolders([string]$pluginDir) {
    # Every skill one plugin ships: `skills\<lang>\<method>`, in language order. Which of them gets
    # linked is decided below, not here: a filter here is what would silently turn a missing language
    # into a missing link.
    $root = Join-Path $pluginDir 'skills'
    $found = @()
    if (-not (Test-Path -LiteralPath $root)) { return $found }
    foreach ($langDir in Get-ChildItem -LiteralPath $root -Directory) {
        if ($langDir.Name -like '.*' -or $langDir.Name -like '__*') { continue }
        foreach ($method in Get-ChildItem -LiteralPath $langDir.FullName -Directory) {
            $found += [pscustomobject]@{ Method = $method.Name; Folder = $method.FullName
                                         Plugin = (Split-Path -Leaf $pluginDir)
                                         Language = $langDir.Name }
        }
    }
    return $found
}

function Get-DeclaredMethods([string]$pluginDir) {
    # The methods a declaration points a node at (`metrics.<key>.method`): the names the install owes a
    # readable skill, each with the plugin that declared it, so a name two owners share can say who
    # claims it. Only the caller says what to do when no skill answers one.
    $file = Join-Path $pluginDir 'plugin.json'
    if (-not (Test-Path -LiteralPath $file)) { return @() }
    try { $decl = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return @() }
    $found = @()
    if ($decl.metrics) {
        foreach ($metric in $decl.metrics.PSObject.Properties) {
            $method = $metric.Value.method
            if ($method -is [string] -and $method) {
                $found += [pscustomobject]@{ Method = $method; Plugin = (Split-Path -Leaf $pluginDir) }
            }
        }
    }
    return $found
}

$pluginFolders = @()
if (Test-Path -LiteralPath $pluginsRoot) {
    $pluginFolders = @(Get-ChildItem -LiteralPath $pluginsRoot -Directory | Where-Object {
        $_.Name -notlike '.*' -and $_.Name -notlike '__*' })
}
$allSkills = @()
$declared = @()
foreach ($plugin in $pluginFolders) {
    $allSkills += Get-SkillFolders $plugin.FullName
    $declared += Get-DeclaredMethods $plugin.FullName
}

# Who claims a name, the first claimant winning: the throw can name one plugin, and the one whose
# claim the run would have read first is the one to send the reader to.
$claims = @{}
foreach ($skill in $allSkills) {
    $key = $skill.Method.ToLowerInvariant()
    if (-not $claims.ContainsKey($key)) { $claims[$key] = $skill.Plugin }
}
foreach ($decl in $declared) {
    $key = $decl.Method.ToLowerInvariant()
    if (-not $claims.ContainsKey($key)) { $claims[$key] = $decl.Plugin }
}
# Uninstall is not refused: it writes no junction, and taking a stolen one off is how the core skill
# gets its own name back for the next run.
if (-not $Uninstall) {
    foreach ($skill in $skills) {
        $key = $skill.Name.ToLowerInvariant()
        if ($claims.ContainsKey($key)) {
            throw ("plugin '$($claims[$key])' claims the skill name '$($skill.Name)', which belongs to a " +
                   "core skill here: one method name has one skill, so that plugin's link would overwrite " +
                   "the core's. Rename the plugin's method or the core skill, and re-run.")
        }
    }
}

# ── 1. skill junctions ────────────────────────────────────────────────────────
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

# ── 2. plugin skills: a skill a plugin ships lives in the plugin's own folder ──
# `<plugin>/skills/<lang>/<method>` is where a method's skill is; this step is what turns one of those
# folders into the `~/.agents/skills/<method>` path a session reads. The install's language is the
# first pick, and a method it has no folder for is taken from another language's folder -- a skill in
# the wrong language is readable, a link that is not there is not -- with one line saying which
# language it came from. A method no language ships at all is said missing for the same reason: the
# run may not report success while a node's method has nothing to read. Removing a plugin folder is
# how its skills go away: the link is dead from that moment on, and the next run of this script takes
# the dead link off -- that is all "installed and removed with it" means. Which links are this step's
# to reconcile is answered by the link's own shape (`Test-PluginSkill`), never by where the checkout
# sits at the moment: a repository that moves keeps its skill links attributable, and a repair then
# re-points them at the new place instead of leaving them dead behind it. The set itself is gathered
# above, before the core's links are made, so a name the two sides share is refused there instead of
# linked here.

function Test-PluginSkill([string]$target) {
    # Is this link pointing at a plugin's skill -- `<checkout>\plugins\<name>\skills\<lang>\<method>`?
    # The shape is the signature, the same way `Test-PfBin` recognizes a `tools\bin` by its own.
    # Nothing else makes a link of that shape, and one method name has exactly one skill, so the skill
    # links in the skills tree are this script's to keep in shape -- and recognizing them by shape is
    # what keeps them attributable once the repository has moved, where asking for this checkout's own
    # path would leave every one of them dead and nameless.
    if (-not $target) { return $false }
    $parts = @($target.TrimEnd('\') -split '\\')
    if ($parts.Count -lt 5) { return $false }
    $tail = $parts[($parts.Count - 5)..($parts.Count - 1)]
    return ($tail[0] -eq 'plugins') -and ($tail[2] -eq 'skills')
}

function Test-UnderPlugins([string]$target) {
    # A skill link pointing into THIS checkout's plugin tree: what `-Uninstall` takes off, because
    # uninstalling one checkout is not the moment to clear another one's links. The repair does not ask
    # this -- it reconciles every skill link there, whoever made it (see `Test-PluginSkill`).
    if (-not $target) { return $false }
    $root = $pluginsRoot.TrimEnd('\') + '\'
    return $target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
}

$pluginSkills = @($allSkills | Where-Object { $_.Language -eq $Lang })
$spareSkills = @{}
foreach ($skill in $allSkills) {
    if ($skill.Language -eq $Lang) { continue }
    $key = $skill.Method.ToLowerInvariant()
    if (-not $spareSkills.ContainsKey($key)) { $spareSkills[$key] = $skill }
}
foreach ($key in @($spareSkills.Keys | Sort-Object)) {
    if (@($pluginSkills | Where-Object { $_.Method.ToLowerInvariant() -eq $key }).Count) { continue }
    $borrowed = $spareSkills[$key]
    $pluginSkills += $borrowed
    if (-not $Uninstall) {
        Say 'borrowed' ("$($borrowed.Method)  from $($borrowed.Language)  (this install's language, $Lang, ships no skill for it)")
    }
}
$shipped = @{}
foreach ($skill in $allSkills) { $shipped[$skill.Method.ToLowerInvariant()] = $true }
if (-not $Uninstall) {
    foreach ($method in @($declared.Method | Sort-Object -Unique)) {
        if ($shipped.ContainsKey($method.ToLowerInvariant())) { continue }
        Say 'missing' ("$method  (no language ships this skill; a node naming it would land on nothing)")
    }
}

$wantedSkills = @{}
foreach ($skill in $pluginSkills) {
    if (-not $wantedSkills.ContainsKey($skill.Method.ToLowerInvariant())) {
        $wantedSkills[$skill.Method.ToLowerInvariant()] = $skill
    }
}

if (Test-Path -LiteralPath $skillsRoot) {
    foreach ($link in Get-ChildItem -LiteralPath $skillsRoot -Directory -Force) {
        if (-not (Test-Reparse $link.FullName)) { continue }
        $target = (Get-Item -LiteralPath $link.FullName -Force).Target
        if (-not (Test-PluginSkill $target)) { continue }
        if ($Uninstall) {
            if (-not (Test-UnderPlugins $target)) { continue }
            cmd /c rmdir "$($link.FullName)" | Out-Null
            Say 'removed' $link.Name
        } elseif (-not $wantedSkills.ContainsKey($link.Name.ToLowerInvariant())) {
            cmd /c rmdir "$($link.FullName)" | Out-Null
            Say 'unlinked' "$($link.Name)  (its plugin is gone, or this run installs another language)"
        }
    }
}

if (-not $Uninstall) {
    foreach ($skill in $pluginSkills) {
        $key = $skill.Method.ToLowerInvariant()
        if ($wantedSkills[$key].Folder -ne $skill.Folder) {
            Say 'skipped' "$($skill.Method)  (a second plugin, $($skill.Plugin), ships the same skill)"
            continue
        }
        New-Junction (Join-Path $skillsRoot $skill.Method) $skill.Folder
        Say 'linked' "$($skill.Method)  ->  $($skill.Folder)"
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
} else {
    # Nothing owns `shared` here yet -- the first install on a machine, or a root someone cleaned.
    # It has to become a real directory: the branch below links this checkout's `pf` into it, and a
    # skill body that says `../shared/pf/…` reads a path that only exists once it is one.
    New-Item -ItemType Directory -Force -Path $sharedRoot | Out-Null
    Say 'created' $sharedRoot
}
if (-not $Uninstall -and (Test-Path -LiteralPath $sharedRoot) -and -not (Test-Reparse $sharedRoot)) {
    New-Junction $sharedPf $repoSharedPf
    Say 'linked' "shared/pf  ->  $repoSharedPf"
}

# ── 4. PATH ───────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Say 'note' 'PATH is left for you to clean (something else may still be using it)'
} elseif (-not $SkipPath) {
    # Read and write the value raw, through the key: GetEnvironmentVariable hands back the expanded
    # string and SetEnvironmentVariable writes REG_SZ, so a rewrite through them would bake
    # `%JAVA_HOME%\bin` into the literal path of the day. The stored kind is kept, so a reference
    # stays a reference.
    $envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $userPath = [string]$envKey.GetValue('Path', '',
                        [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = [Microsoft.Win32.RegistryValueKind]::String
        try { $kind = $envKey.GetValueKind('Path') } catch { }
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
        $envKey.SetValue('Path', ($parts -join ';'), $kind)
    } finally {
        $envKey.Close()
    }
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
    $_.Name -like 'pf-*' -and -not (Test-PluginSkill (Get-Item -LiteralPath $_.FullName -Force).Target) }).Count))
$skillLinks = @(Get-ChildItem -LiteralPath $skillsRoot -Directory -Force -ErrorAction SilentlyContinue | Where-Object {
    (Test-Reparse $_.FullName) -and (Test-PluginSkill (Get-Item -LiteralPath $_.FullName -Force).Target) })
$readableSkills = @($skillLinks | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') })
Say 'note' ("plugin skills visible here: " + $readableSkills.Count)
if ($readableSkills.Count -lt $skillLinks.Count) {
    # A link the repair left in place with nothing behind it: the method's folder is there and its
    # `SKILL.md` is not. That is what a session would land on, and the doctor says the same thing
    # from the declaration's side, so the count alone is not allowed to stand for "all good".
    $emptySkills = @($skillLinks | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md')) })
    Say 'warning' ("a skill link with nothing to read behind it: " + ($emptySkills.Name -join ', '))
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