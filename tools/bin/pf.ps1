# The shim PowerShell finds on PATH -- and the one PowerShell takes when it sits beside `pf.cmd`:
# a bare word resolves to the `.ps1` before the `.cmd` of the same name, in both PS hosts. A cmd.exe
# session still takes `pf.cmd`, which is what carries `pf` there (cmd does not resolve a `.ps1`).
#
# Why a second shim: the caller's shell, not pf, decides how a quoted argument is rendered for the
# target. cmd.exe hands a batch file the caller's own rendering with its double quotes still in it --
# `%*` reads back `-o {"deliverable":"x y"}` -- but that text has no argument boundary left in it
# (`%1` and `"%~1"` are cut at the first space), and once it is spliced into the command line of
# `python.exe` the CRT parses it again and eats a double quote as a delimiter: the handoff line
# (`pf handoff ... -o '{"deliverable":"…"}'`) reached pf.py as `{deliverable:…}`.
#
# PowerShell renders each argument itself, and a host that does not say it renders the way the CRT
# parses renders the way cmd.exe does -- the double quote inside an argument is left to the CRT and
# eaten the same way. An argument like that is what makes the command line be built here, in the one
# spelling the CRT parses back, and handed to the process directly. Which arguments count is the
# caller's own (`$args`), never this shim's fixed `-X utf8 <pf.py>` prefix: counting that prefix would
# send every call down the built branch on a checkout whose path carries a space, and the caller's
# capture would go blind. Arguments carrying no whitespace and no double quote are the same line for
# every renderer, so they stay on `&` -- which is what keeps PowerShell's own pipes, redirection and
# capture working for them.
#
# That built line is taken by a host declaring neither of the two CRT renderers (`Standard`,
# `Windows`) as soon as one of the caller's own arguments is empty or carries whitespace or a double
# quote -- the shim's own prefix is fixed and is not one of them, so a checkout path carrying a space
# does not by itself put a call here. The child then inherits this process's streams -- its output,
# its errors and its exit code all arrive -- while this host's own `|`, `>` and `$x =` around that one
# call see nothing of it.
#
# Which interpreter is a probe, not a fallback -- pf.cmd's contract, kept here: the probe picks one,
# that one runs `pf.py` exactly once, and its exit code is the exit code of `pf`.

function ConvertTo-CrtArgument([string]$text) {
    # The spelling the CRT parses back: an empty argument, or one carrying whitespace or a double
    # quote, travels wrapped in quotes, and every backslash in front of a quote -- and in front of
    # the closing quote -- is doubled.
    if ($text.Length -eq 0) { return '""' }
    if ($text -notmatch '[\s"]') { return $text }
    $built = New-Object System.Text.StringBuilder
    [void]$built.Append('"')
    $slashes = 0
    foreach ($character in $text.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$built.Append('\' * ($slashes * 2 + 1))
            [void]$built.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { [void]$built.Append('\' * $slashes); $slashes = 0 }
        [void]$built.Append($character)
    }
    if ($slashes -gt 0) { [void]$built.Append('\' * ($slashes * 2)) }
    [void]$built.Append('"')
    return $built.ToString()
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pf = Join-Path $here 'pf.py'

# The first match, the one `where python` leads with: a second python on PATH is a candidate the
# probe does not take, and `$python.Source` on a whole match list is not a path.
$interpreter = $null
$leading = @()
$python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -ne $python) {
    $interpreter = $python.Source
    $leading = @('-X', 'utf8', $pf)
} else {
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $py) {
        $interpreter = $py.Source
        $leading = @('-3', '-X', 'utf8', $pf)
    }
}
if ($null -eq $interpreter) {
    [Console]::Error.WriteLine('pf: python not found on PATH')
    exit 127
}

$argv = @($leading) + @($args)

# PowerShell 7.3 and later name the renderer they use for a native command's arguments: `Standard` and
# `Windows` quote the way the CRT parses, and `Legacy` -- like every host that publishes neither --
# only joins the arguments. Those two spellings are what the branch below accepts.
$renderer = Get-Variable PSNativeCommandArgumentPassing -ValueOnly -ErrorAction SilentlyContinue
$crtRenderer = ($renderer -eq 'Standard') -or ($renderer -eq 'Windows')

# An argument carrying neither whitespace nor a double quote, and not empty, is the same string for
# every renderer: joined with the others it is a line with no quoting in it, which the CRT splits back
# into exactly this argv. An argv of those needs no building, so `&` can hand it over and the caller
# keeps every stream of a native command. (An empty one is not among them: Windows PowerShell 5.1
# renders it as nothing at all, so the entry would go missing.) The entries read below are the
# caller's own (`$args`) and not the whole line this shim goes on to run: its own `-X utf8 <pf.py>`
# prefix is fixed, and counting that prefix too would put every call of a checkout whose path carries
# a space on the built line -- and its caller's capture would go blind with it.
$bare = $true
foreach ($argument in @($args)) {
    if ([string]$argument -match '^$|[\s"]') { $bare = $false; break }
}

if ($crtRenderer -or $bare) {
    & $interpreter @argv
    exit $LASTEXITCODE
}

# The host would leave a double quote inside an argument to the CRT, so the command line is built
# here and the process started with it. That child inherits this process's streams -- its output, its
# errors and its exit code reach the caller as they are -- while PowerShell's own capture and
# redirection of this one call do not see them.
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $interpreter
$start.Arguments = (@($argv | ForEach-Object { ConvertTo-CrtArgument ([string]$_) }) -join ' ')
$start.UseShellExecute = $false
# Without this the child starts in this process's directory rather than the one PowerShell is in, and
# `pf` reads the project root from where it was started -- a worktree resolving back to its main tree.
$location = Get-Location
if ($location.Provider.Name -eq 'FileSystem') { $start.WorkingDirectory = $location.Path }
$process = [System.Diagnostics.Process]::Start($start)
$process.WaitForExit()
exit $process.ExitCode