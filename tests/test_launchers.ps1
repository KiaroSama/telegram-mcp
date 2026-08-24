$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$scripts = @(
    (Join-Path $projectRoot 'start-mcp.ps1'),
    (Join-Path $projectRoot 'Manage-Accounts.ps1')
)

foreach ($script in $scripts) {
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw "Missing script: $script"
    }

    $parseErrors = $null
    [void] [Management.Automation.Language.Parser]::ParseFile(
        $script,
        [ref] $null,
        [ref] $parseErrors
    )
    if ($parseErrors.Count -ne 0) {
        throw "PowerShell parse errors in $script`: $($parseErrors -join '; ')"
    }
}

$launcher = Get-Content -LiteralPath $scripts[0] -Raw
$manager = Get-Content -LiteralPath $scripts[1] -Raw
if ($launcher -match '2>&1\s*\|') {
    throw 'Launcher must not pipe native output because that disables original terminal colors.'
}
if ($launcher -notmatch 'runpy\.run_path') {
    throw 'Launcher does not tee Python output while preserving the terminal TTY.'
}

$wrapperMatch = [regex]::Match(
    $launcher,
    "(?ms)\`$pythonWrapper = @'\r?\n(?<body>.*?)\r?\n'@"
)
if (-not $wrapperMatch.Success) {
    throw 'Could not extract the Python tee wrapper from the launcher.'
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$wrapperTestDirectory = Join-Path ([IO.Path]::GetTempPath()) ("telegram-mcp-launcher-" + [guid]::NewGuid())
[void] (New-Item -ItemType Directory -Path $wrapperTestDirectory)
try {
    $wrapperTestScript = Join-Path $wrapperTestDirectory 'raise_error.py'
    $wrapperTestLog = Join-Path $wrapperTestDirectory 'wrapper.log'
    [IO.File]::WriteAllText(
        $wrapperTestScript,
        "raise RuntimeError('wrapper-test-error')$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )

    $wrapperOutput = & $python -c $wrapperMatch.Groups['body'].Value $wrapperTestLog 0 $wrapperTestScript 2>&1
    $wrapperExitCode = $LASTEXITCODE
    if ($wrapperExitCode -eq 0) {
        throw 'Python tee wrapper unexpectedly returned success for an unhandled exception.'
    }

    $wrapperTerminalText = $wrapperOutput -join [Environment]::NewLine
    [string] $wrapperLogText = Get-Content -LiteralPath $wrapperTestLog -Raw
    foreach ($expected in 'Traceback (most recent call last)', 'RuntimeError: wrapper-test-error') {
        if ($wrapperTerminalText -notmatch [regex]::Escape($expected)) {
            throw "Python tee wrapper terminal output is missing: $expected"
        }
        if ($wrapperLogText -notmatch [regex]::Escape($expected)) {
            throw "Python tee wrapper log is missing: $expected"
        }
    }

    $interruptScript = Join-Path $wrapperTestDirectory 'keyboard_interrupt.py'
    $interruptLog = Join-Path $wrapperTestDirectory 'interrupt.log'
    [IO.File]::WriteAllText(
        $interruptScript,
        "raise KeyboardInterrupt()$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )

    $interruptOutput = & $python -c $wrapperMatch.Groups['body'].Value $interruptLog 0 $interruptScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python tee wrapper returned $LASTEXITCODE for Ctrl+C: $($interruptOutput -join [Environment]::NewLine)"
    }
    if (($interruptOutput -join [Environment]::NewLine) -match 'Traceback') {
        throw 'Python tee wrapper printed a traceback for a normal Ctrl+C stop.'
    }

    $shutdownScript = Join-Path $wrapperTestDirectory 'shutdown_output.py'
    $shutdownLog = Join-Path $wrapperTestDirectory 'shutdown.log'
    [IO.File]::WriteAllText(
        $shutdownScript,
        "import atexit, sys$([Environment]::NewLine)atexit.register(lambda: sys.stderr.write('late-shutdown-output\n'))$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )

    $shutdownOutput = & $python -c $wrapperMatch.Groups['body'].Value $shutdownLog 0 $shutdownScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python tee wrapper returned $LASTEXITCODE for shutdown output."
    }
    $shutdownTerminalText = $shutdownOutput -join [Environment]::NewLine
    [string] $shutdownLogText = Get-Content -LiteralPath $shutdownLog -Raw
    if ($shutdownTerminalText -notmatch 'late-shutdown-output') {
        throw 'Python tee wrapper terminal is missing late shutdown output.'
    }
    if ($shutdownLogText -notmatch 'late-shutdown-output') {
        throw 'Python tee wrapper log is missing late shutdown output.'
    }

    # The server runs for as long as the client keeps it, so the tee needs a size
    # ceiling of its own: a retention count alone bounds the number of files, not
    # the one file being written.
    $chattyScript = Join-Path $wrapperTestDirectory 'chatty.py'
    $chattyLog = Join-Path $wrapperTestDirectory 'chatty.log'
    [IO.File]::WriteAllText(
        $chattyScript,
        "import sys$([Environment]::NewLine)for i in range(400): print('x' * 100, file=sys.stderr)$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )

    $null = & $python -c $wrapperMatch.Groups['body'].Value $chattyLog 4096 $chattyScript 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Python tee wrapper returned $LASTEXITCODE for the size test." }
    $liveSize = (Get-Item -LiteralPath $chattyLog).Length
    if ($liveSize -gt 8192) {
        throw "The tee wrote $liveSize bytes past a 4096-byte ceiling without rolling."
    }
    if (-not (Test-Path -LiteralPath "$chattyLog.1")) {
        throw 'The tee never rolled, so the ceiling is not enforced.'
    }
    if (Test-Path -LiteralPath "$chattyLog.2") {
        throw 'Rolling kept more than one previous part.'
    }
    Write-Output 'ok  the launcher tee rolls at its size ceiling and keeps one part'

    # Under the stdio transport stdout carries the MCP protocol -- complete tool
    # results, i.e. the user's own messages, contacts and files. It reaches the
    # terminal and never the disk.
    $protocolScript = Join-Path $wrapperTestDirectory 'protocol.py'
    $protocolLog = Join-Path $wrapperTestDirectory 'protocol.log'
    [IO.File]::WriteAllText(
        $protocolScript,
        "import sys$([Environment]::NewLine)print('protocol-payload-must-not-be-logged')$([Environment]::NewLine)print('diagnostic-line', file=sys.stderr)$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )

    $protocolOutput = & $python -c $wrapperMatch.Groups['body'].Value $protocolLog 0 $protocolScript 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Python tee wrapper returned $LASTEXITCODE for the stdout test." }
    $protocolTerminalText = $protocolOutput -join [Environment]::NewLine
    [string] $protocolLogText = Get-Content -LiteralPath $protocolLog -Raw
    if ($protocolTerminalText -notmatch 'protocol-payload-must-not-be-logged') {
        throw 'The wrapper swallowed stdout instead of passing it through.'
    }
    if ($protocolLogText -match 'protocol-payload-must-not-be-logged') {
        throw 'The wrapper wrote the MCP protocol channel to disk.'
    }
    if ($protocolLogText -notmatch 'diagnostic-line') {
        throw 'The wrapper stopped persisting stderr diagnostics.'
    }
    Write-Output 'ok  the wrapper persists stderr only and leaves stdout on the wire'
}
finally {
    [IO.Directory]::Delete($wrapperTestDirectory, $true)
}

# The account manager rewrites .env, which holds session strings. Two properties are
# not negotiable: it must never echo a secret value, and it must never overwrite the
# file without first putting a copy somewhere recoverable.
if ($manager -notmatch 'Backup-EnvFile') {
    throw 'The account manager rewrites .env with no backup step.'
}
if ($manager -match 'Write-Host[^
]*\$sessionString') {
    throw 'The account manager prints a session string to the terminal.'
}
if ($manager -notmatch 'AsSecureString') {
    throw 'The account manager reads a session string as visible input.'
}
if ($manager -notmatch '(?s)function Set-EnvValue.*?Write-FileAtomic') {
    throw 'The account manager rewrites .env in place instead of replacing it atomically.'
}
if ($manager -match '(?ms)^\s*\[IO\.File\]::WriteAllText\(\s*\r?\n\s*\$envPath') {
    throw 'The account manager still truncates .env before writing it.'
}
if ($manager -match 'Copy-Item[^\r\n]*\$envPath[^\r\n]*-Force') {
    throw 'A backup is taken with -Force, which silently replaces one taken the same second.'
}
foreach ($shipped in $scripts) {
    $text = Get-Content -LiteralPath $shipped -Raw
    # `logs/` beside the source lands inside the git checkout and inherits whatever
    # the repository directory grants; the state directory is the private one.
    if ($text -match "Join-Path \`$PSScriptRoot 'logs'") {
        throw "$(Split-Path -Leaf $shipped) still logs beside its own source."
    }
    foreach ($required in 'Get-StateDirectory', 'Set-OwnerOnlyAcl', 'Remove-StaleFiles') {
        if ($text -notmatch "function $required") {
            throw "$(Split-Path -Leaf $shipped) has no $required."
        }
    }
    # /grant on its own ADDS an entry and leaves the inherited BUILTIN\Users one,
    # so without /inheritance:r the call grants exactly what it meant to remove.
    if ($text -notmatch "'/inheritance:r'") {
        throw "$(Split-Path -Leaf $shipped) grants without removing inheritance."
    }
}
# Every .env this manager creates is restricted before anything is written into
# it: the file ends up holding session strings, and a session string is the
# account, with no password and no second factor.
foreach ($creation in [regex]::Matches($manager, '(?ms)WriteAllText\(\$envPath.{0,300}')) {
    if ($creation.Value -notmatch 'Set-OwnerOnlyAcl -Path \$envPath') {
        throw 'Manage-Accounts.ps1 creates a .env without restricting it to its owner.'
    }
}

if ($launcher -notmatch 'LogMaxBytes') {
    throw 'The launcher log has no size ceiling; a long-running server fills the disk.'
}
# Under the stdio transport stdout IS the MCP protocol channel and carries whole
# tool results. Teeing it wrote the user's messages, contacts and files to disk.
if ($launcher -match 'sys\.stdout\s*=\s*Tee') {
    throw 'The launcher tees stdout, which is the MCP protocol channel.'
}
if ($launcher -notmatch 'sys\.stderr\s*=\s*Tee') {
    throw 'The launcher no longer persists stderr diagnostics at all.'
}
if ($launcher -notmatch 'LogToFile') {
    throw 'Persisting output to disk is not opt-in.'
}

# XDG_STATE_HOME points the launcher at a throwaway directory, so this asserts the
# real path resolution rather than reaching into the operator's own state.
$stateHome = Join-Path ([IO.Path]::GetTempPath()) ("tg-state-" + [guid]::NewGuid())
$originalStateHome = $env:XDG_STATE_HOME
$env:XDG_STATE_HOME = $stateHome
$originalLauncherLog = $env:TELEGRAM_MCP_LAUNCHER_LOG
$env:TELEGRAM_MCP_LAUNCHER_LOG = '1'
$logsDirectory = Join-Path $stateHome 'telegram-mcp/logs'

$originalPath = $env:PATH
$originalPathExt = $env:PATHEXT
$newLogs = @()
try {
    $env:PATH = "$PSScriptRoot\fixtures;$originalPath"
    $env:PATHEXT = ".PS1;$originalPathExt"
    $output = & pwsh -NoProfile -ExecutionPolicy Bypass -File $scripts[0] 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher exited with $LASTEXITCODE`: $($output -join [Environment]::NewLine)"
    }
    $newLogs = @(
        Get-ChildItem -LiteralPath $logsDirectory -Filter 'start-mcp_*.log' -File |
            Select-Object -ExpandProperty FullName
    )
    if ($newLogs.Count -ne 1) {
        throw "Expected one launcher log under $logsDirectory, found $($newLogs.Count)."
    }

    $log = Get-Content -LiteralPath $newLogs[0] -Raw
    foreach ($expected in 'fake-error-output', '[INFO] [launcher]') {
        if ($log -notmatch [regex]::Escape($expected)) {
            throw "Launcher log is missing: $expected"
        }
    }
    if ($log -match [regex]::Escape('fake-normal-output')) {
        throw 'The launcher wrote stdout to disk; stdout is the MCP protocol channel.'
    }
}
finally {
    $env:PATH = $originalPath
    $env:PATHEXT = $originalPathExt
    if ($null -eq $originalStateHome) {
        Remove-Item -LiteralPath Env:XDG_STATE_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:XDG_STATE_HOME = $originalStateHome
    }
    if ($null -eq $originalLauncherLog) {
        Remove-Item -LiteralPath Env:TELEGRAM_MCP_LAUNCHER_LOG -ErrorAction SilentlyContinue
    }
    else {
        $env:TELEGRAM_MCP_LAUNCHER_LOG = $originalLauncherLog
    }
    Remove-Item -LiteralPath $stateHome -Recurse -Force -ErrorAction SilentlyContinue
}

# Without the opt-in, nothing is written to disk at all.
$quietStateHome = Join-Path ([IO.Path]::GetTempPath()) ("tg-quiet-" + [guid]::NewGuid())
$originalStateHome = $env:XDG_STATE_HOME
$originalPath = $env:PATH
$originalPathExt = $env:PATHEXT
try {
    $env:XDG_STATE_HOME = $quietStateHome
    $env:PATH = "$PSScriptRoot\fixtures;$originalPath"
    $env:PATHEXT = ".PS1;$originalPathExt"
    Remove-Item -LiteralPath Env:TELEGRAM_MCP_LAUNCHER_LOG -ErrorAction SilentlyContinue
    $null = & pwsh -NoProfile -ExecutionPolicy Bypass -File $scripts[0] 2>&1
    $quietLogs = Join-Path $quietStateHome 'telegram-mcp/logs'
    if (Test-Path -LiteralPath $quietLogs) {
        $found = @(Get-ChildItem -LiteralPath $quietLogs -File -ErrorAction SilentlyContinue)
        if ($found.Count -ne 0) {
            throw "The launcher wrote $($found.Count) log file(s) without being asked to."
        }
    }
    Write-Output 'ok  the launcher persists nothing unless asked'
}
finally {
    $env:PATH = $originalPath
    $env:PATHEXT = $originalPathExt
    if ($null -eq $originalStateHome) {
        Remove-Item -LiteralPath Env:XDG_STATE_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:XDG_STATE_HOME = $originalStateHome
    }
    Remove-Item -LiteralPath $quietStateHome -Recurse -Force -ErrorAction SilentlyContinue
}

# Retention, proved against the shipped function rather than by eleven real launches.
$retentionDirectory = Join-Path ([IO.Path]::GetTempPath()) ("tg-retention-" + [guid]::NewGuid())
[void] (New-Item -ItemType Directory -Path $retentionDirectory)
try {
    $prune = [regex]::Match($launcher, '(?ms)^function Remove-StaleFiles \{.*?^\}')
    if (-not $prune.Success) { throw 'Could not extract Remove-StaleFiles from the launcher.' }
    . ([ScriptBlock]::Create($prune.Value))

    foreach ($index in 1..15) {
        $name = 'start-mcp_2026-01-{0:d2}_00-00-00_UTC.log' -f $index
        [IO.File]::WriteAllText((Join-Path $retentionDirectory $name), 'x')
    }
    Remove-StaleFiles -Directory $retentionDirectory -Filter 'start-mcp_*.log' -Keep 10
    $kept = @(Get-ChildItem -LiteralPath $retentionDirectory -Filter 'start-mcp_*.log' -File |
            Sort-Object Name)
    if ($kept.Count -ne 10) { throw "Retention kept $($kept.Count) logs, expected 10." }
    # The OLDEST five go, not the newest: a log is only useful while it is recent.
    if ($kept[0].Name -notmatch '2026-01-06') {
        throw "Retention deleted the wrong end: oldest kept is $($kept[0].Name)."
    }
    Write-Output 'ok  launcher logs are pruned to the newest ten'
}
finally {
    Remove-Item -LiteralPath $retentionDirectory -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output 'Launcher checks passed.'
