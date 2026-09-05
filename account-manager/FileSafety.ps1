<#
    Private files, written safely, and the log they are written to.

    Split out of `Manage-Accounts.ps1` when it passed 1100 lines. Everything here
    is about the DISK rather than about accounts: where runtime state lives, how a
    file is replaced without a window in which it is half-written, how an old one
    is pruned, and how a line reaches the log.

    It is dot-sourced, so it runs in the launcher's own scope and shares its
    `$script:` variables. Nothing here reads `$PSScriptRoot` - that automatic
    variable points at the file it is WRITTEN in, so a function that moved here
    and still used it would resolve against `lib\` instead of the project root.
    That is exactly why the venv-calling functions stayed in the launcher.
#>

# --- private files ------------------------------------------------------------

function Get-StateDirectory {
    <#
      Where runtime state goes: NOT beside the source, which may be read-only,
      may be a git checkout, and is where a `logs/` directory ends up committed
      or synced. Same rule as `telegram_mcp.aliases.aliases_file_path`, so an
      operator has one place to look and one place to lock down.
    #>
    $base = if ($env:XDG_STATE_HOME) { $env:XDG_STATE_HOME } else { Join-Path $HOME '.local/state' }
    return Join-Path $base 'telegram-mcp'
}

function Set-OwnerOnlyAcl {
    <#
      Leave exactly one access entry on a private file or directory, and prove it.

      `icacls /inheritance:r /grant:r` was not enough, and the gap is narrow
      enough to have looked like a fix: `/inheritance:r` drops the INHERITED
      entries and `/grant:r` REPLACES the entry for the principal it names -
      every other EXPLICIT entry survives, and the tool still exits 0. A file
      carrying an explicit `BUILTIN\Users` grant therefore kept it while this
      function reported success. Measured on a GitHub Windows runner, whose
      workspace files are born with three explicit entries: all three remained.

      So the whole list is written rather than edited, and then READ BACK: the
      return value says what the object now allows, not that a call succeeded.
      A directory's entry is inheritable, which is what makes the files created
      inside one owner-only from birth. Mirrors
      `telegram_mcp.owner_only.restrict_to_owner_strict`.

      Returns whether it applied, never throws: a permissions detail must not
      abort the operation it was protecting half-way.
    #>
    param([Parameter(Mandatory)] [string] $Path)
    if ($env:OS -ne 'Windows_NT') { return $false }
    try {
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        if (-not $me) { return $false }
        $directory = [IO.Directory]::Exists($Path)

        # A FRESH descriptor rather than the object's own: writing one read back
        # from disk also writes the AUDIT section, which needs SeSecurityPrivilege
        # and fails for an ordinary account. A new one marks only the DACL.
        $acl = if ($directory) {
            [Security.AccessControl.DirectorySecurity]::new()
        }
        else {
            [Security.AccessControl.FileSecurity]::new()
        }
        # $true, $false: protect the list and do NOT copy the inherited entries
        # into it. Without the second argument they are preserved as explicit
        # ones, which is the same leak wearing different bookkeeping.
        $acl.SetAccessRuleProtection($true, $false)
        $inheritance = if ($directory) {
            [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        }
        else {
            [Security.AccessControl.InheritanceFlags]::None
        }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $me, 'FullControl', $inheritance,
                [Security.AccessControl.PropagationFlags]::None, 'Allow'))

        $target = if ($directory) { [IO.DirectoryInfo]::new($Path) }
        else { [IO.FileInfo]::new($Path) }
        if ('System.IO.FileSystemAclExtensions' -as [type]) {
            [IO.FileSystemAclExtensions]::SetAccessControl($target, $acl)
        }
        else {
            $target.SetAccessControl($acl)  # Windows PowerShell 5.1
        }

        return (Test-OwnerOnlyAcl -Path $Path)
    }
    catch { return $false }
}

function Test-OwnerOnlyAcl {
    <#
      Whether the object's DACL names this account and nothing else.

      Read off the object rather than inferred from the call that set it: a
      tool exiting 0 says the tool ran, this says what the object allows.
    #>
    param([Parameter(Mandatory)] [string] $Path)
    if ($env:OS -ne 'Windows_NT') { return $false }
    try {
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $entries = @((Get-Acl -LiteralPath $Path).Access)
        if ($entries.Count -ne 1) { return $false }
        $held = $entries[0].IdentityReference
        if ($held -isnot [Security.Principal.SecurityIdentifier]) {
            $held = $held.Translate([Security.Principal.SecurityIdentifier])
        }
        return $held -eq $me
    }
    catch { return $false }
}
function Write-FileAtomic {
    <#
      Write a file so a crash cannot leave it half-written.

      `[IO.File]::WriteAllText` truncates first and writes second, so an
      interrupted rewrite of `.env` leaves a file missing the accounts that had
      not been written yet - and the backup beside it is the only way back. A
      temp file, flushed to disk, then installed by an atomic replace, has no
      such window.
    #>
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Text
    )
    $temp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $stream = [IO.File]::Open(
            $temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            # The DACL goes on while the file is still EMPTY, and while this
            # process holds it with FileShare::None so nothing else can open that
            # name at all. Hardening AFTER the write left the session strings on
            # disk under inherited permissions for the length of the write.
            #
            # Fail closed: no file at all beats a readable one holding accounts.
            if (-not (Set-OwnerOnlyAcl -Path $temp)) {
                throw "Refusing to write ${Path}: its temporary file could not be made owner-only."
            }
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)  # $true: to the disk, not just to the OS cache
        }
        finally { $stream.Dispose() }
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            # [NullString]::Value, not $null: PowerShell binds $null to a string
            # parameter as "", and File.Replace reads that as "back it up to a
            # file with no name" and refuses.
            [IO.File]::Replace($temp, $Path, [NullString]::Value)
        }
        else {
            [IO.File]::Move($temp, $Path)
        }
    }
    catch {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        throw
    }
    # ReplaceFile carries the destination's own ACL onto the replacement, so this
    # normally confirms rather than acts - which is exactly why a failure here is
    # worth raising: it means the file in place is not owner-only.
    if (-not (Set-OwnerOnlyAcl -Path $Path)) {
        throw "$Path was written but could not be verified as owner-only."
    }
}

function Remove-StaleFiles {
    <#
      Keep the newest $Keep files matching $Filter and delete the rest.
      Named by a UTC timestamp, so the name order IS the age order.
    #>
    param(
        [Parameter(Mandatory)] [string] $Directory,
        [Parameter(Mandatory)] [string] $Filter,
        [Parameter(Mandatory)] [int] $Keep
    )
    $files = @(
        Get-ChildItem -LiteralPath $Directory -Filter $Filter -File -Force -ErrorAction SilentlyContinue |
            Sort-Object Name
    )
    for ($index = 0; $index -lt $files.Count - $Keep; $index++) {
        Remove-Item -LiteralPath $files[$index].FullName -Force -ErrorAction SilentlyContinue
    }
}

# --- logging -----------------------------------------------------------------

function Start-Logging {
    try {
        $logsDirectory = Join-Path (Get-StateDirectory) 'logs'
        [void] (New-Item -ItemType Directory -Path $logsDirectory -Force)
        $timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
        $path = Join-Path $logsDirectory "Manage-Accounts_$timestamp.log"
        $suffix = 1
        while (Test-Path -LiteralPath $path) {
            $path = Join-Path $logsDirectory "Manage-Accounts_${timestamp}_$suffix.log"
            $suffix++
        }
        [IO.File]::WriteAllText($path, '', [Text.UTF8Encoding]::new($false))
        # A log that cannot be made private does not get written to. The catch
        # below turns this into a warning and leaves $script:LogPath unset, which
        # is what disables file logging for the rest of the run.
        if (-not (Set-OwnerOnlyAcl -Path $path)) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            throw 'the log file could not be made owner-only'
        }
        Remove-StaleFiles -Directory $logsDirectory -Filter 'Manage-Accounts_*.log' `
            -Keep $script:LogRetention
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
