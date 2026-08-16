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

# A gh query without --repo resolves to upstream and reports its CI as ours.
if ($updaterText -notmatch "'run',\s*'list',\s*'--repo'") {
    throw 'Show-Actions must scope gh run list to the origin repository with --repo.'
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

# A failed test run and a test run that never started used to be reported with
# the same sentence, and both pushed. The consolation message was also false:
# GitHub Actions is not gating this push.
if ($updaterText -match [regex]::Escape('GitHub Actions will run the Linux CI suite')) {
    throw 'The updater still claims GitHub Actions gates the push.'
}

# Dot-sourcing the updater is not an option: it starts its menu loop at load
# time. Extract just the functions under test and run them against stubs, so no
# real git fetch/merge/push can ever happen.
$wanted = 'Start-PythonTests', 'Invoke-PythonTests', 'Invoke-FullUpdate'
$definitions = foreach ($name in $wanted) {
    $definitionMatch = [regex]::Match($updaterText, "(?ms)^function $name \{.*?^\}")
    if (-not $definitionMatch.Success) {
        throw "Could not extract $name from the updater."
    }
    $definitionMatch.Value
}

$fullUpdateHarness = {
    param($Definitions)

    foreach ($definition in $Definitions) {
        . ([ScriptBlock]::Create($definition))
    }

    function Assert-CleanWorkingTree { }
    function Fetch-Upstream { }
    function Show-AvailableUpdates { }
    function Merge-Upstream { }
    function Invoke-LauncherTests { }
    function Show-Actions { }
    function Push-Origin { $script:pushed = $true }

    try {
        Invoke-FullUpdate
    }
    catch {
        $script:failure = $_.Exception.Message
    }
}

$originalPath = $env:PATH
$originalPathExt = $env:PATHEXT
$fixtures = Join-Path $PSScriptRoot 'fixtures'

# Case 1: uv is missing, so the suite never ran. The push must not happen.
$script:pushed = $false
$script:failure = $null
$emptyPathDirectory = Join-Path ([IO.Path]::GetTempPath()) ("telegram-mcp-nouv-" + [guid]::NewGuid())
[void] (New-Item -ItemType Directory -Path $emptyPathDirectory)
try {
    $env:PATH = $emptyPathDirectory
    & $fullUpdateHarness $definitions
}
finally {
    $env:PATH = $originalPath
    $env:PATHEXT = $originalPathExt
    Remove-Item -LiteralPath $emptyPathDirectory -Recurse -Force -ErrorAction SilentlyContinue
}
if ($script:pushed) {
    throw 'Invoke-FullUpdate pushed even though the Python tests never ran.'
}
if (-not $script:failure) {
    throw 'Invoke-FullUpdate did not report that the Python tests could not be started.'
}
if ($script:failure -notmatch 'uv') {
    throw "Invoke-FullUpdate did not name uv as the reason the tests could not start: $($script:failure)"
}

# Case 2: pytest actually ran and reported failures. That still pushes, loudly.
$script:pushed = $false
$script:failure = $null
try {
    $env:PATH = "$fixtures;$originalPath"
    $env:PATHEXT = ".PS1;$originalPathExt"
    $env:TELEGRAM_MCP_FAKE_PYTEST_EXIT = '1'
    & $fullUpdateHarness $definitions
}
finally {
    $env:PATH = $originalPath
    $env:PATHEXT = $originalPathExt
    Remove-Item -LiteralPath 'Env:\TELEGRAM_MCP_FAKE_PYTEST_EXIT' -ErrorAction SilentlyContinue
}
if ($script:failure) {
    throw "Invoke-FullUpdate aborted on a completed but failing test run: $($script:failure)"
}
if (-not $script:pushed) {
    throw 'Invoke-FullUpdate did not push after a completed but failing test run.'
}

# Case 3: menu option 6 must still fail loudly - the menu's own catch is what
# turns a failing test run into a non-zero exit code.
$menuOptionHarness = {
    param($Definitions)

    foreach ($definition in $Definitions) {
        . ([ScriptBlock]::Create($definition))
    }

    try {
        Invoke-PythonTests
        $script:failure = $null
    }
    catch {
        $script:failure = $_.Exception.Message
    }
}

foreach ($case in @(
    @{ Exit = '1'; ShouldThrow = $true },
    @{ Exit = '0'; ShouldThrow = $false }
)) {
    $script:failure = $null
    try {
        $env:PATH = "$fixtures;$originalPath"
        $env:PATHEXT = ".PS1;$originalPathExt"
        $env:TELEGRAM_MCP_FAKE_PYTEST_EXIT = $case.Exit
        & $menuOptionHarness $definitions
    }
    finally {
        $env:PATH = $originalPath
        $env:PATHEXT = $originalPathExt
        Remove-Item -LiteralPath 'Env:\TELEGRAM_MCP_FAKE_PYTEST_EXIT' -ErrorAction SilentlyContinue
    }

    if ($case.ShouldThrow -and -not $script:failure) {
        throw "Invoke-PythonTests stayed silent for pytest exit $($case.Exit); menu option 6 would report success."
    }
    if (-not $case.ShouldThrow -and $script:failure) {
        throw "Invoke-PythonTests threw for a passing test run: $($script:failure)"
    }
}

Write-Output 'Update menu checks passed.'
