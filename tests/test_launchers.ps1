$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$scripts = @(
    (Join-Path $projectRoot 'run.ps1'),
    (Join-Path $projectRoot 'chigwell.ps1'),
    (Join-Path $projectRoot 'Install-chigwellCommand.ps1')
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

$installer = Get-Content -LiteralPath $scripts[2] -Raw
$launcher = Get-Content -LiteralPath $scripts[0] -Raw
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

    $wrapperOutput = & $python -c $wrapperMatch.Groups['body'].Value $wrapperTestLog $wrapperTestScript 2>&1
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

    $interruptOutput = & $python -c $wrapperMatch.Groups['body'].Value $interruptLog $interruptScript 2>&1
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

    $shutdownOutput = & $python -c $wrapperMatch.Groups['body'].Value $shutdownLog $shutdownScript 2>&1
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
}
finally {
    [IO.Directory]::Delete($wrapperTestDirectory, $true)
}

if ($installer -notmatch "SetEnvironmentVariable\('Path',\s*\$[^,]+,\s*'Machine'\)") {
    throw 'Installer does not persist the project directory in Machine PATH.'
}
if ($installer -notmatch "SetEnvironmentVariable\('PATHEXT',\s*\$[^,]+,\s*'Machine'\)") {
    throw 'Installer does not persist .PS1 command discovery in Machine PATHEXT.'
}

$logsDirectory = Join-Path $projectRoot 'logs'
$logsDirectoryExisted = Test-Path -LiteralPath $logsDirectory
$before = @(
    Get-ChildItem -LiteralPath $logsDirectory -Filter 'run_*.log' -File -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
)

$originalPath = $env:PATH
$originalPathExt = $env:PATHEXT
$newLogs = @()
try {
    $env:PATH = "$PSScriptRoot\fixtures;$originalPath"
    $env:PATHEXT = ".PS1;$originalPathExt"
    $output = & pwsh -NoProfile -ExecutionPolicy Bypass -File $scripts[1] 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher exited with $LASTEXITCODE`: $($output -join [Environment]::NewLine)"
    }
    $after = @(
        Get-ChildItem -LiteralPath $logsDirectory -Filter 'run_*.log' -File |
            Select-Object -ExpandProperty FullName
    )
    $newLogs = @($after | Where-Object { $_ -notin $before })
    if ($newLogs.Count -ne 1) {
        throw "Expected one new launcher log, found $($newLogs.Count)."
    }

    $log = Get-Content -LiteralPath $newLogs[0] -Raw
    foreach ($expected in 'fake-normal-output', 'fake-error-output', '[INFO] [launcher]') {
        if ($log -notmatch [regex]::Escape($expected)) {
            throw "Launcher log is missing: $expected"
        }
    }
}
finally {
    $env:PATH = $originalPath
    $env:PATHEXT = $originalPathExt
    $testLogs = @(
        Get-ChildItem -LiteralPath $logsDirectory -Filter 'run_*.log' -File -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName |
            Where-Object { $_ -notin $before }
    )
    foreach ($logFile in $testLogs) {
        Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue
    }
    if (-not $logsDirectoryExisted -and
        (Test-Path -LiteralPath $logsDirectory) -and
        -not (Get-ChildItem -LiteralPath $logsDirectory -Force)) {
        Remove-Item -LiteralPath $logsDirectory -Force
    }
}

Write-Output 'Launcher checks passed.'
