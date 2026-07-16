$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$updater = Join-Path $projectRoot 'Update-chigwell.ps1'
$command = Join-Path $projectRoot 'chigwell.ps1'

foreach ($script in $updater, $command) {
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

$updaterText = Get-Content -LiteralPath $updater -Raw
foreach ($expected in @(
    'Show status and remotes',
    'Fetch upstream',
    'Show available updates',
    'Merge upstream/main',
    'Run launcher tests',
    'Run Python tests',
    'Push origin/main',
    'Run full update'
)) {
    if ($updaterText -notmatch [regex]::Escape($expected)) {
        throw "Update menu is missing: $expected"
    }
}

$logsDirectory = Join-Path $projectRoot 'logs'
$before = @(Get-ChildItem -LiteralPath $logsDirectory -Filter 'Update-chigwell_*.log' -File -ErrorAction SilentlyContinue)
$beforePaths = @($before | Select-Object -ExpandProperty FullName)
try {
    $output = & pwsh -NoProfile -ExecutionPolicy Bypass -File $updater 0 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Update menu exited with $LASTEXITCODE`: $($output -join [Environment]::NewLine)"
    }
    if (($output -join [Environment]::NewLine) -notmatch 'Chigwell Update Menu') {
        throw 'Update menu header was not displayed.'
    }

    $routedOutput = & pwsh -NoProfile -ExecutionPolicy Bypass -File $command update 0 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "chigwell update exited with $LASTEXITCODE`: $($routedOutput -join [Environment]::NewLine)"
    }
    if (($routedOutput -join [Environment]::NewLine) -notmatch 'Chigwell Update Menu') {
        throw 'chigwell update did not open the update menu.'
    }
}
finally {
    $after = @(Get-ChildItem -LiteralPath $logsDirectory -Filter 'Update-chigwell_*.log' -File -ErrorAction SilentlyContinue)
    foreach ($log in @($after | Where-Object FullName -NotIn $beforePaths)) {
        Remove-Item -LiteralPath $log.FullName -Force -ErrorAction SilentlyContinue
    }
}

Write-Output 'Update menu checks passed.'
