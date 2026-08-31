[CmdletBinding()]
param(
    # Persist the server's DIAGNOSTIC output (stderr) to a file under the state
    # directory. ON by default; never stdout, see the note below. Kept as an
    # explicit switch so an existing command line that passes it still works.
    [switch] $LogToFile,

    # The opt-OUT. A run that must leave nothing on disk asks for that here, or
    # sets TELEGRAM_MCP_LAUNCHER_LOG to 0/false/no/off.
    [switch] $NoLogToFile,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArguments = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$exitCode = 1
$logPath = $null

# STDOUT IS THE MCP PROTOCOL CHANNEL.
#
# Under the stdio transport the server speaks JSON-RPC over stdout, and those
# frames carry complete tool results: message text, contact names, chat titles,
# files listed, whatever the client asked for. Teeing stdout wrote all of it to
# disk. Only stderr -- which the server uses for diagnostics, and which
# `telegram_mcp.safe_log` already bounds -- is ever persisted here.
#
# That is the whole privacy argument, and it is satisfied by the stdout/stderr
# split alone. Logging was nevertheless OPT-IN, so the default run wrote nothing
# and announced `log=unavailable` - a word that reads as "logging broke" when it
# actually meant "you did not ask". The one run an operator most needs a log for
# is the one that went wrong unexpectedly, which is exactly the run nobody
# thought to pass a flag to. So it is on by default now, and the way to get
# nothing on disk is to say so.
$script:LogDisabledReason = $null
if ($NoLogToFile.IsPresent) {
    $script:LogDisabledReason = 'disabled by -NoLogToFile'
}
elseif ($env:TELEGRAM_MCP_LAUNCHER_LOG -in @('0', 'false', 'no', 'off')) {
    $script:LogDisabledReason = 'disabled by TELEGRAM_MCP_LAUNCHER_LOG'
}
$script:LogEnabled = -not $script:LogDisabledReason

# A server run can log for days, so the file needs a ceiling as well as a count:
# the tee below rolls at LogMaxBytes keeping one previous part, and Remove-StaleFiles
# keeps the newest LogRetention runs.
$script:LogRetention = 10
$script:LogMaxBytes = 5MB

function Get-StateDirectory {
    <#
      Where runtime state goes: NOT beside the source. `logs/` next to the script
      lands inside the git checkout, gets committed or synced, and inherits
      whatever the repository directory grants. Same rule as
      `telegram_mcp.aliases.aliases_file_path`, so there is one place to lock down.
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
function Remove-StaleFiles {
    param(
        [Parameter(Mandatory)] [string] $Directory,
        [Parameter(Mandatory)] [string] $Filter,
        [Parameter(Mandatory)] [int] $Keep
    )
    # Named by a UTC timestamp, so the name order IS the age order.
    $files = @(
        Get-ChildItem -LiteralPath $Directory -Filter $Filter -File -Force -ErrorAction SilentlyContinue |
            Sort-Object Name
    )
    for ($index = 0; $index -lt $files.Count - $Keep; $index++) {
        Remove-Item -LiteralPath $files[$index].FullName -Force -ErrorAction SilentlyContinue
    }
}

try {
    # Not `throw 'not requested'`: routing the opt-out through the same catch as a
    # real failure is what made both of them print the same word.
    $script:LogFailure = $null
    try {
        if (-not $script:LogEnabled) { throw [OperationCanceledException]::new('opted out') }
        $logsDirectory = Join-Path (Get-StateDirectory) 'logs'
        [void] (New-Item -ItemType Directory -Path $logsDirectory -Force)
        # The directory first, and fatally: its inheritable entries are what make
        # every log file born inside it owner-only, including the rolled parts the
        # Python tee creates later, which no startup sweep can reach.
        #
        # A throw here is caught below, which sets $logPath to $null - and that is
        # what turns file logging off. Warning and carrying on would keep writing
        # diagnostics into a directory whose permissions nobody established.
        if (-not (Set-OwnerOnlyAcl -Path $logsDirectory)) {
            throw 'the log directory could not be made owner-only'
        }

        $timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
        $logPath = Join-Path $logsDirectory "start-mcp_$timestamp.log"
        # Bounded, and random once the obvious names are gone. The loop had no
        # ceiling: anything that keeps making the path exist - a directory left
        # under that name, a filter driver, a permission quirk - spun it forever,
        # and a launcher that never starts is worse than one that logs elsewhere.
        for ($suffix = 1; $suffix -le 20 -and (Test-Path -LiteralPath $logPath); $suffix++) {
            $logPath = Join-Path $logsDirectory "start-mcp_${timestamp}_$suffix.log"
        }
        if (Test-Path -LiteralPath $logPath) {
            $unique = [guid]::NewGuid().ToString('N').Substring(0, 8)
            $logPath = Join-Path $logsDirectory "start-mcp_${timestamp}_$unique.log"
        }

        [IO.File]::WriteAllText($logPath, '', [Text.UTF8Encoding]::new($true))
        if (-not (Set-OwnerOnlyAcl -Path $logPath)) {
            Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
            throw 'the log file could not be made owner-only'
        }
        # `.log` AND `.log.1`: the Python tee rolls a full log to `<name>.log.1`,
        # and a filter ending in `.log` never matched those, so every rotated part
        # survived every prune and the retention count bounded half the files.
        Remove-StaleFiles -Directory $logsDirectory -Filter 'start-mcp_*.log' -Keep $script:LogRetention
        Remove-StaleFiles -Directory $logsDirectory -Filter 'start-mcp_*.log.1' -Keep $script:LogRetention
    }
    catch [OperationCanceledException] {
        $logPath = $null
    }
    catch {
        $logPath = $null
        # Logging is on by default, so reaching here means it was wanted and
        # broke. Say so unconditionally: a launcher that silently loses its log
        # is the failure being reported here in the first place.
        $script:LogFailure = $_.Exception.Message
        Write-Warning "File logging failed, continuing without it: $($script:LogFailure)"
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv was not found in PATH. Install uv, then run this launcher again."
    }

    # Three distinct states, three distinct words. "unavailable" covered all of
    # them and told the reader nothing about which one they were in.
    $logDisplay = if ($logPath) { $logPath }
    elseif ($script:LogFailure) { "FAILED ($script:LogFailure)" }
    elseif ($script:LogDisabledReason) { $script:LogDisabledReason }
    else { 'none' }
    $startMessage = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [INFO] [launcher] Starting: uv run main.py; log=$logDisplay"
    # stderr, not Write-Host: the host stream is stdout once redirected, and the
    # client reads this process's stdout as the MCP protocol.
    [Console]::Error.WriteLine($startMessage)
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$startMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        if ($logPath) {
            $pythonWrapper = @'
import atexit, os, re, runpy, sys, threading, traceback

# STDERR ONLY. On the stdio transport stdout carries the MCP protocol -- complete
# tool results, i.e. the user's own messages, contacts and files -- so it is
# passed straight through and never written to disk. stderr is diagnostics, which
# telegram_mcp.safe_log already bounds.
ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
lock = threading.RLock()
log_path, max_bytes, script, *args = sys.argv[1:]
max_bytes = int(max_bytes)

def secure(path):
    # Owner-only, on every file this wrapper creates -- the first one and every
    # one a rollover makes. os.chmod is not that guarantee on Windows: it toggles
    # the read-only attribute and cannot clear the read bit.
    #
    # icacls used to stand in for it here and could not: `/inheritance:r`
    # removes the INHERITED entries and `/grant:r` replaces ONE principal's,
    # so every other explicit entry survived while the call exited 0.
    # telegram_mcp.owner_only writes the whole DACL and reads it back, and it
    # imports without pulling in telethon or mcp -- which matters, because
    # reporting an import failure of those is the job this wrapper exists for.
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False
    try:
        from telegram_mcp.owner_only import restrict_to_owner_strict
    except Exception:
        # The launcher made the logs directory owner-only with INHERITABLE
        # entries, and fatally, before this process started - so a file born in
        # it is born private and there is nothing left to prove here. On POSIX
        # this branch is unreachable: the chmod above needs no package.
        return True
    try:
        return bool(restrict_to_owner_strict(path))
    except OSError:
        return False

# newline="" for the same reason roll() has it, twenty lines down: without it
# Windows turns each written line ending into two bytes on disk while the cap
# counts one, so the FIRST segment overran the ceiling by one byte per line.
# Only the rolled segments were ever correct.
log = open(log_path, "a", encoding="utf-8", buffering=1, newline="")
# Fail closed. This file is what an operator attaches to a bug report, and it
# carries whatever the server wrote to stderr; ignoring a failed hardening kept
# writing that into a file whose permissions nobody established.
persist = secure(log_path)
if not persist:
    log.close()
    log = None

def roll():
    # One previous part, not an archive: a server that logs for days must not be
    # able to fill the disk, and the interesting end of a wedged run is the tail.
    global log, persist
    log.close()
    try:
        os.replace(log_path, log_path + ".1")
    except OSError:
        # The roll did NOT happen. Re-opening in append mode would carry on
        # growing the same file past a ceiling this code believes it enforces,
        # so persistence stops rather than the bound quietly ceasing to hold.
        log = None
        persist = False
        return
    secure(log_path + ".1")
    # newline='' disables Windows newline translation. Without it a written line
    # ending becomes two bytes on disk while the cap counts one, so a ceiling
    # computed in bytes is wrong by the number of lines written - and the file
    # ends up over a limit the code believes it enforced.
    log = open(log_path, "a", encoding="utf-8", buffering=1, newline="")
    if not secure(log_path):
        log.close()
        log = None
        persist = False

# Only the server's OWN diagnostics are written down. `telegram_mcp` logs through
# safe_log, whose RedactingFilter replaces every value with a shape and a digest
# before it reaches stderr, so those lines are safe by construction and carry this
# exact prefix. Everything else on the child's stderr was composed by something
# that made no such promise - a Telethon warning naming a chat, a deprecation
# notice quoting an argument, a third-party traceback with locals in it. Removing
# ANSI escapes does not make that safe; it only makes it tidy. Those lines go to
# the terminal and are COUNTED, not persisted.
# Two shapes, both composed by this project and both safe by construction:
#
#   1. `telegram_mcp` logger records, redacted by safe_log's RedactingFilter
#      before they reach stderr.
#   2. `[telegram-mcp] ` startup notes from runner.py's `startup_note`, whose
#      comment states the same promise and routes every exception through
#      safe_exception first.
#
# The second was missing, and its absence is what made this launcher look
# broken: the logger sits at ERROR, so the entire startup narrative - including
# the sentence explaining WHY a run refused to start - travels as those notes,
# and every one of them was being counted and thrown away. An operator opened
# the log after a failed launch and found nothing about the failure.
SERVER_LINE = re.compile(
    r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[[A-Z]+\] telegram_mcp - |\[telegram-mcp\] )'
)
partial = ''
withheld = 0

def emit(line):
    # The one place anything reaches the log, so the size ceiling is enforced in
    # one place too.
    global log, persist
    if not persist:
        return
    encoded = line.encode('utf-8')
    if max_bytes:
        if log.tell() + len(encoded) > max_bytes:
            roll()
            if not persist:
                return
        if len(encoded) > max_bytes:
            marker = '... [truncated at the log size ceiling]' + chr(10)
            keep = max(0, max_bytes - len(marker.encode('utf-8')))
            line = encoded[:keep].decode('utf-8', 'ignore') + marker
    log.write(line)

def persist_chunk(text):
    # Line by line, because the allowlist is a per-line decision and the pump
    # delivers arbitrary chunks.
    global partial, withheld
    partial += text
    while chr(10) in partial:
        line, partial = partial.split(chr(10), 1)
        if SERVER_LINE.match(line):
            emit(line + chr(10))
        else:
            withheld += 1

class Tee:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        with lock:
            # The terminal gets everything, always and first. What is written
            # down is decided by `persist_chunk`, one line at a time.
            written = self.stream.write(text)
            self.stream.flush()
            if not persist:
                return written
            persist_chunk(ansi.sub('', text))
        return written
    def flush(self):
        with lock:
            self.stream.flush()
            if persist:
                log.flush()

    def isatty(self):
        return self.stream.isatty()

    def __getattr__(self, name):
        return getattr(self.stream, name)

def _report_withheld():
    # atexit, registered BEFORE the server is loaded, so it runs LAST: handlers
    # fire in reverse registration order, and anything the child writes from its
    # own atexit hook has to be counted before this line is written.
    global withheld
    with lock:
        if partial and not SERVER_LINE.match(partial):
            withheld += 1
        if withheld and persist:
            emit(
                f'[launcher] {withheld} line(s) of child output were shown on the '
                'terminal and not written here: they did not come from this '
                'server' + chr(39) + 's redacting logger.' + chr(10)
            )

atexit.register(_report_withheld)

stderr = sys.stderr
sys.stderr = Tee(stderr)
sys.argv = [script, *args]
exit_code = 0
try:
    runpy.run_path(script, run_name="__main__")
except KeyboardInterrupt:
    pass
except SystemExit as exc:
    exit_code = exc.code if isinstance(exc.code, int) else 1
    if exc.code is not None and not isinstance(exc.code, int):
        print(exc.code, file=sys.stderr)
except BaseException as exc:
    # Two destinations, deliberately different. A traceback carries the
    # arguments and repr of every frame, which here means chat titles,
    # usernames, phone numbers and paths - and the log file is the thing an
    # operator attaches to a bug report. So the full traceback goes straight to
    # the terminal, bypassing the tee, and only its shape is persisted.
    traceback.print_exception(exc, file=stderr)
    stderr.flush()
    frames = traceback.extract_tb(exc.__traceback__)
    # Basename, not the full filename: the path leaks the install location and
    # with it the OS account name, into the file operators attach to reports.
    where = (
        f'{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}'
        if frames
        else 'unknown'
    )
    # Straight to both, bypassing the tee: this record is composed HERE from a
    # type name and a basename, so it is the one thing on this stream already
    # known to be safe - and routing it through the allowlist withheld it, which
    # left the log with nothing at all about the failure.
    shape = f'{type(exc).__name__} at {where}'
    print(shape, file=stderr)
    stderr.flush()
    with lock:
        emit(shape + chr(10))
    exit_code = 1
finally:
    sys.stdout.flush()
    sys.stderr.flush()

if exit_code:
    raise SystemExit(exit_code)
'@
            & $uv.Path run python -c $pythonWrapper $logPath $script:LogMaxBytes `
                (Join-Path $PSScriptRoot 'main.py') @ServerArguments
        }
        else {
            & $uv.Path run main.py @ServerArguments
        }
        $nativeExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
        $exitCode = if ($null -eq $nativeExitCode) { 0 } else { [int] $nativeExitCode }
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        Write-Error "uv exited with code $exitCode." -ErrorAction Continue
    }
}
catch {
    $stamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC')
    # The MESSAGE goes to the terminal and nowhere else. Whatever threw composed
    # it, so it can carry a path, a chat title, an invite link or a credential out
    # of a connection string - and the log file is the thing an operator attaches
    # to a bug report. What is persisted is the SHAPE: the exception type and the
    # script line that raised it, by basename.
    Write-Error "[$stamp] [ERROR] [launcher] $($_.Exception.Message)" -ErrorAction Continue
    if ($logPath) {
        $type = $_.Exception.GetType().Name
        $where = if ($_.InvocationInfo -and $_.InvocationInfo.ScriptName) {
            "$(Split-Path -Leaf $_.InvocationInfo.ScriptName):$($_.InvocationInfo.ScriptLineNumber)"
        }
        else { 'unknown' }
        $record = "[$stamp] [ERROR] [launcher] $type at $where"
        [IO.File]::AppendAllText($logPath, "$record$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }
}
finally {
    $stopMessage = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [INFO] [launcher] Stopped with exit code $exitCode."
    [Console]::Error.WriteLine($stopMessage)
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$stopMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }
}

exit $exitCode
