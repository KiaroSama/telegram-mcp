#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# The machine environment lives here. It is read and written through the registry
# provider rather than [Environment]::GetEnvironmentVariable/SetEnvironmentVariable
# because the latter expands %VAR% on read; writing that expansion back turns
# REG_EXPAND_SZ into a literal string and freezes every reference in the system
# PATH to whatever it meant at install time.
$environmentKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'

function Get-UpdatedEnvironmentValue {
    <#
      Returns $Current unchanged when $Entry is already present (case-insensitive,
      ignoring surrounding whitespace), otherwise $Current with $Entry appended.
      The existing text is never re-joined or re-trimmed: an entry such as
      %SystemRoot%\system32 must survive byte-for-byte.
    #>
    param(
        [Parameter(Mandatory = $true)] [AllowEmptyString()] [string] $Current,
        [Parameter(Mandatory = $true)] [string] $Entry
    )

    $present = @($Current -split ';' | Where-Object { $_ } | ForEach-Object { $_.Trim() } |
        Where-Object { [string]::Equals($_, $Entry, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
    if ($present) { return $Current }
    if ([string]::IsNullOrEmpty($Current)) { return $Entry }
    return ($Current.TrimEnd(';') + ';' + $Entry)
}

function Get-EnvironmentValueKind {
    <#
      Returns the registry type of a machine environment value so the write can
      restore it, falling back to $Fallback when the value does not exist yet.
      Path is REG_EXPAND_SZ in practice; PATHEXT is REG_SZ.
    #>
    param(
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [Microsoft.Win32.RegistryValueKind] $Fallback
    )

    try {
        $key = Get-Item -LiteralPath $environmentKey
        return $key.GetValueKind($Name)
    }
    catch {
        return $Fallback
    }
}

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

$environmentItem = Get-Item -LiteralPath $environmentKey

$machinePath = [string] $environmentItem.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
$machinePathKind = Get-EnvironmentValueKind -Name 'Path' `
    -Fallback ([Microsoft.Win32.RegistryValueKind]::ExpandString)
$newMachinePath = Get-UpdatedEnvironmentValue -Current $machinePath -Entry $PSScriptRoot

$machinePathExt = [string] $environmentItem.GetValue('PATHEXT', '', 'DoNotExpandEnvironmentNames')
$machinePathExtKind = Get-EnvironmentValueKind -Name 'PATHEXT' `
    -Fallback ([Microsoft.Win32.RegistryValueKind]::String)
$newPathExt = Get-UpdatedEnvironmentValue -Current $machinePathExt -Entry '.PS1'

if ($newMachinePath -ne $machinePath) {
    New-ItemProperty -LiteralPath $environmentKey -Name 'Path' `
        -Value $newMachinePath -PropertyType $machinePathKind -Force | Out-Null
}
if ($newPathExt -ne $machinePathExt) {
    New-ItemProperty -LiteralPath $environmentKey -Name 'PATHEXT' `
        -Value $newPathExt -PropertyType $machinePathExtKind -Force | Out-Null
}

# Read back through the same non-expanding path as the write. Comparing against
# [Environment]::GetEnvironmentVariable would compare an expanded read with an
# unexpanded write and throw on a perfectly successful install.
$verifyItem = Get-Item -LiteralPath $environmentKey
$savedPath = [string] $verifyItem.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
$savedPathExt = [string] $verifyItem.GetValue('PATHEXT', '', 'DoNotExpandEnvironmentNames')
if ($savedPath -ne $newMachinePath -or $savedPathExt -ne $newPathExt) {
    throw 'Windows did not persist the system command settings.'
}

# A registry write does not notify running processes; SetEnvironmentVariable used
# to do this for us. Without the broadcast, open shells and Explorer never learn
# about the change.
Add-Type -Namespace Win32 -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam,
    string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
$broadcastResult = [UIntPtr]::Zero
# 0xffff = HWND_BROADCAST, 0x1A = WM_SETTINGCHANGE, 2 = SMTO_ABORTIFHUNG, 5 s
# timeout so a hung window cannot wedge the installer.
[void] [Win32.NativeMethods]::SendMessageTimeout(
    [IntPtr] 0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref] $broadcastResult)

Write-Host "Installed the 'chigwell' command for PowerShell. Open a new terminal before using it." -ForegroundColor Green
