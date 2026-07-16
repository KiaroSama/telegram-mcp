#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('0', '1', '2', '3', '4', '5', '6', '7', '8', '9')]
    [string] $Choice
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$logsDirectory = Join-Path $PSScriptRoot 'logs'
[void] (New-Item -ItemType Directory -Path $logsDirectory -Force)
$timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
$logPath = Join-Path $logsDirectory "Update-chigwell_$timestamp.log"
$transcriptStarted = $false
$exitCode = 0

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [string[]] $Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE."
    }
}

function Invoke-Git {
    param([string[]] $Arguments)
    Invoke-Checked -FilePath 'git' -Arguments $Arguments
}

function Assert-Repository {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'git was not found in PATH.'
    }

    $inside = & git rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $inside -ne 'true') {
        throw "Not a Git repository: $PSScriptRoot"
    }

    foreach ($remote in 'origin', 'upstream') {
        & git remote get-url $remote *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Required Git remote is missing: $remote"
        }
    }
}

function Assert-CleanWorkingTree {
    $changes = @(& git status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not inspect the Git working tree.'
    }
    if ($changes.Count -gt 0) {
        throw 'Working tree is not clean. Commit or discard your changes before merging upstream.'
    }
}

function Show-StatusAndRemotes {
    Invoke-Git -Arguments @('status', '-sb')
    Invoke-Git -Arguments @('remote', '-v')
}

function Fetch-Upstream {
    Write-Host 'Fetching upstream/main...' -ForegroundColor Cyan
    Invoke-Git -Arguments @('fetch', 'upstream', 'main')
}

function Get-UpdateCount {
    $count = & git rev-list --count HEAD..upstream/main
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not compare main with upstream/main. Fetch upstream first.'
    }
    return [int] $count
}

function Show-AvailableUpdates {
    $count = Get-UpdateCount
    if ($count -eq 0) {
        Write-Host 'No upstream updates are available.' -ForegroundColor Green
        return
    }

    Write-Host "$count upstream commit(s) available:" -ForegroundColor Yellow
    Invoke-Git -Arguments @('log', '--oneline', 'HEAD..upstream/main')
    Invoke-Git -Arguments @('diff', '--stat', 'HEAD...upstream/main')
}

function Merge-Upstream {
    Assert-CleanWorkingTree
    if ((Get-UpdateCount) -eq 0) {
        Write-Host 'Already up to date.' -ForegroundColor Green
        return
    }

    Write-Host 'Merging upstream/main...' -ForegroundColor Cyan
    Invoke-Git -Arguments @('merge', '--no-edit', 'upstream/main')
}

function Invoke-LauncherTests {
    $testScript = Join-Path $PSScriptRoot 'tests\test_launchers.ps1'
    $engine = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $engine) {
        $engine = Get-Command powershell -ErrorAction Stop
    }
    Invoke-Checked -FilePath $engine.Path -Arguments @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $testScript
    )
}

function Invoke-PythonTests {
    $uv = Get-Command uv -ErrorAction Stop
    $names = @(
        'TELEGRAM_API_ID',
        'TELEGRAM_API_HASH',
        'TELEGRAM_SESSION_NAME',
        'TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK'
    )
    $saved = @{}
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }

    try {
        $env:TELEGRAM_API_ID = '12345'
        $env:TELEGRAM_API_HASH = 'dummy_hash'
        $env:TELEGRAM_SESSION_NAME = 'test_session'
        $env:TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK = '0'
        Invoke-Checked -FilePath $uv.Path -Arguments @(
            'run', 'python', '-m', 'pytest', '--cov', '--cov-report=term-missing'
        )
    }
    finally {
        foreach ($name in $names) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
        }
    }
}

function Push-Origin {
    Write-Host 'Pushing main to private origin...' -ForegroundColor Cyan
    Invoke-Git -Arguments @('push', 'origin', 'main')
}

function Show-Actions {
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) {
        Write-Warning 'gh was not found; open GitHub Actions in the browser to inspect CI.'
        return
    }
    Invoke-Checked -FilePath $gh.Path -Arguments @(
        'run', 'list', '--branch', 'main', '--limit', '3'
    )
}

function Invoke-FullUpdate {
    Assert-CleanWorkingTree
    Fetch-Upstream
    Show-AvailableUpdates
    Merge-Upstream
    Invoke-LauncherTests

    try {
        Invoke-PythonTests
    }
    catch {
        Write-Warning "Local Python tests failed: $($_.Exception.Message)"
        Write-Warning 'Continuing to Push as requested; GitHub Actions will run the Linux CI suite.'
    }

    Push-Origin
    Show-Actions
}

function Show-Menu {
    try { Clear-Host } catch { }
    Write-Host 'Chigwell Update Menu' -ForegroundColor Cyan
    Write-Host "Log: $logPath" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  1. Show status and remotes'
    Write-Host '  2. Fetch upstream'
    Write-Host '  3. Show available updates'
    Write-Host '  4. Merge upstream/main'
    Write-Host '  5. Run launcher tests'
    Write-Host '  6. Run Python tests'
    Write-Host '  7. Push origin/main'
    Write-Host '  8. Show GitHub Actions'
    Write-Host '  9. Run full update (fetch, merge, tests, push)'
    Write-Host '  0. Exit'
    Write-Host ''
}

try {
    try {
        Start-Transcript -LiteralPath $logPath -IncludeInvocationHeader:$false | Out-Null
        $transcriptStarted = $true
    }
    catch {
        Write-Warning "Update logging is unavailable: $($_.Exception.Message)"
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        $runOnce = -not [string]::IsNullOrWhiteSpace($Choice)
        do {
            Show-Menu
            $selection = if ($runOnce) { $Choice } else { Read-Host 'Select an option' }
            if ($selection -eq '0') {
                break
            }

            try {
                Assert-Repository
                switch ($selection) {
                    '1' { Show-StatusAndRemotes }
                    '2' { Fetch-Upstream }
                    '3' { Show-AvailableUpdates }
                    '4' { Merge-Upstream }
                    '5' { Invoke-LauncherTests }
                    '6' { Invoke-PythonTests }
                    '7' { Push-Origin }
                    '8' { Show-Actions }
                    '9' { Invoke-FullUpdate }
                    default { Write-Warning 'Select a number from 0 to 9.' }
                }
            }
            catch {
                $exitCode = 1
                Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
            }

            if (-not $runOnce) {
                [void] (Read-Host 'Press Enter to return to the menu')
            }
        } while (-not $runOnce)
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
}

exit $exitCode
