#Requires -Version 5.1
<#
    Add, remove and inspect the Telegram accounts this server exposes.

    Accounts live in `.env` as TELEGRAM_SESSION_STRING_<LABEL> lines, one per
    account, plus the unsuffixed TELEGRAM_SESSION_STRING which the server labels
    "default". This menu edits exactly those lines and leaves every other line in
    the file byte-for-byte alone.

    Two rules shape the whole script, because a session string is a live login to
    a Telegram account and is worth more than a password:

      * it is never printed, never logged, and never passed on a command line -
        it is read as a SecureString and held only long enough to write it;
      * `.env` is copied to .env.backup-<UTC> before any rewrite, so a mistake
        here costs one rename rather than every configured account.

    Adding an account needs a session string, which comes from
    `session_string_generator.py`. This script offers to run that for you, but the
    QR scan or the phone code is yours to complete - nothing here logs you in.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$exitCode = 0
$script:LogPath = $null
$envPath = Join-Path $PSScriptRoot '.env'

# --- logging -----------------------------------------------------------------

function Start-Logging {
    try {
        $logsDirectory = Join-Path $PSScriptRoot 'logs'
        [void] (New-Item -ItemType Directory -Path $logsDirectory -Force)
        $timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
        $path = Join-Path $logsDirectory "Manage-Accounts_$timestamp.log"
        $suffix = 1
        while (Test-Path -LiteralPath $path) {
            $path = Join-Path $logsDirectory "Manage-Accounts_${timestamp}_$suffix.log"
            $suffix++
        }
        [IO.File]::WriteAllText($path, '', [Text.UTF8Encoding]::new($false))
        $script:LogPath = $path
    }
    catch {
        Write-Warning "File logging is unavailable: $($_.Exception.Message)"
    }
}

function Write-Log {
    param(
        [Parameter(Mandatory)] [string] $Message,
        [ValidateSet('INFO', 'WARNING', 'ERROR')] [string] $Level = 'INFO'
    )
    # Labels and counts only. A session string must never reach this function.
    $line = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [$Level] [accounts] $Message"
    if ($script:LogPath) {
        [IO.File]::AppendAllText($script:LogPath, "$line$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }
}

# --- .env handling -----------------------------------------------------------

function Backup-EnvFile {
    <#
      A copy beside the original, named by the moment it was taken. Restoring is a
      rename, which is the point: the recovery path has to be obvious to someone
      who has just realised they deleted the wrong account.
    #>
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { return $null }
    $stamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
    $backup = "$envPath.backup-$stamp"
    Copy-Item -LiteralPath $envPath -Destination $backup -Force
    Write-Log "Backed up .env to $(Split-Path -Leaf $backup)"
    return $backup
}

function Get-EnvLines {
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { return @() }
    return [IO.File]::ReadAllLines($envPath, [Text.UTF8Encoding]::new($false))
}

function Get-Accounts {
    <#
      Label -> the .env key that defines it. Values are deliberately not returned:
      nothing in this script has a reason to hold one except while writing it.
    #>
    $accounts = [ordered] @{}
    foreach ($line in Get-EnvLines) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) { continue }
        $key = $trimmed.Substring(0, $trimmed.IndexOf('=')).Trim()
        $value = $trimmed.Substring($trimmed.IndexOf('=') + 1).Trim()
        if (-not $value) { continue }

        if ($key -eq 'TELEGRAM_SESSION_STRING' -or $key -eq 'TELEGRAM_SESSION_NAME') {
            if (-not $accounts.Contains('default')) { $accounts['default'] = $key }
        }
        elseif ($key -like 'TELEGRAM_SESSION_STRING_*') {
            $accounts[$key.Substring('TELEGRAM_SESSION_STRING_'.Length).ToLowerInvariant()] = $key
        }
        elseif ($key -like 'TELEGRAM_SESSION_NAME_*') {
            $accounts[$key.Substring('TELEGRAM_SESSION_NAME_'.Length).ToLowerInvariant()] = $key
        }
    }
    return $accounts
}

function Set-EnvValue {
    <#
      Replace the line defining $Key, or append one. Every other line survives
      unchanged - comments, ordering, blank lines and any key this script knows
      nothing about, which is most of the file.
    #>
    param(
        [Parameter(Mandatory)] [string] $Key,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Value
    )
    $lines = @(Get-EnvLines)
    $written = $false
    $updated = foreach ($line in $lines) {
        if ($line.Trim() -match "^$([regex]::Escape($Key))\s*=") {
            $written = $true
            "$Key=$Value"
        }
        else { $line }
    }
    if (-not $written) { $updated = @($updated) + "$Key=$Value" }
    [IO.File]::WriteAllText(
        $envPath,
        (($updated -join [Environment]::NewLine) + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
}

function Remove-EnvKey {
    param([Parameter(Mandatory)] [string] $Key)
    $kept = @(Get-EnvLines | Where-Object { $_.Trim() -notmatch "^$([regex]::Escape($Key))\s*=" })
    [IO.File]::WriteAllText(
        $envPath,
        (($kept -join [Environment]::NewLine) + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
}

# --- prompts -----------------------------------------------------------------

function Read-Confirmation {
    param([Parameter(Mandatory)] [string] $Question)
    $answer = Read-Host "$Question [Y/n]"
    return ([string]::IsNullOrWhiteSpace($answer) -or $answer -match '^(y|yes)$')
}

function Read-Label {
    <#
      The label becomes the `account` argument tools take, and the server
      lowercases it. Restricting it to A-Z, 0-9 and _ is not decoration: the label
      is spliced into an environment variable name, where anything else is either
      illegal or silently mangled.
    #>
    param([Parameter(Mandatory)] [string] $Prompt)
    while ($true) {
        $label = (Read-Host $Prompt).Trim()
        if (-not $label) { return $null }
        if ($label -notmatch '^[A-Za-z0-9_]+$') {
            Write-Host 'A label may contain only letters, digits and underscores.' -ForegroundColor Yellow
            continue
        }
        return $label.ToLowerInvariant()
    }
}

function Read-SessionString {
    <#
      Read-Host -AsSecureString keeps the value off the screen and out of the
      console history. It is converted back only at the moment of writing, and the
      unmanaged copy is freed immediately afterwards.
    #>
    param([Parameter(Mandatory)] [string] $Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

# --- actions -----------------------------------------------------------------

function Show-Accounts {
    $accounts = Get-Accounts
    if ($accounts.Count -eq 0) {
        Write-Host 'No accounts are configured yet.' -ForegroundColor Yellow
        Write-Host 'Choose "Add an account" to configure the first one.'
        return
    }
    Write-Host ''
    Write-Host "Configured accounts ($($accounts.Count)):" -ForegroundColor Cyan
    foreach ($label in $accounts.Keys) {
        $note = if ($label -eq 'default') { '  (used when a tool is called without account=)' } else { '' }
        Write-Host ("  {0,-16} {1}{2}" -f $label, $accounts[$label], $note)
    }
    if ($accounts.Count -gt 1) {
        Write-Host ''
        Write-Host 'Multi-account mode is active: write tools now require account=, and' -ForegroundColor Yellow
        Write-Host 'read-only tools fan out across every account when it is omitted.' -ForegroundColor Yellow
    }
}

function Invoke-SessionGenerator {
    Write-Host ''
    Write-Host 'The generator will ask you to scan a QR code with the Telegram app.' -ForegroundColor Cyan
    Write-Host 'Log in as the account you want to ADD, not the one already configured.'
    Write-Host 'It prints a session string at the end - copy it, then paste it here.'
    Write-Host ''
    if (-not (Read-Confirmation 'Run the session generator now?')) { return }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    Push-Location -LiteralPath $PSScriptRoot
    try {
        if ($uv) { & $uv.Path run session_string_generator.py --qr }
        else {
            $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
                throw 'Neither uv nor .venv\Scripts\python.exe was found. Install uv, or create the virtual environment first.'
            }
            & $python session_string_generator.py --qr
        }
    }
    finally { Pop-Location }
}

function Add-Account {
    $accounts = Get-Accounts
    Write-Host ''
    $label = Read-Label 'Label for the new account (e.g. work, personal) - blank to cancel'
    if (-not $label) { Write-Host 'Cancelled.'; return }

    if ($accounts.Contains($label)) {
        Write-Host "An account labelled '$label' already exists ($($accounts[$label]))." -ForegroundColor Yellow
        if (-not (Read-Confirmation 'Replace its session string?')) { Write-Host 'Cancelled.'; return }
    }

    Write-Host ''
    Write-Host 'A session string authorises full access to that Telegram account.' -ForegroundColor Yellow
    if (Read-Confirmation 'Do you need to generate one first?') { Invoke-SessionGenerator }

    $sessionString = Read-SessionString 'Paste the session string (input stays hidden)'
    if ([string]::IsNullOrWhiteSpace($sessionString)) {
        Write-Host 'Nothing was pasted; no change made.' -ForegroundColor Yellow
        return
    }
    if ($sessionString.Length -lt 40) {
        Write-Host 'That does not look like a session string - they are far longer.' -ForegroundColor Yellow
        if (-not (Read-Confirmation 'Save it anyway?')) { Write-Host 'Cancelled.'; return }
    }

    $backup = Backup-EnvFile
    $key = "TELEGRAM_SESSION_STRING_$($label.ToUpperInvariant())"
    Set-EnvValue -Key $key -Value $sessionString
    $sessionString = $null

    Write-Log "Added account '$label' as $key"
    Write-Host ''
    Write-Host "Added '$label'." -ForegroundColor Green
    if ($backup) { Write-Host "Previous .env kept as $(Split-Path -Leaf $backup)" }
    Write-Host 'Restart the MCP server for it to take effect.' -ForegroundColor Cyan
    if ((Get-Accounts).Count -gt 1) {
        Write-Host ''
        Write-Host 'You now have more than one account, so write tools will require' -ForegroundColor Yellow
        Write-Host "account=<label> from here on - for example account=$label." -ForegroundColor Yellow
    }
}

function Remove-Account {
    $accounts = Get-Accounts
    if ($accounts.Count -eq 0) { Write-Host 'There is nothing to remove.' -ForegroundColor Yellow; return }

    Show-Accounts
    Write-Host ''
    $label = Read-Label 'Label to remove - blank to cancel'
    if (-not $label) { Write-Host 'Cancelled.'; return }
    if (-not $accounts.Contains($label)) {
        Write-Host "No account is labelled '$label'." -ForegroundColor Yellow
        return
    }
    if ($accounts.Count -eq 1) {
        Write-Host ''
        Write-Host 'This is the only account. Removing it leaves the server unable to start' -ForegroundColor Yellow
        Write-Host 'until another one is configured.' -ForegroundColor Yellow
    }

    Write-Host ''
    Write-Host "This removes $($accounts[$label]) from .env." -ForegroundColor Yellow
    Write-Host 'The Telegram session itself stays authorised - to truly revoke it, end the'
    Write-Host 'session from Telegram: Settings > Devices.'
    if (-not (Read-Confirmation "Remove '$label'?")) { Write-Host 'Cancelled.'; return }

    $backup = Backup-EnvFile
    Remove-EnvKey -Key $accounts[$label]
    Write-Log "Removed account '$label' ($($accounts[$label]))"
    Write-Host ''
    Write-Host "Removed '$label'." -ForegroundColor Green
    if ($backup) { Write-Host "Previous .env kept as $(Split-Path -Leaf $backup)" }
    Write-Host 'Restart the MCP server for it to take effect.' -ForegroundColor Cyan
}

function Rename-Account {
    $accounts = Get-Accounts
    if ($accounts.Count -eq 0) { Write-Host 'There is nothing to rename.' -ForegroundColor Yellow; return }

    Show-Accounts
    Write-Host ''
    $from = Read-Label 'Label to rename - blank to cancel'
    if (-not $from) { Write-Host 'Cancelled.'; return }
    if (-not $accounts.Contains($from)) { Write-Host "No account is labelled '$from'." -ForegroundColor Yellow; return }
    if ($from -eq 'default') {
        Write-Host "'default' comes from the unsuffixed TELEGRAM_SESSION_STRING and cannot be" -ForegroundColor Yellow
        Write-Host 'renamed here. Remove it and add it back under a label instead.' -ForegroundColor Yellow
        return
    }

    $to = Read-Label 'New label'
    if (-not $to) { Write-Host 'Cancelled.'; return }
    if ($accounts.Contains($to)) { Write-Host "'$to' is already taken." -ForegroundColor Yellow; return }

    $oldKey = $accounts[$from]
    $value = $null
    foreach ($line in Get-EnvLines) {
        if ($line.Trim() -match "^$([regex]::Escape($oldKey))\s*=(.*)$") { $value = $Matches[1].Trim() }
    }
    if (-not $value) { Write-Host "Could not read $oldKey from .env." -ForegroundColor Red; return }

    $backup = Backup-EnvFile
    Remove-EnvKey -Key $oldKey
    Set-EnvValue -Key "TELEGRAM_SESSION_STRING_$($to.ToUpperInvariant())" -Value $value
    $value = $null

    Write-Log "Renamed account '$from' to '$to'"
    Write-Host ''
    Write-Host "Renamed '$from' to '$to'." -ForegroundColor Green
    if ($backup) { Write-Host "Previous .env kept as $(Split-Path -Leaf $backup)" }
    Write-Host 'Restart the MCP server for it to take effect.' -ForegroundColor Cyan
}

# --- menu --------------------------------------------------------------------

function Show-Menu {
    Write-Host ''
    Write-Host '=========================================' -ForegroundColor Cyan
    Write-Host '  Telegram MCP - account manager' -ForegroundColor Cyan
    Write-Host '=========================================' -ForegroundColor Cyan
    Write-Host '  1. List configured accounts'
    Write-Host '  2. Add an account'
    Write-Host '  3. Remove an account'
    Write-Host '  4. Rename an account'
    Write-Host '  5. Generate a session string only'
    Write-Host '  0. Quit'
    Write-Host ''
}

Start-Logging
Write-Log 'Account manager started'

try {
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        Write-Host ''
        Write-Host "No .env file exists at $envPath." -ForegroundColor Yellow
        Write-Host 'It also has to hold TELEGRAM_API_ID and TELEGRAM_API_HASH, which this menu'
        Write-Host 'does not manage - copy .env.example first, fill those in, then come back.'
        if (Read-Confirmation 'Create an empty .env now so accounts can be added?') {
            [IO.File]::WriteAllText($envPath, '', [Text.UTF8Encoding]::new($false))
            Write-Log 'Created an empty .env'
        }
        else {
            Write-Host 'Nothing was changed.'
            exit 0
        }
    }

    while ($true) {
        Show-Menu
        switch ((Read-Host 'Choose').Trim()) {
            '1' { Show-Accounts }
            '2' { Add-Account }
            '3' { Remove-Account }
            '4' { Rename-Account }
            '5' { Invoke-SessionGenerator }
            '0' { break }
            ''  { }
            default { Write-Host 'Pick a number from the menu.' -ForegroundColor Yellow }
        }
        if ($Matches) { $null = $Matches }
        if ((Read-Host 'Press Enter to return to the menu, or type q to quit') -match '^q') { break }
    }
}
catch {
    $exitCode = 1
    $message = $_.Exception.Message
    Write-Host ''
    Write-Host "Failed: $message" -ForegroundColor Red
    Write-Log $message -Level ERROR
}
finally {
    Write-Log "Account manager stopped with exit code $exitCode"
    if ($script:LogPath) { Write-Host "Log: $script:LogPath" -ForegroundColor DarkGray }
}

exit $exitCode
