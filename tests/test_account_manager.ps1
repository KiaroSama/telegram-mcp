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
    $escLine = [regex]::Match($source, '(?m)^\$script:Esc = .*$')
    if (-not $escLine.Success) { throw 'Could not find the Esc definition.' }

    $harness = @"
`$PSScriptRoot = '$sandbox'
`$envPath = Join-Path `$PSScriptRoot '.env'
`$script:LogPath = `$null
`$script:Quitting = `$false
$($escLine.Value)
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

    # --- the backup is a real restore point ----------------------------------
    Copy-Item -LiteralPath $backup -Destination $envPath -Force
    $restored = Get-Accounts
    if ($restored.Count -ne 2 -or -not $restored.Contains('work')) {
        throw "Restoring the backup did not bring the file back: $($restored.Keys -join ', ')"
    }
    Write-Host 'ok  the backup restores the file by a plain copy'

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
}
finally {
    Remove-Item -LiteralPath $e2e -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Account manager checks passed.' -ForegroundColor Green

