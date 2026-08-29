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

# How many of each are kept. Both hold private material - a log names the accounts
# on this machine, a backup holds a full login to every one of them - so an
# unbounded pile of either turns one readable directory into a standing leak.
$script:LogRetention = 10
$script:EnvBackupRetention = 5
$script:MaxBackupCollisions = 100

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

# --- theme -------------------------------------------------------------------
#
# Ported from FFmWiz (`ffmwiz/core/colors.py`, `ffmwiz/appio.py`) so the launchers
# across these projects read as one family. Only the tokens this menu actually
# uses are carried over - copying the whole palette would be importing a hundred
# names to spend six.
#
# 256-colour SGR, not Write-Host -ForegroundColor: the console's sixteen named
# colours cannot express `38;5;166`, and the whole point of the theme is that
# back-orange and exit-blue are distinguishable at a glance.

$script:Esc = [char] 27

$script:Color = @{
    Reset       = "$script:Esc[0m"
    Bold        = "$script:Esc[1m"
    Red         = "$script:Esc[91m"
    Green       = "$script:Esc[92m"
    White       = "$script:Esc[97m"
    LightBlue   = "$script:Esc[38;5;117m"
    NoteYellow  = "$script:Esc[38;5;227m"
    HintYellow  = "$script:Esc[38;5;221m"
    Dim         = "$script:Esc[38;5;250m"
    BackPrompt  = "$script:Esc[38;5;166m"
    ExitPrompt  = "$script:Esc[38;5;32m"
}

function Test-ColorSupport {
    <#
      NO_COLOR is honoured the same way FFmWiz honours it. Beyond that, PowerShell
      7 always renders SGR, and Windows PowerShell 5.1 only does so on a host with
      virtual-terminal processing - Windows Terminal has it, an old conhost does
      not, and printing escapes into one that does not turns the menu into noise.
    #>
    if ($env:NO_COLOR) { return $false }
    if ($PSVersionTable.PSVersion.Major -ge 6) { return $true }
    if ($env:WT_SESSION) { return $true }
    try { return [bool] $Host.UI.SupportsVirtualTerminal } catch { return $false }
}

$script:UseColor = Test-ColorSupport

function Get-Painted {
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Text,
        [Parameter(Mandatory)] [string] $ColorName
    )
    if (-not $script:UseColor) { return $Text }
    return "$($script:Color[$ColorName])$Text$($script:Color.Reset)"
}

function Get-BackText {
    <#
      FFmWiz's `back_text`: the comma-separated parts are coloured by what they
      mean, not by position, and the whole thing is wrapped in braces. Keeping the
      shape identical is the point - someone who knows one launcher can read the
      other without being told.
    #>
    param([string] $Text = 'back=0, quit=exit')
    $parts = foreach ($part in ($Text -split ', ')) {
        $lowered = $part.ToLowerInvariant()
        if ($lowered -match 'back') { Get-Painted -Text $part -ColorName 'BackPrompt' }
        elseif ($lowered -match 'exit') { Get-Painted -Text $part -ColorName 'ExitPrompt' }
        else { Get-Painted -Text $part -ColorName 'White' }
    }
    return '{' + ($parts -join ', ') + '}'
}

function Write-Note { param([Parameter(Mandatory)] [string] $Message)
    Write-Host (Get-Painted -Text $Message -ColorName 'NoteYellow') }

function Write-Failure { param([Parameter(Mandatory)] [string] $Message)
    Write-Host (Get-Painted -Text $Message -ColorName 'Red') }

function Write-Hint { param([Parameter(Mandatory)] [string] $Message)
    Write-Host (Get-Painted -Text $Message -ColorName 'Dim') }

# `exit` typed at any prompt ends the program; `0` steps back to the menu. A
# sub-prompt cannot return two different kinds of "no", so quitting sets this and
# every loop above it unwinds.
$script:Quitting = $false

function Read-Answer {
    <#
      One reader for every prompt, so the two words behave identically everywhere.
      Returns $null for "go back" - which is also what blank means - and sets
      $script:Quitting for "exit".
    #>
    param(
        [Parameter(Mandatory)] [string] $Prompt,
        [switch] $NoBack
    )
    $hint = if ($NoBack) { Get-BackText -Text 'quit=exit' } else { Get-BackText }
    $answer = (Read-Host "$(Get-Painted -Text $Prompt -ColorName 'Bold') $hint").Trim()
    if ($answer -ieq 'exit') { $script:Quitting = $true; return $null }
    if (-not $NoBack -and ($answer -eq '0' -or $answer -eq '')) { return $null }
    return $answer
}

# --- prompts -----------------------------------------------------------------

function Read-Confirmation {
    param([Parameter(Mandatory)] [string] $Question)
    $default = Get-Painted -Text '[Y/n]' -ColorName 'Green'
    $answer = (Read-Host "$(Get-Painted -Text $Question -ColorName 'Bold') $default").Trim()
    if ($answer -ieq 'exit') { $script:Quitting = $true; return $false }
    return ([string]::IsNullOrWhiteSpace($answer) -or $answer -match '^(y|yes)$')
}

function ConvertTo-Label {
    <#
      Turn what a person typed into a label that can actually be stored.

      The label is spliced into an environment variable NAME
      (`TELEGRAM_SESSION_STRING_<LABEL>`), and python-dotenv refuses to parse a
      line whose key contains a space - it prints a warning and DROPS the line.
      So "KGB Verifier" written literally would produce an account that is saved
      and then never loads, which is the worst of both outcomes.

      Spaces and hyphens therefore become underscores rather than being rejected.
      Anything else is refused, because there is no safe mapping for it.
    #>
    param([Parameter(Mandatory)] [AllowEmptyString()] [string] $Raw)
    $trimmed = $Raw.Trim()
    if (-not $trimmed) { return $null }
    if ($trimmed -notmatch '^[A-Za-z0-9_ \-]+$') { return '' }
    return ($trimmed -replace '[\s\-]+', '_').Trim('_').ToLowerInvariant()
}

function Read-Label {
    <#
      Reads a label and reports the stored form when it differs from what was
      typed - the caller will use that stored form as `account=` later, so being
      told "kgb_verifier" now is what stops a puzzled `account=KGB Verifier`.
    #>
    param([Parameter(Mandatory)] [string] $Prompt)
    while ($true) {
        # `0` and `exit` are consumed by Read-Answer, so neither can ever be a
        # label. Nobody wants an account called "exit"; the trade is worth it.
        $raw = Read-Answer -Prompt $Prompt
        if ($null -eq $raw) { return $null }
        $label = ConvertTo-Label -Raw $raw
        if ($null -eq $label) { return $null }
        if ($label -eq '') {
            Write-Note 'A label may contain letters, digits, underscores, spaces and hyphens.'
            Write-Hint 'Spaces and hyphens are stored as underscores.'
            continue
        }
        if ($label -ne $raw.Trim().ToLowerInvariant()) {
            Write-Hint "Stored as '$label' - that is the value tools take as account=."
        }
        return $label
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
    param(
        [string] $Label,
        # Add-Account has just asked whether to generate one. Asking again here is
        # the same question twice in a row, which is what a caller reports as noise.
        [switch] $AlreadyConfirmed
    )

    Write-Host ''
    Write-Host 'Log in as the account you want to ADD, not one already configured.'
    if ($Label) {
        Write-Hint "It will save the result as '$Label' - press Enter when it offers to."
    }
    Write-Host ''
    if (-not $AlreadyConfirmed -and -not (Read-Confirmation 'Run the session generator now?')) { return }

    # No --qr / --phone here on purpose: without a flag the generator asks, so the
    # choice always matches whatever methods it actually supports.
    $script = 'session_string_generator.py'
    # The label it would otherwise ask for. Passing it is what stops the same
    # question being put twice, once by each half of this flow.
    $arguments = if ($Label) { @($script, '--label', $Label) } else { @($script) }
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

    Push-Location -LiteralPath $PSScriptRoot
    try {
        $script:GeneratorExitCode = $null
        if (Test-Path -LiteralPath $python -PathType Leaf) {
            # Straight to the interpreter. `uv run` would rebuild and reinstall the
            # project first whenever a source file has changed, printing build and
            # wheel-install progress on top of the login prompt.
            & $python @arguments
            $script:GeneratorExitCode = $LASTEXITCODE
        }
        else {
            $uv = Get-Command uv -ErrorAction SilentlyContinue
            if (-not $uv) {
                throw "Neither .venv\Scripts\python.exe nor uv was found. Create the virtual environment, or install uv, then try again."
            }
            Write-Hint 'No .venv found - falling back to uv, which may build the project first.'
            # UV_LINK_MODE is uv's own advice for the hardlink warning it prints when
            # the cache and the target sit on different filesystems.
            $previousLinkMode = $env:UV_LINK_MODE
            $env:UV_LINK_MODE = 'copy'
            try {
                & $uv.Path run --quiet @arguments
                $script:GeneratorExitCode = $LASTEXITCODE
            }
            finally { $env:UV_LINK_MODE = $previousLinkMode }
        }
    }
    finally { Pop-Location }

    if ($script:GeneratorExitCode -ne 0) {
        Write-Host ''
        Write-Failure 'The generator did not finish, so it produced no session string.'
        Write-Host 'Nothing was saved. Run it again once the problem above is resolved.'
    }
}

function Test-SessionString {
    <#
      Ask Telethon whether this parses as a session, rather than guessing from its
      length. A 42-character paste sailed past the old `length -lt 40` check and
      was written to .env as a working account; `StringSession` rejects it outright.

      The value goes in on STDIN, never as an argument: a command line is visible
      to anything that can list processes.
    #>
    param([Parameter(Mandatory)] [AllowEmptyString()] [string] $Value)

    # An empty value is a session with no auth key; say so rather than throwing on
    # the parameter binding, which is what a Mandatory [string] does to ''.
    if ([string]::IsNullOrWhiteSpace($Value)) { return 'empty' }

    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return 'unchecked' }

    $probe = @'
import sys
from telethon.sessions import StringSession
raw = sys.stdin.read().strip()
try:
    session = StringSession(raw)
except Exception:
    print("invalid")
else:
    print("valid" if session.auth_key and session.dc_id else "empty")
'@
    try {
        $verdict = ($Value | & $python -c $probe 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) { return 'unchecked' }
        return "$verdict".Trim()
    }
    catch { return 'unchecked' }
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
    Write-Host (Get-Painted -Text 'A session string authorises full access to that Telegram account.' -ColorName 'NoteYellow')
    if (Read-Confirmation 'Do you need to generate one first?') {
        Invoke-SessionGenerator -Label $label -AlreadyConfirmed

        # The generator can write the line itself now. Asking for a paste after it
        # already did would be asking someone to copy a 350-character secret across
        # a terminal for no reason - which is how a mis-paste got saved once.
        if ((Get-Accounts).Contains($label)) {
            Write-Host ''
            Write-Host "The generator saved '$label' to .env." -ForegroundColor Green
            Write-Host 'Restart the MCP server for it to take effect.' -ForegroundColor Cyan
            return
        }
        Write-Hint 'The generator did not save it, so paste the string it printed.'
    }

    $sessionString = Read-SessionString 'Paste the session string (input stays hidden)'
    if ([string]::IsNullOrWhiteSpace($sessionString)) {
        Write-Host 'Nothing was pasted; no change made.' -ForegroundColor Yellow
        return
    }
    switch (Test-SessionString -Value $sessionString) {
        'valid' { }
        'empty' {
            Write-Failure 'That parses as a session but carries no auth key - it is an empty session.'
            Write-Host 'Nothing was saved.'
            return
        }
        'invalid' {
            Write-Failure 'Telethon cannot read that as a session string, so it would never load.'
            Write-Host 'Check you copied the whole line the generator printed. Nothing was saved.'
            return
        }
        default {
            Write-Note 'Could not verify the session string (no .venv to check it with).'
            if (-not (Read-Confirmation 'Save it unverified?')) { Write-Host 'Cancelled.'; return }
        }
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

    # The PREFIX decides what the value MEANS. A file-based account is defined by
    # TELEGRAM_SESSION_NAME_*, and rewriting it as TELEGRAM_SESSION_STRING_* hands
    # the server a session PATH where it expects a session STRING - so the rename
    # succeeds, says so, and the account silently stops loading.
    $prefix = if ($oldKey.StartsWith('TELEGRAM_SESSION_NAME_')) {
        'TELEGRAM_SESSION_NAME_'
    }
    else { 'TELEGRAM_SESSION_STRING_' }
    $newKey = "$prefix$($to.ToUpperInvariant())"

    $backup = Backup-EnvFile
    # One write. The value is never read into a variable here - it moves inside
    # the transform, so nothing in this scope ever holds a session string.
    Rename-EnvKey -From $oldKey -To $newKey

    Write-Log "Renamed account '$from' to '$to'"
    Write-Host ''
    Write-Host "Renamed '$from' to '$to'." -ForegroundColor Green
    if ($backup) { Write-Host "Previous .env kept as $(Split-Path -Leaf $backup)" }
    Write-Host 'Restart the MCP server for it to take effect.' -ForegroundColor Cyan
}

# --- menu --------------------------------------------------------------------

$script:MenuItems = [ordered] @{
    '1' = 'List configured accounts'
    '2' = 'Add an account'
    '3' = 'Remove an account'
    '4' = 'Rename an account'
    '5' = 'Generate a session string only'
}

function Show-Menu {
    Write-Host ''
    Write-Host (Get-Painted -Text 'Telegram MCP account manager:' -ColorName 'LightBlue')
    foreach ($key in $script:MenuItems.Keys) {
        Write-Host "  $(Get-Painted -Text "$key." -ColorName 'LightBlue') $($script:MenuItems[$key])"
    }
    Write-Host ''
}

Start-Logging
Write-Log 'Account manager started'

try {
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        Write-Host ''
        Write-Note "No .env file exists at $envPath."
        Write-Host 'It also has to hold TELEGRAM_API_ID and TELEGRAM_API_HASH, which this menu'
        Write-Host 'does not manage - copy .env.example first, fill those in, then come back.'
        if (Read-Confirmation 'Create an empty .env now so accounts can be added?') {
            [IO.File]::WriteAllText($envPath, '', [Text.UTF8Encoding]::new($false))
            # Before anything is put in it: this file ends up holding session
            # strings, and a session string is the account.
            if (-not (Set-OwnerOnlyAcl -Path $envPath)) {
                Remove-Item -LiteralPath $envPath -Force -ErrorAction SilentlyContinue
                Write-Host 'The .env could not be made owner-only, so it was not created.'
                exit 1
            }
            Write-Log 'Created an empty .env'
        }
        else {
            Write-Host 'Nothing was changed.'
            exit 0
        }
    }

    while (-not $script:Quitting) {
        Show-Menu
        # -NoBack, and deliberately: the main menu has no previous step, so
        # advertising back=0 here would promise something that cannot happen.
        # This is FFmWiz's own rule, kept rather than reinvented.
        $choice = Read-Answer -Prompt 'Selection' -NoBack
        if ($script:Quitting) { break }
        if ([string]::IsNullOrEmpty($choice)) { continue }

        switch ($choice) {
            '1' { Show-Accounts }
            '2' { Add-Account }
            '3' { Remove-Account }
            '4' { Rename-Account }
            '5' { Invoke-SessionGenerator }
            default { Write-Failure "Enter a menu number from 1 to $($script:MenuItems.Count), or exit." }
        }
        if ($script:Quitting) { break }
    }
}
catch {
    $exitCode = 1
    # Shown in full, persisted as its shape. This log records account operations,
    # so an exception message here can carry a label, a path or part of a session
    # string - and the file outlives the terminal.
    Write-Host ''
    Write-Failure "Failed: $($_.Exception.Message)"
    $where = if ($_.InvocationInfo -and $_.InvocationInfo.ScriptName) {
        "$(Split-Path -Leaf $_.InvocationInfo.ScriptName):$($_.InvocationInfo.ScriptLineNumber)"
    }
    else { 'unknown' }
    Write-Log "$($_.Exception.GetType().Name) at $where" -Level ERROR
}
finally {
    Write-Log "Account manager stopped with exit code $exitCode"
    if ($script:LogPath) { Write-Hint "Log: $script:LogPath" }
}

exit $exitCode
