# Exercise the account manager's .env editing against a throwaway copy.
#
# The real .env is never touched: the script is copied into a temp directory with
# a fake .env beside it, and every assertion runs there. The menu itself is
# interactive, so this drives the functions directly by dot-sourcing the parts
# that do the work.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("tg-accounts-" + [guid]::NewGuid())
[void] (New-Item -ItemType Directory -Path $sandbox)

try {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'Manage-Accounts.ps1') -Destination $sandbox

    $envPath = Join-Path $sandbox '.env'
    $original = @(
        '# a comment that must survive'
        'TELEGRAM_API_ID=12345'
        'TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef'
        ''
        'TELEGRAM_SESSION_STRING=1AAAAAoriginal'
        'TELEGRAM_SESSION_STRING_WORK=1AAAAAwork'
        'SOME_OTHER_KEY=keep me'
    )
    [IO.File]::WriteAllLines($envPath, $original, [Text.UTF8Encoding]::new($false))

    # Load only the function definitions: run the file up to the point it starts
    # the menu, by extracting the function blocks.
    $source = Get-Content -LiteralPath (Join-Path $sandbox 'Manage-Accounts.ps1') -Raw
    $PSScriptRoot_shim = $sandbox
    $functions = [regex]::Matches($source, '(?ms)^function [\w-]+ \{.*?^\}')
    if ($functions.Count -lt 6) { throw "Expected the function blocks, found $($functions.Count)." }
    # Colour tokens and the menu table live outside any function, so pull those
    # assignments across as well or Get-Painted indexes a null hashtable.
    $tables = [regex]::Matches($source, '(?ms)^\$script:(Color|MenuItems) = (\[ordered\] )?@\{.*?^\}')
    if ($tables.Count -ne 2) { throw "Expected the Color and MenuItems tables, found $($tables.Count)." }
    # The numeric script-scope constants - retention counts, collision ceilings.
    # A function that reads one it was not given fails at runtime under
    # StrictMode, which is a stack trace rather than a readable assertion.
    $constants = [regex]::Matches($source, '(?m)^\$script:\w+ = \d+\s*$')
    if ($constants.Count -lt 3) {
        throw "Expected the numeric script-scope constants, found $($constants.Count)."
    }
    $escLine = [regex]::Match($source, '(?m)^\$script:Esc = .*$')
    if (-not $escLine.Success) { throw 'Could not find the Esc definition.' }

    $harness = @"
`$PSScriptRoot = '$sandbox'
`$envPath = Join-Path `$PSScriptRoot '.env'
`$script:LogPath = `$null
`$script:Quitting = `$false
$($escLine.Value)
$($constants.Value -join "`n")
$($tables.Value -join "`n`n")
$($functions.Value -join "`n`n")
"@
    . ([ScriptBlock]::Create($harness))

    # --- discovery ---------------------------------------------------------
    $accounts = Get-Accounts
    if ($accounts.Count -ne 2) { throw "Expected 2 accounts, found $($accounts.Count): $($accounts.Keys -join ', ')" }
    if (-not $accounts.Contains('default')) { throw 'The unsuffixed session was not labelled default.' }
    if (-not $accounts.Contains('work')) { throw 'The suffixed session was not found.' }
    Write-Host 'ok  discovers both the unsuffixed default and a suffixed label'

    # --- add ---------------------------------------------------------------
    $backup = Backup-EnvFile
    if (-not (Test-Path -LiteralPath $backup)) { throw 'Backup-EnvFile returned a path that does not exist.' }
    Set-EnvValue -Key 'TELEGRAM_SESSION_STRING_PERSONAL' -Value '1AAAAApersonal'
    $after = Get-Accounts
    if ($after.Count -ne 3) { throw "Add did not take: $($after.Keys -join ', ')" }
    Write-Host 'ok  adding a label writes a new key'

    # --- everything else survives -------------------------------------------
    $text = [IO.File]::ReadAllText($envPath)
    foreach ($expected in '# a comment that must survive', 'TELEGRAM_API_ID=12345', 'SOME_OTHER_KEY=keep me') {
        if ($text -notmatch [regex]::Escape($expected)) { throw "Rewriting .env lost: $expected" }
    }
    Write-Host 'ok  comments and unrelated keys survive a rewrite'

    # --- replace in place, not append ---------------------------------------
    Set-EnvValue -Key 'TELEGRAM_SESSION_STRING_WORK' -Value '1AAAAAwork-replaced'
    $workLines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -match '^TELEGRAM_SESSION_STRING_WORK=' })
    if ($workLines.Count -ne 1) { throw "Replacing a key appended a duplicate ($($workLines.Count) lines)." }
    if ($workLines[0] -notmatch 'replaced$') { throw 'The replacement value was not written.' }
    Write-Host 'ok  replacing a label edits its line instead of appending a second'

    # --- remove --------------------------------------------------------------
    Remove-EnvKey -Key 'TELEGRAM_SESSION_STRING_WORK'
    $afterRemove = Get-Accounts
    if ($afterRemove.Contains('work')) { throw 'Remove did not take.' }
    if (-not $afterRemove.Contains('default') -or -not $afterRemove.Contains('personal')) {
        throw "Remove took the wrong accounts with it: $($afterRemove.Keys -join ', ')"
    }
    Write-Host 'ok  removing one label leaves the others alone'

    # --- renaming an account -------------------------------------------------
    #
    # The prefix decides what the value MEANS. Rename used to write
    # TELEGRAM_SESSION_STRING_* whatever it read, so renaming a file-based account
    # handed the server a session PATH where it expects a session STRING: the
    # rename reported success and the account silently stopped loading.

    [IO.File]::WriteAllLines($envPath, @(
        '# keep me'
        'TELEGRAM_API_ID=12345'
        'TELEGRAM_SESSION_NAME_FILEBASED=C:\sessions\work.session'
        'TELEGRAM_SESSION_STRING_STRINGY=1AAAAAstring'
    ), [Text.UTF8Encoding]::new($false))

    Rename-EnvKey -From 'TELEGRAM_SESSION_NAME_FILEBASED' -To 'TELEGRAM_SESSION_NAME_RENAMED'
    $after = Get-Content -LiteralPath $envPath
    if ($after -notcontains 'TELEGRAM_SESSION_NAME_RENAMED=C:\sessions\work.session') {
        throw "A file-based rename lost its type or its value:`n$($after -join "`n")"
    }
    if ($after -join "`n" -match 'TELEGRAM_SESSION_STRING_RENAMED') {
        throw 'A file-based account was rewritten as a session string.'
    }
    if ($after -notcontains 'TELEGRAM_SESSION_STRING_STRINGY=1AAAAAstring') {
        throw 'Renaming one account disturbed another.'
    }
    if ($after -notcontains '# keep me') { throw 'The rename dropped a comment.' }
    Write-Host 'ok  a rename keeps the variable type that decides what the value means'

    # A rename is one transform and one write. It used to be a remove followed by a
    # separate add, so a failure between them left the account deleted and not
    # re-added - the operator asked to rename one, not to lose one.
    $before = Get-Content -LiteralPath $envPath -Raw
    $realWrite = ${function:Write-FileAtomic}
    function Write-FileAtomic { param($Path, $Text) throw [IO.IOException]::new('disk full') }
    $failed = $false
    try { Rename-EnvKey -From 'TELEGRAM_SESSION_STRING_STRINGY' -To 'TELEGRAM_SESSION_STRING_MOVED' }
    catch { $failed = $true }
    # Restore, not remove: there is one function table, so removing the shadow
    # would take the real one with it and every later check would fail on a
    # missing cmdlet rather than on its own subject.
    ${function:Write-FileAtomic} = $realWrite
    if (-not $failed) { throw 'A failing write was reported as a successful rename.' }
    if ((Get-Content -LiteralPath $envPath -Raw) -ne $before) {
        throw 'A failed rename changed the file; the account should still be exactly as it was.'
    }
    Write-Host 'ok  a rename that cannot be written leaves the account untouched'

    # Renaming something that is not there is an error, not a silent no-op that
    # reports success.
    $missing = $false
    try { Rename-EnvKey -From 'TELEGRAM_SESSION_STRING_NOPE' -To 'TELEGRAM_SESSION_STRING_X' }
    catch { $missing = $true }
    if (-not $missing) { throw 'Renaming an absent key reported success.' }
    Write-Host 'ok  renaming an account that does not exist is refused'

    # Backup retry is for a taken NAME. A permission or disk failure raises
    # IOException too, and retrying those a hundred times turns one clear error
    # into a hang and then a wrong message about collisions. Asserted on the
    # source because the failure needs File.Copy itself to fail, which cannot be
    # arranged here without a real permission change on the test machine.
    $backupSource = [regex]::Match(
        (Get-Content -LiteralPath (Join-Path $sandbox 'Manage-Accounts.ps1') -Raw),
        '(?ms)^function Backup-EnvFile \{.*?^\}').Value
    if ($backupSource -notmatch 'Test-Path[^
]*\$backup') {
        throw 'Backup-EnvFile retries without checking the name was actually taken.'
    }
    Write-Host 'ok  backup retry is reserved for a genuine name collision'

    # The checks above drive Rename-EnvKey, which moves a value to whatever key it
    # is told. Choosing that key is Rename-Account's job, and it is the half that
    # was wrong - so drive the real function with the prompts stubbed.
    [IO.File]::WriteAllLines($envPath, @(
        'TELEGRAM_API_ID=12345'
        'TELEGRAM_SESSION_NAME_ONDISK=C:\sessions\ondisk.session'
    ), [Text.UTF8Encoding]::new($false))

    $realRead = ${function:Read-Label}
    $realBackup = ${function:Backup-EnvFile}
    $script:answers = @('ondisk', 'moved')
    $script:answerIndex = 0
    function Read-Label { param($Prompt) $a = $script:answers[$script:answerIndex]; $script:answerIndex++; $a }
    function Backup-EnvFile { $null }
    try { Rename-Account }
    finally {
        ${function:Read-Label} = $realRead
        ${function:Backup-EnvFile} = $realBackup
    }

    $renamed = Get-Content -LiteralPath $envPath
    if ($renamed -notcontains 'TELEGRAM_SESSION_NAME_MOVED=C:\sessions\ondisk.session') {
        throw "Rename-Account did not keep the file-based type:`n$($renamed -join "`n")"
    }
    if (($renamed -join "`n") -match 'TELEGRAM_SESSION_STRING_MOVED') {
        throw 'Rename-Account rewrote a file session as a session string.'
    }
    Write-Host 'ok  Rename-Account itself picks the prefix from what it is renaming'


    # --- the backup is a real restore point ----------------------------------
    Copy-Item -LiteralPath $backup -Destination $envPath -Force
    $restored = Get-Accounts
    if ($restored.Count -ne 2 -or -not $restored.Contains('work')) {
        throw "Restoring the backup did not bring the file back: $($restored.Keys -join ', ')"
    }
    Write-Host 'ok  the backup restores the file by a plain copy'

    # --- the file that holds every login is written safely -------------------
    #
    # `[IO.File]::WriteAllText` truncates first and writes second: an interrupted
    # rewrite leaves a .env missing the accounts that had not been written yet.
    # A temp file installed by an atomic replace has no such window, and the
    # temp file must not survive either outcome.
    $strays = @(Get-ChildItem -LiteralPath $sandbox -Filter '*.tmp' -File -Force)
    if ($strays.Count -ne 0) {
        throw "Atomic .env writes left temp files behind: $($strays.Name -join ', ')"
    }
    Write-Host 'ok  an atomic .env write leaves no temporary file behind'

    # Each backup is a complete set of logins, so an unbounded pile of them turns
    # one readable directory into a leak of every session ever configured.
    foreach ($index in 1..($script:EnvBackupRetention + 4)) {
        $stamp = '2026-01-{0:d2}_00-00-00_UTC' -f $index
        [IO.File]::Copy($envPath, "$envPath.backup-$stamp", $false)
    }
    Remove-StaleFiles -Directory $sandbox -Filter '.env.backup-*' -Keep $script:EnvBackupRetention
    $kept = @(Get-ChildItem -LiteralPath $sandbox -Filter '.env.backup-*' -File -Force | Sort-Object Name)
    if ($kept.Count -ne $script:EnvBackupRetention) {
        throw "Retention kept $($kept.Count) backups, expected $($script:EnvBackupRetention)."
    }
    Write-Host 'ok  .env backups are pruned to the newest few'

    # Two operations in the same second each need their own restore point;
    # `Copy-Item -Force` silently replaced the first with the second.
    Get-ChildItem -LiteralPath $sandbox -Filter '.env.backup-*' -File -Force |
        Remove-Item -Force
    $first = Backup-EnvFile
    $second = Backup-EnvFile
    if ($first -eq $second) { throw 'Two backups in one second collapsed into one file.' }
    if (-not (Test-Path -LiteralPath $first)) { throw 'The first backup was overwritten.' }
    Write-Host 'ok  a second backup in the same second gets its own name'

    if ($env:OS -eq 'Windows_NT') {
        # os.chmod-style modes do not exist here: without an explicit ACL the
        # file holding every login inherits whatever the directory grants.
        #
        # The foreign entry is PLANTED rather than hoped for. The old
        # implementation removed the inherited entries and left every explicit
        # one, so it passed on a developer machine whose workspace carried none
        # and failed on a GitHub runner whose workspace carried three. Planting
        # one makes the check bite on both, and icacls stays the oracle because
        # it is not the API the implementation writes through.
        # Planted with icacls, not Set-Acl: writing a whole descriptor back
        # asks for SeSecurityPrivilege, which an ordinary account does not
        # hold. A bare `/grant` adds one explicit entry and nothing else,
        # which is the shape a runner workspace already has.
        & icacls $envPath '/grant' 'BUILTIN\Users:(R)' *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Could not plant a foreign access entry.' }
        if (-not (Set-OwnerOnlyAcl -Path $envPath)) {
            throw 'Set-OwnerOnlyAcl reported it could not make the .env owner-only.'
        }

        $acl = & icacls $envPath
        $entries = @($acl | Select-String -Pattern ':\(' -AllMatches |
                ForEach-Object { $_.Matches.Count } | Measure-Object -Sum).Sum
        if ($entries -ne 1) { throw "The .env carries $entries access entries, expected 1." }
        if (-not (Test-OwnerOnlyAcl -Path $envPath)) {
            throw 'The .env has one entry, but it does not name this account.'
        }
        Write-Host 'ok  a foreign access entry on .env is removed, not merely joined'
    }

    Get-ChildItem -LiteralPath $sandbox -Filter '.env.backup-*' -File -Force |
        Remove-Item -Force -ErrorAction SilentlyContinue

    # --- a commented-out account is not an account ---------------------------
    [IO.File]::WriteAllLines($envPath, @('#TELEGRAM_SESSION_STRING_OLD=1AAAAAold', 'TELEGRAM_SESSION_STRING=1AAAAAx'),
        [Text.UTF8Encoding]::new($false))
    $commented = Get-Accounts
    if ($commented.Contains('old')) { throw 'A commented-out line was read as a configured account.' }
    Write-Host 'ok  a commented-out session line is ignored'

    # --- labels a person actually types --------------------------------------
    #
    # python-dotenv REFUSES a key containing a space: it warns and drops the line,
    # so "KGB Verifier" stored literally would save an account that never loads.
    # Spaces and hyphens must therefore reach .env as underscores.
    $cases = @(
        @{ In = 'KGB Verifier';      Out = 'kgb_verifier' }
        @{ In = '  Work  Account  '; Out = 'work_account' }
        @{ In = 'my-second-phone';   Out = 'my_second_phone' }
        @{ In = 'Personal';          Out = 'personal' }
        @{ In = 'a  b';              Out = 'a_b' }
        @{ In = '_padded_';          Out = 'padded' }
    )
    foreach ($case in $cases) {
        $got = ConvertTo-Label -Raw $case.In
        if ($got -ne $case.Out) { throw "ConvertTo-Label '$($case.In)' gave '$got', expected '$($case.Out)'." }
    }
    Write-Host 'ok  spaces and hyphens in a typed label become underscores'

    if ($null -ne (ConvertTo-Label -Raw '   ')) { throw 'Blank input should cancel, not produce a label.' }
    foreach ($bad in 'has.dot', 'has/slash', 'کانال', 'has=equals') {
        if ((ConvertTo-Label -Raw $bad) -ne '') { throw "'$bad' should be refused - there is no safe mapping for it." }
    }
    Write-Host 'ok  blank cancels, and a character with no safe mapping is refused'

    # The stored key must be one python-dotenv can parse back.
    $label = ConvertTo-Label -Raw 'KGB Verifier'
    Set-EnvValue -Key "TELEGRAM_SESSION_STRING_$($label.ToUpperInvariant())" -Value '1AAAAAkgb'
    $line = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -match '^TELEGRAM_SESSION_STRING_KGB' })
    if ($line.Count -ne 1) { throw "Expected one KGB line, found $($line.Count)." }
    if ($line[0] -match '\s.*=') { throw "The stored key contains whitespace: $($line[0])" }
    if (-not (Get-Accounts).Contains($label)) { throw 'The spaced label did not round-trip through .env.' }
    Write-Host 'ok  the stored key has no whitespace and reads back as the same label'

    # --- the theme, ported from FFmWiz ---------------------------------------
    #
    # `back_text` there renders {back=0, quit=exit} with the back half in 256-colour
    # 166 and the exit half in 32. Those two being distinguishable at a glance is the
    # whole reason the palette is 256-colour rather than the console's sixteen names.
    $script:UseColor = $true
    $back = Get-BackText
    if ($back -notmatch '^\{.*\}$') { throw "back text is not brace-wrapped: $back" }
    if ($back -notmatch 'back=0') { throw "back text lost the back key: $back" }
    if ($back -notmatch 'quit=exit') { throw "back text lost the quit key: $back" }
    $esc = [char] 27
    if ($back -notmatch [regex]::Escape("$esc[38;5;166m")) { throw 'back=0 is not painted with FFmWiz BACK_PROMPT (166).' }
    if ($back -notmatch [regex]::Escape("$esc[38;5;32m")) { throw 'quit=exit is not painted with FFmWiz EXIT_PROMPT (32).' }
    Write-Host 'ok  back=0 and quit=exit carry the FFmWiz prompt colours'

    $quitOnly = Get-BackText -Text 'quit=exit'
    if ($quitOnly -match 'back') { throw "A quit-only hint still advertises back: $quitOnly" }
    Write-Host 'ok  a prompt with nowhere to go back to advertises only quit'

    # NO_COLOR is honoured the same way FFmWiz honours it, and a terminal without
    # virtual-terminal processing would otherwise show the escapes as literal noise.
    $script:UseColor = $false
    $plain = Get-BackText
    if ($plain -ne '{back=0, quit=exit}') { throw "Uncoloured hint is not plain text: $plain" }
    if ($plain -match [regex]::Escape($esc)) { throw 'An escape survived with colour disabled.' }
    $script:UseColor = $true
    Write-Host 'ok  colour off leaves plain text with no escapes'

    # --- how the session generator is launched -------------------------------
    #
    # Static assertions, deliberately: the real path opens a Telegram login, so the
    # only honest automated check is on how the command is built.
    $generator = [regex]::Match($source, '(?ms)^function Invoke-SessionGenerator \{.*?^\}')
    if (-not $generator.Success) { throw 'Could not find Invoke-SessionGenerator.' }
    $body = $generator.Value
    # Comments are stripped first. A comment explaining why `--qr` is absent
    # contains the string `--qr`, and matching that would be asserting against my
    # own prose rather than against the command that actually runs.
    $code = ($body -split "`n" | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"

    # Forcing --qr made phone login unreachable from the menu. With no flag the
    # generator asks, so the offer can never drift from what it really supports.
    if ($code -match '--qr|--phone') {
        throw 'The generator is launched with a login-method flag, which hides the other method.'
    }
    Write-Host 'ok  no login-method flag is forced, so the generator offers both'

    # `uv run` rebuilds and reinstalls the project when a source file changed, and
    # prints build/wheel progress on top of the login prompt. The venv is already
    # there; uv is only for when it is not.
    $venvAt = $code.IndexOf('.venv\Scripts\python.exe')
    $uvAt = $code.IndexOf('Get-Command uv')
    if ($venvAt -lt 0 -or $uvAt -lt 0) { throw 'The generator launch lost one of its two interpreters.' }
    if ($venvAt -gt $uvAt) { throw 'uv is tried before the venv, which reintroduces the rebuild noise.' }
    Write-Host 'ok  the existing venv is preferred over a uv rebuild'

    if ($code -notmatch 'UV_LINK_MODE') {
        throw 'The uv fallback does not apply uv''s own fix for the hardlink warning.'
    }
    if ($code -notmatch '\$env:UV_LINK_MODE = \$previousLinkMode') {
        throw 'UV_LINK_MODE is set for the process and never restored.'
    }
    Write-Host 'ok  the uv fallback silences the hardlink warning and restores the variable'

    # The main menu has no previous step. FFmWiz states this in a comment and does
    # not advertise back there; the same has to hold here or the hint lies.
    if ($source -notmatch "Read-Answer -Prompt 'Selection' -NoBack") {
        throw 'The main menu advertises back=0, but there is nothing to go back to.'
    }
    Write-Host 'ok  the main menu does not advertise a back it cannot perform'

}
finally {
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

# The checks above run extracted functions. This one runs the SHIPPED script with
# scripted input, because navigation is the thing a harness cannot vouch for: the
# question is whether `0` unwinds one level and `exit` unwinds all of them.
$e2e = Join-Path ([IO.Path]::GetTempPath()) ("tg-accounts-e2e-" + [guid]::NewGuid())
[void] (New-Item -ItemType Directory -Path $e2e)
try {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'Manage-Accounts.ps1') -Destination $e2e
    $e2eEnv = Join-Path $e2e '.env'
    $before = @('TELEGRAM_API_ID=1', 'TELEGRAM_SESSION_STRING_WORK=1AAAAAwork')
    [IO.File]::WriteAllLines($e2eEnv, $before, [Text.UTF8Encoding]::new($false))

    # 3 = remove, 0 = step back out of it, exit = leave.
    $output = "3`n0`nexit`n" | & pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $e2e 'Manage-Accounts.ps1') 2>&1
    $code = $LASTEXITCODE
    $text = $output -join [Environment]::NewLine

    if ($code -ne 0) { throw "The script exited $code from a plain exit: $text" }
    if ($text -notmatch 'Cancelled') { throw "0 did not cancel the remove step: $text" }
    if ($text -notmatch 'Telegram MCP account manager') { throw 'The menu was never redrawn after 0.' }

    $after = @([IO.File]::ReadAllLines($e2eEnv))
    if ("$after" -ne "$before") { throw "Backing out of remove still changed .env: $after" }
    if (@(Get-ChildItem -LiteralPath $e2e -Filter '.env.backup-*' -Force).Count -ne 0) {
        throw 'A backup was written for a cancelled operation.'
    }
    Write-Host 'ok  0 unwinds one level and exit leaves, changing nothing on the way'

    # --- finishing the account against TDLib -------------------------------------
    #
    # Secret chats and the newer admin rights run on TDLib, which keeps its own
    # authorisation. That used to mean a second login code. It does not: Telegram's
    # device-linking flow lets the login that just happened authorise this one, so
    # the step asks for nothing. What it must NOT do is grow a phone-and-code
    # prompt of its own - that would be the second code coming back.

    $source = [IO.File]::ReadAllText((Join-Path $projectRoot 'Manage-Accounts.ps1'))

    if ($source -notmatch 'function Invoke-SecretChatLogin') {
        throw 'Adding an account no longer finishes it against TDLib.'
    }
    if ($source -notmatch 'Invoke-SecretChatLogin -Label \$label') {
        throw 'Invoke-SecretChatLogin exists but Add-Account never calls it.'
    }
    Write-Host 'ok  adding an account finishes it against TDLib too'

    $step = [regex]::Match($source, '(?ms)^function Invoke-SecretChatLogin \{.*?^\}').Value
    if (-not $step) { throw 'Could not isolate Invoke-SecretChatLogin.' }
    # The doc comment explains the mechanism and mentions "the phone app", so it
    # has to come out before looking for a prompt - otherwise the prose that says
    # no code is asked for is itself read as asking for one.
    $code = [regex]::Replace($step, '(?ms)<#.*?#>', '')
    foreach ($asked in @('Read-Host', 'phone', 'Phone', 'code:')) {
        if ($code -match [regex]::Escape($asked)) {
            throw "The TDLib step asks for '$asked' - the second code is back."
        }
    }
    if ($step -notmatch 'secret_chat_login\.py') {
        throw 'The TDLib step does not run the login script.'
    }
    Write-Host 'ok  it asks for nothing: the existing login authorises TDLib'

    # `Read-Confirmation` is already in scope: the harness above dot-sources every
    # function block, which is how this file drives an interactive script at all.
    $onEnter = & { function Read-Host { param($Prompt) '' }; Read-Confirmation 'finish it now?' }
    if (-not $onEnter) { throw 'Enter no longer means yes for the ordinary confirmation.' }
    $onNo = & { function Read-Host { param($Prompt) 'n' }; Read-Confirmation 'finish it now?' }
    if ($onNo) { throw 'A typed no was ignored.' }
    Write-Host 'ok  Enter accepts and a typed n declines'

    # Every route that puts an account into .env must finish the TDLib half.
    #
    # The property is unchanged; the mechanism moved. It used to be a launcher
    # step that spawned the login script and asked for the two-step password a
    # SECOND time - seconds after the generator had already collected it and
    # Telegram had accepted it. That reads as the tool not paying attention, and
    # every extra attempt counts against the account's own limits. So the
    # generator, which is holding the password and an authorised client, now
    # finishes both halves itself, and the separate menu entry is gone.
    $generator = [IO.File]::ReadAllText((Join-Path $projectRoot 'session_string_generator.py'))

    if ($generator -notmatch '_finish_secret_chats\(client, safe_label, password\)') {
        throw 'The generator no longer finishes the TDLib half - that is the original gap, reopened.'
    }
    # The load-bearing part: the password is REUSED, not asked for again.
    if ($generator -notmatch 'complete_login\(label, client, password=password\)') {
        throw 'The generator no longer passes the password through, so it would ask a second time.'
    }
    # `\s*$` rather than `$`: the file is CRLF, and `$` closes before the \n with
    # the \r still to match. That gotcha has cost this suite a false failure before.
    if ($generator -notmatch '(?m)^\s+return pw\s*$') {
        throw 'The sign-in no longer hands the accepted password back, so nothing can reuse it.'
    }
    # A pasted session string is the one case with no password to reuse, so that
    # path keeps its own prompt.
    if ($source -notmatch 'Invoke-SecretChatLogin -Label \$label') {
        throw 'The pasted-session path no longer offers the TDLib half at all.'
    }
    if ($source -match "'6' = ") {
        throw 'Menu entry 6 is back; the generator is supposed to make it unnecessary.'
    }

    $dispatch = [regex]::Match($source, '(?ms)switch \(\$choice\) \{.*?^        \}').Value
    if (-not $dispatch) { throw 'Could not isolate the menu dispatch.' }
    foreach ($entry in @('2', '5')) {
        $branch = [regex]::Match($dispatch, "(?ms)'$entry' \{.*?
            \}|'$entry' \{[^
]*\}").Value
        if (-not $branch) { throw "Menu entry $entry has no dispatch branch." }
    }
    Write-Host 'ok  the generator finishes both halves and reuses the password it already took'

    # A status probe attached to a listing must never take the listing down.
    $probe = [regex]::Match($source, '(?ms)^function Get-SecretChatStates \{.*?^\}').Value
    if ($probe -notmatch 'IsNullOrWhiteSpace\(\$PSScriptRoot\)') {
        throw 'Get-SecretChatStates no longer tolerates a missing script root.'
    }
    if ($probe -notmatch 'catch') {
        throw 'Get-SecretChatStates can throw, which would break Show-Accounts.'
    }
    Write-Host 'ok  the status probe degrades to unknown instead of breaking the list'


}
finally {
    Remove-Item -LiteralPath $e2e -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Account manager checks passed.' -ForegroundColor Green

