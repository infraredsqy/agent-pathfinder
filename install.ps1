# PathFinder 的安装：把技能挂进 ~/.agents/skills，把 `pf` 放进 PATH，然后自检。
#
#   pwsh -File install.ps1                 装上（幂等，重跑即修复）
#   pwsh -File install.ps1 -TakeShared     接管 ~/.agents/skills/shared（原先它整个是指向别处的链接时）
#   pwsh -File install.ps1 -Uninstall      摘掉本仓库挂上去的东西
#
# 技能用 junction 挂载：改这个仓库里的文件立刻生效，不必再同步。`pf` 只在 PATH 上出现一次，
# 它自己解析工具树的位置，所以仓库搬到哪儿都行（搬完重跑一次本脚本即可）。

[CmdletBinding()]
param(
    [switch]$TakeShared,
    [switch]$Uninstall,
    [switch]$SkipPath
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$binDir = Join-Path $repo 'tools\bin'
$pfPy = Join-Path $binDir 'pf.py'
$skillsRoot = Join-Path $HOME '.agents\skills'
$sharedRoot = Join-Path $skillsRoot 'shared'
$sharedPf = Join-Path $sharedRoot 'pf'
$repoSharedPf = Join-Path $repo 'skills\shared\pf'

function Say([string]$what, [string]$detail = '') { Write-Host ("  {0,-16}{1}" -f $what, $detail) }

function Test-Reparse([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    return ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

function New-Junction([string]$link, [string]$target) {
    if (Test-Reparse $link) { cmd /c rmdir "$link" | Out-Null }
    elseif (Test-Path -LiteralPath $link) {
        throw "$link 已经存在，而且是一个真实的目录。先自己确认它里面的东西，再删掉它重跑。"
    }
    cmd /c mklink /J "$link" "$target" | Out-Null
    if (-not (Test-Reparse $link)) { throw "junction 没建成：$link" }
}

if (-not (Test-Path -LiteralPath $pfPy)) { throw "找不到 $pfPy —— 这个脚本要放在仓库根跑。" }
if (-not (Test-Path -LiteralPath $skillsRoot)) { New-Item -ItemType Directory -Force -Path $skillsRoot | Out-Null }

# ── 1. 技能 junction ────────────────────────────────────────────────────────────
$skills = Get-ChildItem -LiteralPath (Join-Path $repo 'skills') -Directory | Where-Object { $_.Name -like 'pf-*' }
foreach ($skill in $skills) {
    $link = Join-Path $skillsRoot $skill.Name
    if ($Uninstall) {
        if (Test-Reparse $link) { cmd /c rmdir "$link" | Out-Null; Say '摘掉' $skill.Name }
    } else {
        New-Junction $link $skill.FullName
        Say '挂上' "$($skill.Name)  ->  $($skill.FullName)"
    }
}

# ── 2. shared/pf：技能正文里的 `../shared/pf/…` 要落在这里 ──────────────────────
if ($Uninstall) {
    if (Test-Reparse $sharedPf) { cmd /c rmdir "$sharedPf" | Out-Null; Say '摘掉' 'shared/pf' }
} elseif (Test-Reparse $sharedRoot) {
    $target = (Get-Item -LiteralPath $sharedRoot -Force).Target
    if (-not $TakeShared) {
        Say '跳过' "shared 整个是指向 $target 的链接"
        Write-Host '    → 技能正文里的 `../shared/pf/…` 会落不到这里。想让它成为一个真实目录'
        Write-Host '      （里面放本仓库的 pf、以及原处仍要保留的东西），加 -TakeShared 重跑。'
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
            Say '保留' "$($item.Name)  ->  $($item.FullName)"
        }
        Say '接管' "shared 成为一个真实目录（原目标：$oldTarget）"
    }
} elseif (Test-Path -LiteralPath $sharedRoot) {
    Say '已是目录' $sharedRoot
}
if (-not $Uninstall -and (Test-Path -LiteralPath $sharedRoot) -and -not (Test-Reparse $sharedRoot)) {
    New-Junction $sharedPf $repoSharedPf
    Say '挂上' "shared/pf  ->  $repoSharedPf"
}

# ── 3. PF_HOME 与 PATH ─────────────────────────────────────────────────────────
if ($Uninstall) {
    Say '提示' 'PATH / PF_HOME 留给你自己清（它们可能还有别处在用）'
} else {
    [Environment]::SetEnvironmentVariable('PF_HOME', $repo, 'User')
    Say 'PF_HOME' $repo
    if (-not $SkipPath) {
        $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
        $parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
        if ($parts -contains $binDir) {
            Say 'PATH' '已经在里面了'
        } else {
            [Environment]::SetEnvironmentVariable('PATH', (($parts + $binDir) -join ';'), 'User')
            Say 'PATH' "已追加 $binDir（新开的终端才认）"
        }
    }
}

# ── 4. 自检 ───────────────────────────────────────────────────────────────────
Write-Host ''
Say '记录' ("本机现在能看见的 pf 技能：" + ((Get-ChildItem -LiteralPath $skillsRoot -Directory | Where-Object { $_.Name -like 'pf-*' } | Measure-Object).Count) + ' 个')
if (Test-Path -LiteralPath $sharedPf) { Say '记录' 'shared/pf 就位' } else { Say '注意' 'shared/pf 还不在位（见上面第 2 节）' }
Write-Host "  下一步：新开一个终端，跑"
Write-Host "    pf version        # 确认 PATH 生效"
Write-Host "    pf doctor         # 在某个项目里体检（记录目录、合并驱动）"