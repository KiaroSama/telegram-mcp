<#
    Reading and rewriting `.env`, one account line at a time.

    Split out of `Manage-Accounts.ps1`. This is the only place that parses the
    file, and it edits exactly the TELEGRAM_SESSION_* lines while leaving every
    other line byte-for-byte alone - which is the whole contract of the menu that
    calls it.

    Dot-sourced into the launcher's scope, so `$envPath` and the retention
    constants defined there are visible here. Uses `Write-FileAtomic` from
    `FileSafety.ps1`, so that file must be dot-sourced first.
#>

# --- .env handling -----------------------------------------------------------

function Backup-EnvFile {
    <#
      A copy beside the original, named by the moment it was taken. Restoring is a
      rename, which is the point: the recovery path has to be obvious to someone
      who has just realised they deleted the wrong account.

      Never overwrites: `Copy-Item -Force` would silently replace a backup taken
      in the same second, which is exactly when two operations in a row need
      both. Owner-only, and pruned - each one is a complete set of logins, so
      keeping every backup ever taken leaks every session ever configured.
    #>
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { return $null }
    $stamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
    for ($attempt = 0; $attempt -lt $script:MaxBackupCollisions; $attempt++) {
        $suffix = if ($attempt -eq 0) { '' } else { "-$attempt" }
        $backup = "$envPath.backup-$stamp$suffix"
        try {
            [IO.File]::Copy($envPath, $backup, $false)
        }
        catch [IO.IOException] {
            # Only a name that is genuinely taken is worth another attempt. A
            # permission failure, a full disk or an unreadable source all raise
            # IOException too, and retrying those a hundred times turns one clear
            # error into a hang and then a wrong message about collisions.
            if (-not (Test-Path -LiteralPath $backup)) { throw }
            continue
        }
        # A backup of .env is a second copy of every session string.
        if (-not (Set-OwnerOnlyAcl -Path $backup)) {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
            throw "Refusing to keep a backup of $envPath that is not owner-only."
        }
        Remove-StaleFiles -Directory (Split-Path -Parent $envPath) `
            -Filter "$(Split-Path -Leaf $envPath).backup-*" -Keep $script:EnvBackupRetention
        Write-Log "Backed up .env to $(Split-Path -Leaf $backup)"
        return $backup
    }
    throw "Could not find a free backup name for $envPath within one second."
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
    Write-FileAtomic -Path $envPath `
        -Text (($updated -join [Environment]::NewLine) + [Environment]::NewLine)
}

function Rename-EnvKey {
    <#
      Move a key's value to a new key in ONE pass and ONE atomic write.

      Rename used to call Remove-EnvKey and then Set-EnvValue, which is two
      separate atomic writes with a window between them. An error, a full disk or
      a closed lid in that window left the file with the account deleted and not
      re-added - the session string is only in a backup at that point, and the
      operator asked to RENAME an account, not to lose one.

      Every other line survives untouched: comments, ordering, blanks and keys
      this script knows nothing about.
    #>
    param(
        [Parameter(Mandatory)] [string] $From,
        [Parameter(Mandatory)] [string] $To
    )
    $moved = $false
    $updated = foreach ($line in @(Get-EnvLines)) {
        if ($line.Trim() -match "^$([regex]::Escape($From))\s*=(.*)$") {
            $moved = $true
            "$To=$($Matches[1].Trim())"
        }
        else { $line }
    }
    if (-not $moved) { throw "'$From' is not defined in .env, so there is nothing to rename." }
    Write-FileAtomic -Path $envPath `
        -Text ((@($updated) -join [Environment]::NewLine) + [Environment]::NewLine)
}

function Remove-EnvKey {
    param([Parameter(Mandatory)] [string] $Key)
    $kept = @(Get-EnvLines | Where-Object { $_.Trim() -notmatch "^$([regex]::Escape($Key))\s*=" })
    Write-FileAtomic -Path $envPath `
        -Text (($kept -join [Environment]::NewLine) + [Environment]::NewLine)
}
