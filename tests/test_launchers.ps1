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
