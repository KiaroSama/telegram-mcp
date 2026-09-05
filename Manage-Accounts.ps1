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
$script:EnvBackupRetention = 1
$script:MaxBackupCollisions = 100

# --- the pieces this launcher is made of --------------------------------------
#
# Dot-sourced rather than imported as a module: these run in THIS scope, so the
# `$script:` state above and `$envPath` are visible to them without being passed
# around. `$PSScriptRoot` is per-FILE, which is why every function that resolves
# the project root - the ones that call the venv - stayed in this file.
foreach ($piece in 'FileSafety', 'EnvFile', 'Console') {
    $module = Join-Path $PSScriptRoot (Join-Path 'account-manager' "$piece.ps1")
    if (-not (Test-Path -LiteralPath $module)) {
        Write-Host "Missing $module - this launcher needs the lib folder beside it." -ForegroundColor Red
        exit 1
    }
    . $module
}

# --- actions -----------------------------------------------------------------

function Show-Accounts {
    <#
      The numbers are not decoration: `Remove-Account` asks for one, so the
      listing and the choice must agree. The caller passes the very dictionary it
      will index, rather than each re-reading `.env`, because two reads are two
      chances for the numbering to mean different things.
    #>
    param($Accounts)

    $accounts = if ($null -ne $Accounts) { $Accounts } else { Get-Accounts }
    if ($accounts.Count -eq 0) {
        Write-Host 'No accounts are configured yet.' -ForegroundColor Yellow
        Write-Host 'Choose "Add an account" to configure the first one.'
        return
    }
    Write-Host ''
    Write-Host "Configured accounts ($($accounts.Count)):" -ForegroundColor Cyan
    # Both halves, because "is this account actually usable" is the question the
    # list is opened to answer, and the Telethon half alone leaves eleven tools
    # dark without saying so.
    $states = Get-SecretChatStates
    $unfinished = @()
    $number = 0
    foreach ($label in $accounts.Keys) {
        $number++
        $note = if ($label -eq 'default') { '  (used when a tool is called without account=)' } else { '' }
        Write-Host ("  {0,2}. {1,-16} {2}{3}" -f $number, $label, $accounts[$label], $note)
        $state = if ($states.ContainsKey($label)) { $states[$label] } else { '' }
        $summary = Get-SecretChatSummary -State $state
        # Indented under the name, past the number, so the two lines read as one
        # entry rather than as two accounts.
        $continuation = '      {0,-16} {1}'
        if ($state -eq 'authorizationStateReady') {
            Write-Host ($continuation -f '', $summary) -ForegroundColor Green
        }
        else {
            Write-Host ($continuation -f '', $summary) -ForegroundColor Yellow
            $unfinished += $label
        }
    }
    if ($unfinished.Count -gt 0) {
        Write-Host ''
        Write-Host "Not finished: $($unfinished -join ', ')" -ForegroundColor Yellow
        # Write-Host, not Write-Hint: the rest of this listing paints directly,
        # and Write-Hint needs colour state a caller that only wants the list
        # has no reason to have set up.
        # Name a remedy that EXISTS. This line used to point at a menu entry
        # that had been removed, which is worse than saying nothing: the reader
        # scans the menu for it and concludes the tool is broken.
        Write-Host 'Option 2, same label: it offers to finish just that half.' -ForegroundColor Yellow
        Write-Host 'No scan and no code - only the two-step password.' -ForegroundColor Yellow
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
        # Check, do not assume: the generator writes .env before finishing the
        # secret-chat half, so a late failure leaves a PERFECTLY GOOD account
        # behind. Announcing "nothing was saved" there sent the owner back to
        # log in again - which is the one cost this whole flow exists to avoid.
        $saved = if ($Label) { (Get-Accounts).Contains($Label) } else { $false }
        if ($saved) {
            Write-Failure 'The generator stopped before it finished everything.'
            Write-Host "'$Label' IS saved and usable - do not log in again." -ForegroundColor Yellow
            Write-Host 'Only the step after it failed; the message above says which.'
        }
        else {
            Write-Failure 'The generator did not finish, so it produced no session string.'
            Write-Host 'Nothing was saved. Run it again once the problem above is resolved.'
        }
    }
}

function Get-SecretChatStates {
    <#
      Which accounts have finished their TDLib half, as label -> state.

      Read from the code rather than guessed from a file's existence: a TDLib
      database can exist and hold a half-finished authorisation, which is
      exactly the state this project spent an afternoon in.
    #>
    # Never throws. This is a status probe attached to a listing, and a listing
    # that dies because a probe could not run is worse than one that says
    # "unknown" - which is what an empty result renders as.
    $states = @{}
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) { return $states }
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $states }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        $lines = & $python (Join-Path 'scripts' 'secret_chat_login.py') '--status' 2>$null
    }
    catch { $lines = @() }
    finally { Pop-Location }

    foreach ($line in $lines) {
        if ($line -match '^\s*([A-Za-z0-9_]+)=(\w+)\s*$') { $states[$Matches[1]] = $Matches[2] }
    }
    return $states
}


function Get-SecretChatSummary {
    param([Parameter(Mandatory)] [AllowEmptyString()] [string] $State)
    switch ($State) {
        'authorizationStateReady' { return 'secret chats: ready' }
        '' { return 'secret chats: unknown' }
        default { return 'secret chats: NOT finished' }
    }
}


function Invoke-SecretChatLogin {
    <#
      Finish the account by signing it in to TDLib too, which is what secret
      chats and the newer admin rights run on.

      This asks for NOTHING. TDLib keeps its own authorisation and cannot import
      a Telethon session - but Telegram's device-linking flow lets a new client
      publish a login token and an already-authorised client accept it, so the
      login that just happened authorises this one. No second code, and no QR
      code is shown or scanned: that name is only what the phone app calls the
      same exchange.

      What it does add is a DEVICE in the account's session list, because TDLib
      is a separate client. That part is the protocol. Declining is free and the
      same script runs later.
    #>
    param(
        [Parameter(Mandatory)] [string] $Label,
        # The caller has already put the question. Asking again here is the same
        # question twice in a row, which is what a caller reports as noise.
        [switch] $AlreadyConfirmed
    )

    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return }

    # Ask the code which prerequisite is missing rather than guessing: the
    # library is an optional extra, and "not installed" and "not signed in" are
    # fixed in completely different places.
    $probe = 'from telegram_mcp.tdlib import tdjson_status; print("yes" if tdjson_status()["available"] else "no")'
    Push-Location -LiteralPath $PSScriptRoot
    try { $available = (& $python -c $probe 2>$null | Select-Object -Last 1) }
    catch { $available = 'no' }
    finally { Pop-Location }

    Write-Host ''
    if ($available -ne 'yes') {
        Write-Hint 'Telegram''s own library is not usable here, so secret chats are off.'
        Write-Hint '  Repair:   uv pip install -e .'
        Write-Hint "  Sign in:  python scripts\secret_chat_login.py $Label"
        Write-Log "TDLib login for '$Label' not offered: the library is not usable here" -Level WARNING
        return
    }

    if (-not $AlreadyConfirmed) {
        Write-Host 'One step left: sign this account in to TDLib as well.' -ForegroundColor Cyan
        Write-Hint 'That is what secret chats and the newer admin rights run on. It uses the'
        Write-Hint 'login you just did - no second code, and nothing to scan.'
        Write-Hint 'It does add one device to this account''s Telegram session list.'
        Write-Host ''
    }
    if (-not $AlreadyConfirmed -and -not (Read-Confirmation 'Finish it now?')) {
        Write-Host "Skipped. Run scripts\secret_chat_login.py $Label whenever you want it."
        Write-Log "TDLib login for '$Label' offered and declined"
        return
    }

    $code = $null
    Push-Location -LiteralPath $PSScriptRoot
    try {
        & $python (Join-Path 'scripts' 'secret_chat_login.py') $Label
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }

    Write-Host ''
    if ($code -eq 0) {
        Write-Host "Done - secret chats are ready for '$Label'." -ForegroundColor Green
        Write-Log "TDLib login completed for '$Label'"
    }
    else {
        Write-Failure 'That sign-in did not finish, so secret chats are not available yet.'
        Write-Hint "Nothing else was affected - '$Label' still works for every other tool."
        Write-Hint "Run scripts\secret_chat_login.py $Label to try again."
        Write-Log "TDLib login for '$Label' attempted and did not finish (exit $code)" -Level WARNING
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
        # An account whose Telethon half works and whose TDLib half does not needs
        # neither a new scan nor a new session string - only the two-step password
        # Telegram wants even from a linked device. Offering "replace it all" for
        # that was the whole reason a half-finished account had nowhere to go once
        # the separate menu entry was removed.
        $states = Get-SecretChatStates
        $half = if ($states.ContainsKey($label)) { $states[$label] } else { '' }
        if ($half -and $half -ne 'authorizationStateReady') {
            Write-Host ''
            Write-Host "'$label' is already configured; only its secret-chat half is unfinished." -ForegroundColor Yellow
            Write-Hint 'Finishing it needs no scan and no code - just the two-step password.'
            Write-Host ''
            if (Read-Confirmation 'Finish that half now?') {
                Invoke-SecretChatLogin -Label $label -AlreadyConfirmed
                return
            }
        }
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
            Write-Host 'A running server picks this up on its own - no restart needed.' -ForegroundColor Cyan
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
    Write-Host 'A running server picks this up on its own - no restart needed.' -ForegroundColor Cyan
    if ((Get-Accounts).Count -gt 1) {
        Write-Host ''
        Write-Host 'You now have more than one account, so write tools will require' -ForegroundColor Yellow
        Write-Host "account=<label> from here on - for example account=$label." -ForegroundColor Yellow
    }

    # Last, because the account is already usable without it.
    Invoke-SecretChatLogin -Label $label
}


function Read-AccountNumber {
    <#
      Pick an account by the number the listing just printed.

      Typing the label was the old way and it made "list it, read the name, go
      back, type it exactly" a four-step job for a one-word answer - and an
      underscore in the stored form that the eye reads as a space is enough to
      make the typed version miss.

      Indexes the caller's own dictionary, so the numbering cannot disagree with
      what was shown. Returns the label, or $null for cancel.
    #>
    param(
        [Parameter(Mandatory)] $Accounts,
        [Parameter(Mandatory)] [string] $Prompt
    )
    $labels = @($Accounts.Keys)
    while ($true) {
        $raw = Read-Answer -Prompt $Prompt
        if ($null -eq $raw) { return $null }
        $trimmed = $raw.Trim()
        if ($trimmed -eq '') { return $null }
        if ($trimmed -match '^[0-9]+$') {
            $index = [int] $trimmed
            if ($index -ge 1 -and $index -le $labels.Count) { return $labels[$index - 1] }
        }
        Write-Note "Enter a number from 1 to $($labels.Count)."
    }
}

function Remove-TdlibDatabase {
    <#
      Delete the account's TDLib database along with its .env line.

      These are two stores and only one used to be cleared. The database
      outlived the account, so removing an account and adding it again handed
      the NEW session the OLD one's dead auth key, and every attempt after that
      failed with AUTH_KEY_UNREGISTERED - a state no amount of logging in again
      can clear, while each attempt costs a real login.

      Best effort by design: the account is already gone from `.env` by this
      point, and a leftover database is a nuisance rather than a failure. The
      code recovers from one anyway (`telegram_mcp.tdlib.complete_login`), so a
      failure here must not abort a removal that has already happened.
    #>
    param([Parameter(Mandatory)] [string] $Label)

    $database = Join-Path (Join-Path (Get-StateDirectory) 'tdlib') $Label
    if (-not (Test-Path -LiteralPath $database)) { return }
    try {
        Remove-Item -LiteralPath $database -Recurse -Force -ErrorAction Stop
        Write-Log "Removed the TDLib database for '$Label'"
    }
    catch {
        Write-Log "Could not remove the TDLib database for '$Label': $($_.Exception.Message)" -Level WARNING
    }
}

function Remove-Account {
    $accounts = Get-Accounts
    if ($accounts.Count -eq 0) { Write-Host 'There is nothing to remove.' -ForegroundColor Yellow; return }

    # The same dictionary the listing numbered, so choice N is the account
    # printed as N. Re-reading .env here would be a second source of truth for
    # the one thing that must not be wrong in a delete.
    Show-Accounts -Accounts $accounts
    Write-Host ''
    $label = Read-AccountNumber -Accounts $accounts -Prompt 'Number to remove - blank to cancel'
    if (-not $label) { Write-Host 'Cancelled.'; return }
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
    Remove-TdlibDatabase -Label $label
    Write-Log "Removed account '$label' ($($accounts[$label]))"
    Write-Host ''
    Write-Host "Removed '$label'." -ForegroundColor Green
    if ($backup) { Write-Host "Previous .env kept as $(Split-Path -Leaf $backup)" }
    Write-Host 'A running server picks this up on its own - no restart needed.' -ForegroundColor Cyan
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
    Write-Host 'A running server picks this up on its own - no restart needed.' -ForegroundColor Cyan
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
