<#
    What the menu looks like, and how it asks a question.

    Split out of `Manage-Accounts.ps1`: colour tokens, the painter, and the
    prompts. Ported from FFmWiz so the launchers across these projects read as
    one family.

    Dot-sourced, so `$script:Esc`, `$script:Color`, `$script:UseColor` and
    `$script:Quitting` are established in the launcher's scope when this file
    runs - the harness in `tests/test_account_manager.ps1` relies on that, and on
    these definitions being findable in the source text.
#>

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
