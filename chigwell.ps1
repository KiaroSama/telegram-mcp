[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArguments = @()
)

$targetScript = Join-Path $PSScriptRoot 'run.ps1'
$targetArguments = $ServerArguments
if ($ServerArguments.Count -gt 0 -and $ServerArguments[0] -ieq 'update') {
    $targetScript = Join-Path $PSScriptRoot 'Update-chigwell.ps1'
    $targetArguments = if ($ServerArguments.Count -gt 1) {
        @($ServerArguments[1..($ServerArguments.Count - 1)])
    }
    else {
        @()
    }
}

if (-not (Test-Path -LiteralPath $targetScript -PathType Leaf)) {
    Write-Error "Launcher not found: $targetScript"
    exit 1
}

$engine = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $engine) {
    $engine = Get-Command powershell -ErrorAction Stop
}

& $engine.Path -NoProfile -ExecutionPolicy Bypass -File $targetScript @targetArguments
exit $LASTEXITCODE
