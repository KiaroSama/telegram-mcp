#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdministrator) {
    $engine = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $engine) {
        $engine = Get-Command powershell -ErrorAction Stop
    }

    try {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        $process = Start-Process -FilePath $engine.Path -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    }
    catch {
        Write-Error "Administrator approval is required to update the system PATH. $($_.Exception.Message)"
        exit 1
    }
}

$commandScript = Join-Path $PSScriptRoot 'chigwell.ps1'
if (-not (Test-Path -LiteralPath $commandScript -PathType Leaf)) {
    throw "Command script not found: $commandScript"
}

$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$pathEntries = @($machinePath -split ';' | Where-Object { $_ } | ForEach-Object { $_.Trim() })
$hasProjectPath = @($pathEntries | Where-Object {
    [string]::Equals($_, $PSScriptRoot, [StringComparison]::OrdinalIgnoreCase)
}).Count -gt 0

$newMachinePath = if ($hasProjectPath) {
    $machinePath
}
else {
    (($pathEntries + $PSScriptRoot) -join ';')
}
[Environment]::SetEnvironmentVariable('Path', $newMachinePath, 'Machine')

$machinePathExt = [Environment]::GetEnvironmentVariable('PATHEXT', 'Machine')
$pathExtensions = @($machinePathExt -split ';' | Where-Object { $_ } | ForEach-Object { $_.Trim() })
$hasPowerShellExtension = @($pathExtensions | Where-Object {
    [string]::Equals($_, '.PS1', [StringComparison]::OrdinalIgnoreCase)
}).Count -gt 0

$newPathExt = if ($hasPowerShellExtension) {
    $machinePathExt
}
else {
    (($pathExtensions + '.PS1') -join ';')
}
[Environment]::SetEnvironmentVariable('PATHEXT', $newPathExt, 'Machine')

$savedPath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$savedPathExt = [Environment]::GetEnvironmentVariable('PATHEXT', 'Machine')
if ($savedPath -ne $newMachinePath -or $savedPathExt -ne $newPathExt) {
    throw 'Windows did not persist the system command settings.'
}

Write-Host "Installed the 'chigwell' command for PowerShell. Open a new terminal before using it." -ForegroundColor Green
