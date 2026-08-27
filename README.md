<div align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=gradient&height=200&section=header&text=Telegram%20MCP%20Server&fontSize=50&fontAlignY=35&animation=fadeIn&fontColor=FFFFFF&descAlignY=55&descAlign=62" alt="Telegram MCP Server" width="100%" />
</div>

![MCP Badge](https://badge.mcpx.dev)
[![Licence: GPL-3.0-or-later](https://img.shields.io/badge/licence-GPL--3.0--or--later-blue?style=flat-square)](LICENSE)
[![Tests](https://github.com/KiaroSama/telegram-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/KiaroSama/telegram-mcp/actions/workflows/tests.yml)
[![Python Lint & Format Check](https://github.com/KiaroSama/telegram-mcp/actions/workflows/python-lint-format.yml/badge.svg)](https://github.com/KiaroSama/telegram-mcp/actions/workflows/python-lint-format.yml)

A Telegram integration for Claude, Cursor, and other MCP-compatible clients. It exposes Telegram account, chat, message, contact, media, folder, and admin operations through the [Model Context Protocol](https://modelcontextprotocol.io/) using [Telethon](https://docs.telethon.dev/).

**Free software under the GNU GPL v3 or later.** See [LICENSE](LICENSE). You may use, study,
modify and redistribute it; anything you distribute that builds on it carries the same licence.

## 🤖 MCP in Action

Basic Telegram MCP usage in Claude:

![Telegram MCP in action](screenshots/1.png)

Asking Claude to analyze chat history and send a response:

![Telegram MCP Request](screenshots/2.png)

Message sent successfully:

![Telegram MCP Result](screenshots/3.png)

## Contents

- [What It Can Do](#what-it-can-do)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [MCP Client Configuration](#mcp-client-configuration)
- [Windows launchers](#windows-launchers)
- [Multi-Account Setup](#multi-account-setup)
- [Device Identity](#device-identity)
- [Proxy Support](#proxy-support)
- [File Path Security](#file-path-security)
- [Docker](#docker)
- [Development](#development)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Visual and structured access](#visual-and-structured-access)
- [Content types beyond plain messages](#content-types-beyond-plain-messages)
- [Licence](#licence)

## What It Can Do

The server registers **165 MCP tools**. That count is measured, not estimated — see
[docs/api-coverage.md](docs/api-coverage.md), which also records what Telegram has that this
server deliberately does not. The tools group into these areas:

- **Accounts:** list configured accounts and route tool calls by account label.
- **Chats and groups:** list chats, inspect metadata, create groups/channels, join or leave chats, invite users, manage admins, bans, default permissions, slow mode, topics, invite links, common chats, read receipts, and message links.
- **Messages:** send, schedule, edit, delete, forward, pin, unpin, mark read, reply, search, inspect context, create polls, manage reactions, inspect inline buttons, and press inline callbacks. `send_message`, `reply_to_message`, and `edit_message` support classic formatting (`parse_mode='md'`/`'html'`) and server-side rich formatting (`parse_mode='rich'`/`'rich_markdown'`/`'rich_html'` — full Markdown/HTML with tables, headings, formulas, and collapsible sections). Rich modes require Telegram Premium on the account; Premium is re-checked on every call, and without it nothing is sent — the tool returns a structured `telegram_premium_required` result so the agent can reformat with classic modes and retry.
- **Contacts:** list, search, add, delete, block, unblock, import, export, inspect direct chats, find recent contact interactions, and remember contacts by the names you actually use (see below).

### Remembered contacts

`set_contact_alias` teaches the server what you call someone, and every tool that takes a `chat_id` understands it from then on — `send_message("андрей бекендер", ...)` just works. A contact can carry any number of aliases, which is how tags work: save both `андрей бекендер` and `бекендер` for the same person and either resolves.

**Only an exact saved wording ever sends.** Similar wording (`Андрею бекендеру` for a saved `андрей бекендер`) is matched too, but only to *suggest*: the tool sends nothing and asks you to confirm the contact by name. This is deliberate — `Лена`/`Леня` and `Иван`/`Иванов` differ exactly as much as a case ending does, so a matcher confident enough to handle declensions is also confident enough to message the wrong person whenever the one you meant is not saved yet. Confirming saves that wording as its own alias, so each new phrasing costs one yes/no the first time and nothing ever again. Set `TELEGRAM_CONTACT_FUZZY=0` to drop the suggestions too.

When a reference is unknown, resembles one contact, matches several, or points at a contact that no longer resolves, tools send nothing and return a structured instruction telling the agent exactly what to ask you, to save the answer with `set_contact_alias`, and to retry once. `list_contact_aliases` shows one row per person with all their aliases (use it to spot a wrong memory), `delete_contact_alias` forgets one, and repointing an alias at someone else requires `replace=True`. The save path itself refuses a target it would have to guess at: contacts are saved by @username, phone, numeric ID, or an alias already confirmed for them.

Aliases live in `${XDG_STATE_HOME:-~/.local/state}/telegram-mcp/aliases.json` (owner-only, written atomically); `TELEGRAM_ALIASES_FILE` overrides the path, and a pre-existing `aliases.json` next to the code is still read as a fallback.

**Aliases belong to the account that saved them.** A chat ID only identifies a person within one login, so the same alias on a different account would name someone else entirely, or nobody. Two accounts can therefore each save their own `мама`, and neither can send to the other's. Aliases saved before this scoping existed are adopted automatically when there is exactly one login configured; with several, the account that saved them cannot be known, so they are offered as something to confirm rather than resolved into a recipient.
- **Media:** send files, download media, upload files, send voice notes, stickers, GIFs, and inspect message media.
- **Peer photos:** `list_photos` indexes a peer's pictures, `open_photo` returns one, and
  `get_photo_sheet` returns them as a single labelled grid instead of one image block each.
  Two sources, because Telegram has no single photo list: `avatars` is the profile-picture
  history (Telegram's own call for a user; rebuilt from service messages for a group or
  channel), and `messages` is photos sent in the conversation, keyed by message id. An id
  from one source names nothing in the other. Every transfer is capped, and each sheet cell
  carries the id `open_photo` takes back.
- **Profile and privacy:** get your own account info, update profile fields, set or delete profile photos, inspect privacy settings, get user info/photos/status, and manage bot commands.
- **Folders and drafts:** list, create, update, reorder, and delete Telegram folders; save, list, and clear drafts.
- **Events:** wait for incoming messages with debounce (`wait_for_new_message`, `wait_for_settled_message`), optionally for one chat only via `chat_id` — without it any unrelated conversation wakes the wait — or enable the opt-in incoming event feed for callback-style delivery (see below).

All tool results that include Telegram user-controlled content are sanitized and, where practical, returned as structured JSON.

### How much a listing may return

Every tool that takes a `limit`, `page_size` or `context_size` runs it through one shared rule, and
every ceiling is declared in one table (`telegram_mcp/paging.py`) rather than decided per function —
they used to disagree, and several tools passed whatever arrived straight into RPC iteration and then
into the response.

- A count that is not a whole number — `0`, a negative, `2.5`, `nan`, `inf`, `true`, `"lots"` — is
  **refused** before anything is fetched. Zero and negative matter most: Telethon reads a
  non-positive limit as *no* limit, so the value that looks like "none" is the one that asks for
  everything.
- A count above the tool's ceiling is **clamped**, not refused, and the reply says so:
  `requested_limit`, `effective_limit`, a `limit_note`, and `has_more` so a short page is
  distinguishable from a trimmed one. Each tool's docstring names its own ceiling.
- Page-numbered listings (`get_chats`, `get_messages`, `search_global`, `get_participants`) validate
  the page number too and stop at 100,000 records in. `(page - 1) * page_size` does not overflow in
  Python — it just asks Telegram to skip past an arbitrary number of records — so the bound is
  explicit.

### Incoming Event Feed (callback mode, Claude Code only)

By default, an agent waits for replies by calling `wait_for_settled_message`, which blocks up to the MCP tool timeout and must be re-called — that works everywhere (Codex, Cursor, etc.) and is unchanged.

Clients that can wake an agent on external output (Claude Code's persistent `Monitor` on `tail -f`) can switch to callback mode instead:

1. The agent calls `enable_incoming_feed` (or set `TELEGRAM_EVENT_FEED=1` in the environment to auto-enable). Each settled incoming burst is appended as one JSON line to `${XDG_STATE_HOME:-~/.local/state}/telegram-mcp/incoming_feed.jsonl`, created readable by its owner alone — mode `0600` on POSIX, and a real owner-only ACL on Windows, where `chmod` toggles nothing but the read-only flag. Override the path with `TELEGRAM_EVENT_FEED_FILE` — an explicit path's directory must already exist. `incoming_feed_status` reports the effective path and a ready-to-use watch command.
2. The agent arms a persistent Monitor with the `watch_command` returned by the tool. Every new line re-invokes the agent with the burst summary; no blocking tool call is held open, and the chat stays free.

`disable_incoming_feed` switches back and waits for the consumer to actually stop; `incoming_feed_status` reports the current mode. While the feed is enabled it consumes settled bursts, so don't combine it with `wait_for_settled_message`. Feed lines contain user-generated `name` fields — treat them as untrusted data.

Nothing here grows without a ceiling, because a server that runs for weeks otherwise leaks disk, memory and context in three separate places:

| Bound | Default | Override |
|---|---|---|
| Feed file size before rotation (one previous generation is kept, so disk is bounded by ~2×) | 8 MiB | `TELEGRAM_EVENT_FEED_MAX_BYTES` |
| Age at which the rotated generation is deleted | 7 days | `TELEGRAM_EVENT_FEED_MAX_AGE_SECONDS` |
| Chats held in the pending-burst map | 500 | `TELEGRAM_EVENT_PENDING_MAX` |
| Age at which an uncollected burst is forgotten | 1 hour | `TELEGRAM_EVENT_PENDING_TTL_SECONDS` |
| Chats listed by one `wait_for_new_message` | 50 (`limit`, max 100) | — |

An override that is not a usable number — zero, negative, `nan`, `inf`, or not a number at all — is logged and ignored rather than silently removing the ceiling. Dropping a burst is never silent: `wait_for_new_message` and `incoming_feed_status` both report `dropped_total`, a per-reason breakdown, and the most recent drops. `tail -F` follows the name rather than the descriptor, so it reads across a rotation.

## Requirements

- Python 3.11+ (the test suite imports `tomllib`, which arrived in 3.11)
- Telegram API credentials from [my.telegram.org/apps](https://my.telegram.org/apps)
- A Telegram session string or file-based session
- An MCP client such as Claude Desktop, Cursor, or another MCP-compatible host
- Optional: [uv](https://docs.astral.sh/uv/) for local development

## Quick Start

> Do not install this server with `uvx telegram-mcp`, `uvx --from telegram-mcp`,
> or `pip install telegram-mcp`. The `telegram-mcp` name on PyPI is currently
> owned by a different project and does not install this repository. Passing
> `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, or `TELEGRAM_SESSION_STRING` to that
> package can expose Telegram account credentials to unrelated third-party code.

### 1. Clone and Install

```bash
git clone https://github.com/KiaroSama/telegram-mcp.git
cd telegram-mcp
uv sync
```

### 2. Generate a Session String

```bash
uv run session_string_generator.py
```

Follow the prompts. Save the generated session string securely.

For scripted setup or operational runbooks, choose the login method explicitly:

```bash
# QR login, recommended when you already have Telegram open on another device
uv run session_string_generator.py --qr

# Phone number + verification code login
uv run session_string_generator.py --phone
```

Without a flag, the generator keeps the interactive method prompt.

### 3. Configure Environment

Copy the example file and fill in your real values. Create it readable by you
alone: it holds your API hash and, in the single-account setup below, a session
string — and a session string is the account, with no password and no second
factor. A plain `cp` copies the example's mode, which a normal umask leaves
readable by every account on the machine.

```bash
install -m 600 .env.example .env
```

On Windows, `Copy-Item` then strip the inherited access. `/inheritance:r` is the
half that matters: `icacls /grant` on its own ADDS an entry and leaves the
inherited `BUILTIN\Users` one in place.

```powershell
Copy-Item .env.example .env
icacls .env /inheritance:r /grant:r "${env:USERDOMAIN}\${env:USERNAME}:(F)"
```

The server re-applies this at startup on both platforms and warns if it cannot,
so an existing `.env` gets repaired rather than silently left open.

Single-account setup:

```env
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_SESSION_STRING=your_session_string_here
```

By default, all Telegram MCP tools are exposed. If you want to prevent MCP
clients from sending messages or performing chat/account mutations, set
`TELEGRAM_EXPOSED_TOOLS=read-only` to expose only tools annotated with
`readOnlyHint=True`:

```env
TELEGRAM_EXPOSED_TOOLS=read-only
```

If read-only is too strict but `all` is too broad, append `+` and a
comma-separated list of tool names to also expose those specific write tools.
Every other write tool stays unregistered:

```env
TELEGRAM_EXPOSED_TOOLS=read-only+send_message,reply_to_message,send_file
```

An unknown name in the allowlist aborts startup, so a typo cannot silently
degrade into a narrower surface that looks like it worked.

This is an MCP tool-surface restriction, not a Telegram session sandbox or
reduced Telegram account permission. The Telegram session string still has its
normal authority inside the server process; read-only mode only prevents
non-read-only tools from being registered and exposed through MCP. Accepted
values are `all` (the default), `read-only`, and `read-only+<tool>,<tool>`.

Run the server locally:

```bash
uv run main.py
```

## MCP Client Configuration

For Claude Desktop or Cursor, point the MCP server at a cloned checkout of
this project:

```json
{
  "mcpServers": {
    "telegram-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/full/path/to/telegram-mcp",
        "run",
        "main.py"
      ],
      "env": {
        "TELEGRAM_API_ID": "your_api_id_here",
        "TELEGRAM_API_HASH": "your_api_hash_here",
        "TELEGRAM_SESSION_STRING": "your_session_string_here"
      }
    }
  }
}
```

To expose only read-only tools in Claude Desktop or Cursor, add this to the
server `env` block:

```json
"TELEGRAM_EXPOSED_TOOLS": "read-only"
```

Or keep read-only as the baseline and allow a few write tools on top:

```json
"TELEGRAM_EXPOSED_TOOLS": "read-only+send_message,reply_to_message"
```

Alternatively, install this repository directly from GitHub into a virtual
environment using a specific release tag or commit:

```bash
python -m venv .venv
. .venv/bin/activate
pip install "git+https://github.com/KiaroSama/telegram-mcp.git@<tag-or-commit>"
```

Then configure your MCP client to run the installed console script:

```json
{
  "mcpServers": {
    "telegram-mcp": {
      "command": "/full/path/to/.venv/bin/telegram-mcp",
      "env": {
        "TELEGRAM_API_ID": "your_api_id_here",
        "TELEGRAM_API_HASH": "your_api_hash_here",
        "TELEGRAM_SESSION_STRING": "your_session_string_here"
      }
    }
  }
}
```

Generate a session string without cloning the repo by sourcing this repository
from GitHub explicitly:

```bash
uvx --from "git+https://github.com/KiaroSama/telegram-mcp.git@<pinned-release-tag-or-commit>" telegram-mcp-generate-session
```

### Transports

The server speaks three MCP transports, selected with `MCP_TRANSPORT`:

| Value   | Transport                  | Use case                                                        |
| ------- | -------------------------- | --------------------------------------------------------------- |
| `stdio` | stdio (default)            | One dedicated server process per MCP client                     |
| `http`  | streamable HTTP            | One shared server for many clients (Claude Code, Codex, Cursor) |
| `sse`   | SSE (legacy HTTP)          | Clients that only support the deprecated SSE transport          |

For `http` and `sse`, the server binds `MCP_HOST`:`MCP_PORT` (default
`127.0.0.1:8765`); the streamable HTTP endpoint is `/mcp`, the SSE endpoint is
`/sse`.

DNS-rebinding protection is **on by default**, not off: FastMCP enables it during
construction because the server binds `127.0.0.1`, with an allow-list of
`127.0.0.1:*`, `localhost:*` and `[::1]:*`.

That default is why a domain needs configuring rather than merely permitting. If the
server is reached through a reverse proxy or any name other than localhost, set
`MCP_ALLOWED_HOSTS` (and optionally `MCP_ALLOWED_ORIGINS`) to allow that Host header,
e.g. `MCP_ALLOWED_HOSTS=mcp.example.com`. Comma-separated; a `:*` suffix allows any
port. Leave it unset while changing `MCP_HOST` and the localhost allow-list stays in
force, so the symptom is every request being **rejected** — not an unprotected server.

**A bind beyond this machine has to say what authenticates it.** `MCP_ALLOWED_HOSTS`
is not that: DNS-rebinding protection checks which *name* a request arrived under, which
stops a browser on your own machine being tricked into calling the server — it asks
nothing about *who* is calling. Every tool here acts as your Telegram account, so on a
routable address, reaching the port is the authorization. The server therefore refuses to
start on a non-loopback `MCP_HOST` unless one of these is set:

| Variable | Meaning |
|---|---|
| *(neither)* | Default. Loopback only — nothing to configure. |
| `MCP_TRUSTED_PROXY_AUTH=1` | A reverse proxy in front authenticates requests. The server cannot verify this; you are stating it. |
| `MCP_ALLOW_UNAUTHENTICATED_REMOTE=1` | Deliberately open on a network you trust. Warns on every start. |

This server implements no authentication of its own, and that is deliberate — a token
scheme no real client had exercised would read as protection without being any. Put
authentication in front of it, or keep it on localhost.

Prefer `http` when more than one MCP client (or many coding-agent sessions)
will use the server: a single long-lived process holds one Telegram
connection, instead of every client spawning its own Telethon session —
Telegram throttles and may flag accounts that open many parallel sessions.

Register the shared server with clients:

```bash
# Claude Code
claude mcp add --transport http telegram http://127.0.0.1:8765/mcp

# Codex
codex mcp add telegram --url http://127.0.0.1:8765/mcp
```

For stdio-only clients, bridge with [mcp-remote](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "telegram-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
    }
  }
}
```

## Windows launchers

Two PowerShell scripts sit at the repository root. Both resolve everything relative
to their own location, so they work from any directory and from a shortcut.

| Script | What it does |
|---|---|
| `start-mcp.ps1` | Runs the server through `uv`, teeing its output to a timestamped file in `logs/` without losing the terminal's colours or its TTY. |
| `Manage-Accounts.ps1` | Menu for the accounts in `.env`: list, add, remove, rename, or just generate a session string. |

`Manage-Accounts.ps1` edits only the `TELEGRAM_SESSION_*` lines and leaves the rest of
`.env` byte-for-byte alone — comments, ordering and every key it does not recognise.
Before any rewrite it copies the file to `.env.backup-<UTC>`, so undoing a mistake is a
rename rather than a reconstruction.

A session string is a live login to a Telegram account, so the menu reads one as hidden
input, never echoes it, and never writes it to its log — the log records labels and
counts only. Removing an account takes it out of `.env`; it does **not** revoke the
Telegram session, which is done from the app under Settings → Devices.

Adding the second account switches the server into multi-account mode, where write tools
require `account=<label>` and read-only tools fan out across every account when it is
omitted, returning one JSON object keyed by account label. The menu says so at the
moment it happens.

Choosing *Generate a session string* hands over to `session_string_generator.py` without
forcing a login method, so it offers both QR and phone code. A rejected two-factor
password is asked for again rather than ending the run — losing the login there means
spending a fresh SMS code to start over.

What you paste back is checked by Telethon's own parser, not by its length, and an
account is not written unless that parses as a session carrying an auth key. If the
generator exits without producing one, the menu says so and saves nothing. It runs through the existing
`.venv` rather than `uv run`, which would rebuild and reinstall the project first and
print build progress over the login prompt; `uv` is the fallback for when no venv exists.
The menu passes the label it already collected, so the generator does not ask again, and
the generator saves the line itself — pressing Enter at its save prompt is enough. That
removes the copy-paste of a 350-character secret entirely; the paste path remains for a
session string obtained some other way, and what you paste there is validated.

Both writers back `.env` up to `.env.backup-<UTC>` first. There is only one owner of the
line at a time: if the generator saved it, the menu says so and asks for nothing further.

The generator shares the menu's palette (`telegram_mcp/console_theme.py` carries it for
Python, `Manage-Accounts.ps1` for PowerShell, and `tests/test_console_theme.py` compares
the two so they cannot drift), and it skips the parts the menu has already said or asked.

The menu follows the same convention as the other launchers in this family: `0` steps
back one level and `exit` leaves, shown at each prompt as `{back=0, quit=exit}` with the
two keys in different colours. The main menu shows only `{quit=exit}`, because there is
nothing above it to go back to. Colours are 256-colour ANSI and are dropped when
`NO_COLOR` is set or the terminal has no virtual-terminal processing.

Both the menu and the generator normalise the label the same way, and the generator
refuses outright to write a key containing whitespace — such a line parses as nothing,
so a "successful" save would leave an account that looks configured and never loads.

Type a label however reads naturally — `KGB Verifier` is fine. Spaces and hyphens are
stored as underscores (`kgb_verifier`) and the menu tells you the stored form, because
that is the value tools take as `account=`. The substitution is not cosmetic: the label
becomes part of an environment variable *name*, and python-dotenv refuses to parse a key
containing a space — it warns and drops the line, so a literal space would save an
account that then never loads.

## Multi-Account Setup

Use suffixed session variables to configure multiple Telegram accounts:

```env
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_SESSION_STRING_WORK=session_string_for_work
TELEGRAM_SESSION_STRING_PERSONAL=session_string_for_personal
```

Labels are lowercased and become the `account` parameter value in tools.

- In single-account mode, `account` is optional.
- In multi-account mode, write tools require `account`.
- Read-only tools fan out to all accounts when `account` is omitted, and answer with
  `{"accounts": {"<label>": <that account's result>, ...}}`. An account that failed
  appears as `{"error": "..."}` rather than discarding the accounts that succeeded.

Example prompts:

- "List my accounts"
- "Show unread messages from all accounts"
- "Send this from my work account to @example"

### Session pool (one account, several concurrent clients)

To run several MCP clients against the **same** Telegram account at once (for
example the desktop app *and* a terminal CLI), give each client its own
authorized session. Telegram forbids one session (auth key) being used from two
IPs simultaneously, so on a VPN or dual-stack host two local clients can collide
with `AuthKeyDuplicatedError`. List several interchangeable session strings in
`TELEGRAM_SESSION_STRINGS` (separated by whitespace, comma or semicolon); each
process claims a free one via an advisory file lock, so clients deterministically
pick distinct sessions:

```env
TELEGRAM_SESSION_STRINGS=<session A> <session B> <session C>
```

Generate extra sessions with `uv run session_string_generator.py`. The pool
takes precedence over `TELEGRAM_SESSION_STRING` for the default account. As an
extra safety net, a transient `AuthKeyDuplicatedError` at connect time (e.g.
during a VPN reconnect) is retried with backoff before the server gives up.

Size the pool to the number of clients you actually run concurrently. If every
slot is already claimed, the server refuses to start with an explicit error
rather than reusing a session another client holds — reuse would make Telegram
permanently invalidate that session for both clients.

## Message Links

`TELEGRAM_LINK_DOMAIN` sets the domain used to build message permalinks; the default is
`t.me`. It is overridable because that default is a single point of failure: on
2026-07-13 the .me registry put `t.me` on serverHold over an OFAC listing and every
`t.me` link on earth broke for about a day, while `telegram.me` kept resolving.

```env
TELEGRAM_LINK_DOMAIN=t.me
```

## Device Identity

These optional variables control how the client appears in Telegram under
**Settings > Devices** (the active-sessions list):

```env
TELEGRAM_DEVICE_MODEL=Telegram MCP
TELEGRAM_SYSTEM_VERSION=1.0
TELEGRAM_APP_VERSION=1.0
```

If left unset, Telethon falls back to the host platform (for example `arm64`).
Because these values are re-sent on every connection, a long-running server
would otherwise overwrite the name chosen during login on each reconnect, so
set them to keep a stable, recognisable device name. The same variables are
read both by the session string generator (at login) and by the server (on
every connect), so set them in the same place as your other credentials.

## Proxy Support

Route Telegram traffic through a proxy by setting the `TELEGRAM_PROXY_*`
environment variables. Supported types are `socks5`, `socks4`, `http`, and
`mtproxy`.

SOCKS and HTTP proxies require the optional `python-socks` package:

```bash
uv sync --extra proxy
# or
pip install python-socks
```

Single-account configuration:

```env
TELEGRAM_PROXY_TYPE=socks5
TELEGRAM_PROXY_HOST=127.0.0.1
TELEGRAM_PROXY_PORT=1080
TELEGRAM_PROXY_USERNAME=optional_user
TELEGRAM_PROXY_PASSWORD=optional_pass
TELEGRAM_PROXY_RDNS=true
```

MTProxy:

```env
TELEGRAM_PROXY_TYPE=mtproxy
TELEGRAM_PROXY_HOST=mtproxy.example
TELEGRAM_PROXY_PORT=443
TELEGRAM_PROXY_SECRET=ee0123456789abcdef...
```

Per-account overrides use the same `_<LABEL>` suffix as session variables and
take precedence over the unsuffixed defaults:

```env
TELEGRAM_PROXY_TYPE=socks5
TELEGRAM_PROXY_HOST=127.0.0.1
TELEGRAM_PROXY_PORT=1080

TELEGRAM_PROXY_TYPE_WORK=http
TELEGRAM_PROXY_HOST_WORK=proxy.work.example
TELEGRAM_PROXY_PORT_WORK=3128
```

Misconfigured proxy settings (unknown type, missing host/port, invalid port,
missing MTProxy secret, or a missing `python-socks` package) cause the server
to fail fast at startup with a clear error message instead of silently
bypassing the proxy.

## File Path Security

File-path tools are disabled until allowed roots are configured. This affects tools such as `send_file`, `download_media`, `upload_file`, `send_voice`, `send_sticker`, `set_profile_photo`, and `edit_chat_photo`.

Allowed roots can come from:

- Server CLI arguments, used as a fallback.
- MCP client Roots, when supported by the client.

Security behavior:

- Client MCP Roots replace server CLI roots when available.
- Some clients (notably Cursor) return workspace roots as bare absolute paths
  instead of `file://` URIs. That breaks MCP SDK validation of `list_roots`;
  the server recovers those absolute paths from the validation error so
  file-path tools keep working.
- Empty client Roots are treated as deny-all by default. Some clients implement
  the Roots capability but advertise an empty list, which disables file tools
  even when server CLI roots are configured. Set
  `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1` to fall back to the server CLI roots
  in that case (opt-in; the default stays deny-all). The same opt-in also applies
  when `list_roots` fails unexpectedly and no client paths could be recovered.
- Paths are resolved through real paths and must stay inside an allowed root.
- Traversal, wildcard-like, shell-like, and null-byte path patterns are rejected.
- Relative paths resolve under the first allowed root.
- Downloads default to `<first_root>/downloads/`.
- Size and extension limits are enforced for sensitive media tools.

Run with allowed roots:

```bash
uv run main.py /data/telegram /tmp/telegram-mcp
```

From an MCP client configuration, pass the same roots after `main.py`:

```json
{
  "mcpServers": {
    "telegram-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/full/path/to/telegram-mcp",
        "run",
        "main.py",
        "/data/telegram",
        "/tmp/telegram-mcp"
      ],
      "env": {
        "TELEGRAM_API_ID": "your_api_id_here",
        "TELEGRAM_API_HASH": "your_api_hash_here",
        "TELEGRAM_SESSION_STRING": "your_session_string_here"
      }
    }
  }
}
```

## Docker

Build the image:

```bash
docker build -t telegram-mcp:latest .
```

### Shared server (recommended)

Run one long-lived container serving streamable HTTP, and point every MCP
client at it (see [Transports](#transports) for client registration):

```bash
docker run -d --name telegram-mcp --restart unless-stopped \
  --env-file .env \
  -e MCP_TRANSPORT=http \
  -e MCP_HOST=0.0.0.0 \
  -p 127.0.0.1:8765:8765 \
  telegram-mcp:latest
```

`MCP_HOST=0.0.0.0` binds inside the container so the published port works;
`-p 127.0.0.1:8765:8765` keeps the server reachable only from the local
machine — the endpoint is unauthenticated, so never publish it on a public
interface.

The bundled Compose file runs the same setup:

```bash
docker compose up --build -d
```

### One container per client (stdio)

Alternatively, an MCP client can spawn a dedicated container itself:

```json
{
  "mcpServers": {
    "telegram-mcp": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--env-file", "/full/path/to/.env", "telegram-mcp:latest"]
    }
  }
}
```

This is fine for a single client, but with several clients (or coding agents
that spawn subagent sessions) each one starts its own container and its own
Telegram session, which Telegram throttles; a client that exits uncleanly can
also leave its container running. Prefer the shared server above in those
setups.

For multiple accounts, pass variables such as `TELEGRAM_SESSION_STRING_WORK` and `TELEGRAM_SESSION_STRING_PERSONAL`.

## Development

The implementation is split into a small compatibility entrypoint and modular package code.
`settings.py` is deliberately the lowest layer - everything above needs some of it and none
of it needs them - and `runtime.py` re-exports the layers below so that
`from telegram_mcp.runtime import *` keeps working for every tool module.

**Patch a module at its source, not through `runtime`.** The star imports mean a name
exists in two places at once; rebinding the re-exported one leaves the code reading its
own. That is what `tests/test_tool_registry.py` guards.

```text
main.py                       # historical entrypoint and compatibility exports
telegram_mcp/settings.py      # environment configuration; the bottom of the import graph
telegram_mcp/runtime.py       # shared MCP setup, validation, entity resolution, formatting
telegram_mcp/connection.py    # proxies, the session pool, account routing, reconnection
telegram_mcp/file_roots.py    # allowed roots, and resolving a caller's path inside one
telegram_mcp/handles.py       # file access bound to an open handle, not to a pathname
telegram_mcp/safe_log.py      # the only module allowed to write a log line
telegram_mcp/paging.py        # one limit rule for every list and search tool
telegram_mcp/aliases.py       # calling a contact what the operator calls them
telegram_mcp/runner.py        # application startup
telegram_mcp/tools/           # tool modules grouped by domain
telegram_mcp/message_view.py  # deep structured message view
telegram_mcp/visual/          # Telegram Desktop capture and image/frame helpers
sanitize.py                   # output sanitization helpers
tests/                        # pytest suite, plus PowerShell suites for the launchers
```

Run tests:

```bash
uv run python scripts/run_tests_guarded.py --
```

The `.tgs` Lottie tests skip unless the optional renderer is installed, which is also how the
base install stays verified. To exercise that path too:

```bash
uv run --extra lottie python scripts/run_tests_guarded.py --
```

Run tests with coverage:

```bash
uv run python scripts/run_tests_guarded.py -- --cov --cov-report=term-missing --cov-fail-under=0
```

Coverage is configured in `pyproject.toml`: an 85% floor (`fail_under` under
`[tool.coverage.report]`) over the modules listed in `[tool.coverage.run] source`, which is
every module whose behaviour a test can pin down without a live Telegram connection. The
`telegram_mcp.tools.*` adapters are deliberately outside it — they marshal arguments to
Telethon, so measuring them would reward mocking the API rather than testing anything.
`tests/test_packaging.py` fails if any other module drifts out of that list.

**The floor is applied to both platforms combined, not to either one.** Neither runner can
execute the whole project: `telegram_mcp.visual.capture` is Win32 and cannot run on Linux,
while the POSIX branches of `handles.py`, `owner_only.py` and `singleton.py` cannot run on
Windows. On one commit that read 85.56% on Windows and 81.90% on Linux, against 85.75%
combined. So each CI test job writes its own coverage data file with `--cov-fail-under=0`,
and a separate job combines them, applies the floor once and uploads the combined
`coverage.xml`. `--cov-fail-under=0` above does the same locally: a single-platform number
is worth reading, but it is not the gate.

The launchers have their own suites, which `pytest` does not see. Run them on Windows:

```powershell
Get-ChildItem tests -Filter 'test_*.ps1' | ForEach-Object { pwsh -NoProfile -File $_.FullName }
```

CI discovers them the same way rather than from a list — a hardcoded list is how a suite
outlived the script it tested.

Run formatting checks:

```bash
uv run black --check .
uv run flake8 .
```

## Security Notes

- Never commit `.env`, session strings, or `.session` files.
- A Telegram session string grants access to the account it belongs to.
- The `telegram-mcp` package name on PyPI is not controlled by this project.
  Avoid PyPI-based `telegram-mcp` install commands unless ownership changes and
  the package is verified.
- This repository includes a best-effort startup guard that refuses installed
  `telegram-mcp` distributions without a source checkout or direct git/file
  install record. That guard cannot run when the unrelated PyPI package itself
  is launched, so use clone-based or explicit git installs.
- Prefer session strings over file sessions when running multiple server instances.
- By default, Telegram API calls go directly from your machine/container to Telegram.
  If `TELEGRAM_PROXY_*` is configured, Telegram traffic is routed through the
  configured SOCKS/HTTP/MTProxy proxy instead.
- User-generated Telegram content is sanitized before being returned to MCP clients.

### Prompt Injection Protection

Telegram messages, display names, chat titles, and button labels are untrusted content. The server mitigates prompt-injection risk with:

- Structured JSON output for user-controlled data where practical.
- `sanitize_user_content()`, `sanitize_name()`, and `sanitize_dict()` for control-character stripping, invisible-character stripping, and length limits.
- MCP content annotations marking returned content as user audience data.
- Tool descriptions that warn clients not to treat returned Telegram fields as model instructions.
- No brittle keyword-based filtering.

## Troubleshooting

- **No Telegram session configured:** set `TELEGRAM_SESSION_STRING`, `TELEGRAM_SESSION_NAME`, or suffixed multi-account variants.
- **Session is not authorized:** run `uv run session_string_generator.py --qr` outside
  the MCP server when you can scan from an existing Telegram app, or
  `uv run session_string_generator.py --phone` when you need phone-code login.
  Then set `TELEGRAM_SESSION_STRING` in `.env`. The MCP server does not perform
  interactive phone-code login over stdio.
- **Invalid API credentials:** verify `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` at [my.telegram.org/apps](https://my.telegram.org/apps).
- **Database is locked:** prefer string sessions, or make sure no other process is using the same file session.
- **Where is my `.session` file?** A bare `TELEGRAM_SESSION_NAME` resolves to
  `$XDG_STATE_HOME/telegram-mcp/` (`~/.local/state/telegram-mcp/` by default), a directory the
  server makes readable by you alone *before* Telethon creates the database in it — the database
  holds your auth key, so anyone who can read it is signed in as you, and the `-journal`/`-wal`/
  `-shm` files SQLite adds later are the same credential. A session an older version left beside
  the installation or in the working directory is moved there once, with its sidecars: those
  directories cannot be made private without stripping the permissions off everything else in
  them. Give `TELEGRAM_SESSION_NAME` a path to choose the location yourself — but that directory
  must already be readable by your account alone, or the account refuses to start rather than run
  from a credential anyone can read.
- **`AuthKeyDuplicatedError` / "Another telegram-mcp process is already connected with this session":** two processes tried to connect the same Telegram session at once (e.g. an MCP client restarted the connector before the old process exited), which Telegram rejects and can invalidate the session for both. The server now takes an exclusive lock per session before connecting; a second concurrent launch waits briefly (default 20s, override with `TELEGRAM_LOCK_GRACE_SECONDS`) for the first to release it and otherwise exits without ever calling `connect()`, instead of racing into a duplicate connection. Retry once only one instance is running.
- **File tools are disabled:** pass allowed roots or configure MCP Roots in your client.
- **Path rejected:** ensure the path is inside an allowed root and does not use traversal or wildcard patterns.
- **Auth errors after password changes:** regenerate your session string.
- **Bot-only tool rejected:** regular user accounts cannot manage bot command settings.
- **Need details:** check your MCP client logs, terminal output, and the server log at
  `$XDG_STATE_HOME/telegram-mcp/mcp_errors.log` (`~/.local/state/telegram-mcp/mcp_errors.log`
  by default; `%USERPROFILE%\.local\state\telegram-mcp\mcp_errors.log` on Windows). It is
  created readable by you alone and holds bounded metadata only: the failing tool, an error
  code, numeric ids and an exception type with a stable digest. Message text, names, titles,
  paths and queries are deliberately not in it, so quote the error code when reporting a bug.
- **Want the server's diagnostics on disk:** run `./start-mcp.ps1 -LogToFile` (or set
  `TELEGRAM_MCP_LAUNCHER_LOG=1`). Only stderr is written; stdout is the MCP protocol channel
  and carries whole tool results, so it is never persisted.

## Working on it

This is the maintainer's own loop. Issues and pull requests are welcome; the same checks
below are what a change has to pass.

1. Clone the repository.
2. Install dependencies and git hooks:
   - `uv sync`
   - `uv run pre-commit install --hook-type pre-commit --hook-type pre-push`
3. Create a focused branch.
4. Add or update tests when behavior changes.
5. Run checks locally:
   - `uv run pre-commit run --all-files`
   - `uv run pre-commit run --hook-stage pre-push --all-files`

   The pre-push stage includes `scripts/secret_scan.py`, which refuses a push carrying a
   credential shape, a protected path or a personal home directory. It runs before the
   push rather than after because this repository is public: GitHub's own guidance is
   that data reaching a fork stays reachable there, and neither the owner nor GitHub
   Support can remove it from someone else's fork. A deliberate example value belongs in
   that script's `KNOWN_PLACEHOLDERS`, so a real secret in an example file still fails.
6. Every test command on this page already goes through the guarded runner, and so does
   CI and the pre-push hook — an unbounded run has no wall ceiling and no process-tree
   cleanup, so a hang cannot be proven cleaned up. This project drives ffmpeg, ffprobe and
   a native rlottie decoder, any of which can wedge without exiting, and a wedged child
   outlives the pytest that spawned it. The run is therefore over only when pytest AND
   everything it spawned are gone: a descendant still alive after pytest exits is a leak,
   and the runner terminates the tree and exits 124 rather than reporting the pass:

   ```bash
   uv run python scripts/run_tests_guarded.py -- -q
   ```

   It bounds the run by wall clock *and* by silence (a deadlock prints nothing while still
   holding the CPU), terminates the whole process tree on either, and exits 124 on timeout
   while otherwise returning pytest's own exit code. CI runs the same script.

## Visual and structured access

Tools that let an agent see Telegram the way a person does — the full Telegram API view of a
message (entities, custom emoji, reactions, media metadata) and the real Telegram Desktop
rendering as an image — plus control over the two kinds of message that are not simply "sent
now": the scheduled queue, and media that destroys itself. Everything in the first three rows
below is read-only; the Actions row is not, and each entry there has a real effect.

| | Tools |
|---|---|
| Structured | `inspect_message`, `inspect_messages`, `get_media_details`, `inspect_buttons`, `list_scheduled_messages`, `list_disappearing_media` |
| Previews | `get_media_thumbnail`, `get_media_frames`, `get_custom_emoji`, `get_message_effect` |
| Visual (Windows) | `list_telegram_windows`, `get_telegram_screen`, `get_telegram_region`, `get_telegram_frames` |
| Actions | `click_button`, `schedule_message`, `edit_scheduled_message`, `cancel_scheduled_message`, `send_disappearing_media`, `save_disappearing_media` |

The scheduled queue is a separate history with its own message IDs, so the inherited
`send_scheduled_message` could queue something an agent then had no way to see, correct or stop.
`schedule_message` adds Telegram's recurring period — `repeat="daily"` or `"weekly"`, which the
server accepts only from a Premium account — and the other three read, edit and cancel the queue.

`list_disappearing_media` finds media the sender set to self-destruct, and finds it *before* it is
opened: a read starts the countdown and Telegram then drops the file, after which nothing can
fetch it. `save_disappearing_media` **writes the file to disk** — photo, video, voice, audio or document —
under the same allowed roots as `download_media`. It fetches the bytes exactly once and writes
those, because a second fetch after the countdown has started returns nothing. With roots
unconfigured nothing can be written and it says so, still returning a photo/video preview so the
content is at least visible meanwhile. Saving one keeps a copy the sender did not agree to leave
behind — the protocol permits it because the bytes already arrived, which is not the same as
consent, so every result says so.

The file's **extension comes from the sender**, so it is checked before anything is written. A
suffix that is not a dot plus one to seven letters or digits becomes `.bin` — `.webm:ads` would
otherwise make NTFS put the payload in an alternate data stream, leaving a file that looks empty
next to a reported path carrying the `:stream` suffix. A short denylist of extensions Windows
executes or follows (`.hta`, `.cmd`, `.exe`, `.lnk` and similar, matched case-insensitively) is
replaced too, because those are well formed and still dangerous in a folder the operator opens.
Either substitution is reported as `suffix_replaced`. It is a denylist rather than an allowlist
because this tool saves arbitrary media — a PDF, a zip, an mp3 — and an allowlist would refuse
legitimate documents.

`send_disappearing_media` sends with a timer through that same
gate; `seconds` is 1-60 or 0 for view-once, and anything longer is refused because Telegram
silently drops an out-of-range timer and would send permanent media instead.

`inspect_buttons` and `click_button` cover the inline ("glass") keyboard. The pairing matters:
a button label is written by whoever sent the message and can carry a bidi override that makes
it read as a different button, so `inspect_buttons` cleans every label, flags one that changed,
and publishes a stable index — and `click_button` presses by that index rather than by text.
`expect_text` is required as the readable guard — pass the label you saw at that index — but it is
not the identity: a bot can keep the label and swap the callback payload, and two raw labels can
clean to the same display string. So `inspect_buttons` also publishes a `press_token` per pressable
button and `click_button` requires it. It is an HMAC under a per-process key over the account, chat,
message, position, kind, raw label and raw payload; it reveals none of them, cannot be forged, and
stops verifying on any keyboard edit or a server restart.

See **[docs/visual-structured-access.md](docs/visual-structured-access.md)** for the tool
reference, requirements and limitations.

## Content types beyond plain messages

A message is not the only thing an account holds, and each of these was unreachable until it
had a tool. Grouped by what an agent can actually do with it.

| Area | Read | Write |
|---|---|---|
| **Polls** | `get_poll_results`, `get_poll_voters` | `vote_in_poll` (`create_poll` already existed) |
| **Stories** | `list_peer_stories`, `get_stories` | `react_to_story`, `post_story` |
| **Saved Messages** | `list_saved_dialogs`, `get_saved_history`, `list_saved_tags` | `name_saved_tag` |
| **Quick replies** | `list_quick_replies` | `send_quick_reply` |
| **Sticker sets** | `inspect_sticker_set`, `suggest_sticker_set_name` | `add_sticker_to_set`, `remove_sticker_from_set`, `move_sticker_in_set` |
| **Channel identity** | `check_channel_username` | `set_channel_username` |
| **Channel analytics** | `get_channel_statistics`, `get_similar_channels` | — |
| **Translation** | `translate` | — |

Four of these carry a caveat that is part of the feature rather than a footnote:

- **Poll options are identified by opaque bytes on the wire, not by index.** `vote_in_poll` takes
  the human-facing index and looks the blob up in the poll it just read, so a reordered poll
  cannot silently turn a vote into a vote for something else.
- **Sticker-set writes are not idempotent.** A timeout after the server applied the change looks
  exactly like a timeout before it, so a blind retry duplicates the sticker. Every write reports
  the set's count before and after, and `add_sticker_to_set` takes an `expected_count` that
  refuses the add if the set moved underneath you.
- **Saved Messages is not one flat chat.** Forwarding into it files the copy under the *original
  sender*, so it is a set of per-sender buckets; a reaction placed there doubles as a named tag.
- **Statistics come back as graph tokens, not numbers.** Telegram answers most graphs with a token
  that needs a second call, so `get_channel_statistics` resolves what it can and plainly labels
  what it could not — a token is never presented as data.

## Donate

If this project helps you, donations are appreciated.

| Currency | Network | Address |
| --- | --- | --- |
| Bitcoin (BTC) | Bitcoin | `bc1qmth5m03pu5hujw5xw5jmywam3jj3sqwqupesdt` |
| USDT, BNB, USDC, etc. | BEP20 | `0x0Bd0BA443a8B9cf15922bf7f0Bb0a4b495fD06Ef` |
| USDT, TRX, USDC, etc. | TRC20 | `TWBA3xFTqgZAeAYMxqo85xWnzvty3DcAhw` |
| Ethereum (ETH) | ERC20 | `0x0Bd0BA443a8B9cf15922bf7f0Bb0a4b495fD06Ef` |
| TON | TON | `UQCN8Umo_OfOWqImZetQsrNStPcmLkMAKajFyiCOhso23NDb` |
| Litecoin (LTC) | LTC | `ltc1qntqnnrunadurnw4cshv3qgspywrueyyeyngwuy` |
| Solana (SOL) | Solana | `7B2wkczUjmkDhETwQuknBL8sUsbuV7nErxc317TmQuwR` |
| Polygon (POL) | Polygon | `0x0Bd0BA443a8B9cf15922bf7f0Bb0a4b495fD06Ef` |

## Licence

GNU General Public License v3.0 or later. See [LICENSE](LICENSE) for the licence text and [NOTICE](NOTICE) for the copyright and prior-work attribution.

Portions originated in an Apache-2.0 project (below), and that licence permits their
inclusion in a GPLv3 work — the Apache Software Foundation states it plainly: *"Apache 2
software can therefore be included in GPLv3 projects."* The compatibility runs one way
only, so the program as a whole is GPLv3 while the prior Apache-2.0 grant continues to
cover what it originally covered.

## Built on

- [Telethon](https://github.com/LonamiWebs/Telethon)
- [Model Context Protocol](https://modelcontextprotocol.io/)

Portions of this codebase originated in [telegram-mcp](https://github.com/chigwell/telegram-mcp)
by chigwell and l1v0n1, under the Apache License 2.0, and have been substantially modified and
extended since. That attribution is recorded in [LICENSE](LICENSE).
