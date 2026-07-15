[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArguments = @()
)

$runScript = Join-Path $PSScriptRoot 'run.ps1'
if (-not (Test-Path -LiteralPath $runScript -PathType Leaf)) {
    Write-Error "Launcher not found: $runScript"
    exit 1
}

$engine = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $engine) {
    $engine = Get-Command powershell -ErrorAction Stop
}

& $engine.Path -NoProfile -ExecutionPolicy Bypass -File $runScript @ServerArguments
exit $LASTEXITCODE
