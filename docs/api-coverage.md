# API coverage: what this server exposes, and what Telegram has

Measured, not estimated. Snapshot: **Telethon 1.44.0, TL layer 227, 2026-08-17.**
Reproduce it by counting `telethon.tl.functions` request classes against the tools
registered in `telegram_mcp.tools` and the `functions.<ns>.<Name>Request` calls in
`telegram_mcp/`.

| | Count |
|---|---|
| MCP tools registered | **148** |
| TL namespaces | 23 |
| TL request classes in layer 227 | **802** |
| Raw TL requests this codebase calls | 69 |
| Telethon high-level client methods called | 27 |

## Why "100% of Telegram" is the wrong target

802 request classes is the whole protocol, including `langpack`, `smsjobs`,
`fragment`, `aicompose` and the login flow. Exposing all of it would produce
hundreds of tools an agent will never pick correctly, and every one still needs a
docstring, a test and a safety review. Tool choice degrades as the surface grows —
so coverage is worth measuring per **feature**, not per method.

## Surface by namespace

`raw-called` counts only direct `functions.*` calls; namespaces are also reached
through Telethon's high-level methods, so a `0` does not always mean untouched.

| Namespace | Requests | Raw-called |
|---|---|---|
| messages | 257 | 35 |
| account | 129 | 4 |
| payments | 66 | 0 |
| channels | 59 | 14 |
| phone | 44 | 0 |
| bots | 39 | 0 |
| stories | 34 | 0 |
| contacts | 29 | 10 |
| auth | 27 | 0 |
| help | 26 | 1 |
| chatlists | 12 | 0 |
| stickers | 12 | 0 |
| stats / upload | 9 each | 0 |
| aicompose / smsjobs / users | 8 each | 0 / 0 / 1 |
| langpack / photos / premium | 6 each | 0 / 3 / 0 |
| updates | 4 | 0 |
| folders / fragment | 2 each | 1 / 0 |

## Named features: reachable today

Probed by looking for the TL verbs that implement each one.

- Dialog folders / chat lists
- Drafts
- Forum topics (create / list / edit)
- Message search, global and filtered
- Read receipts and view counts
- Reading who reacted
- Sending a reaction
- Web App / Mini App launch

Plus everything this fork added: deep structured message access, entity offsets,
custom-emoji and effect resolution, glass-button inspection and pressing, Telegram
Desktop capture, the scheduled queue, and self-destructing media.

## Named features NOT reachable

Twenty-one, ranked by what they are worth to an agent rather than by TL size.

### Worth building

| Feature | Why | Cost |
|---|---|---|
| **Polls: create, vote, read results** | Common in every group. The poll question is already visible but the agent cannot vote or tally. | Small — 3 requests |
| **Stories: read, react, post** | An entire content type that is invisible today. Reading is the valuable half. | Medium — 34 requests, a new media shape |
| **Saved messages: tags and saved dialogs** | The account's own scratch space; the natural place for an agent to keep notes. | Small |
| **Translation** | One request, immediate utility on foreign-language chats. | Trivial |
| **Sticker-set management** | Directly relevant here — sibling projects already automate pack edits, and the caps are recorded in `CROSS_PROJECT_LESSONS.md`. | Medium, and **not idempotent**: never blind-retry `addStickerToSet`. |
| **Quick-reply shortcuts** | Canned responses map cleanly onto agent use. | Small |
| **Similar / recommended channels** | Cheap discovery, one request. | Trivial |

### Lower value, build on demand

Pinned-dialog ordering, channel statistics, business hours and greetings, fact-check,
todo lists, history import/export, message-level bot inline queries.

### Deliberately not building

| Feature | Reason |
|---|---|
| Payments, Stars, gifts, giveaways, boosts | Moving money. This server will not execute financial transfers; reading a balance is the most that could ever be justified, and even that invites the rest. |
| Two-step verification, password settings | Changing account security settings from an agent is the wrong place for that authority. |
| Terminating active sessions | Reading the device list is harmless; ending sessions is a security action for a human. |
| Login / QR / auth flow | Session creation already belongs to the operator's setup, and putting it behind a tool widens what a compromised agent can do. |
| Group and video calls | Needs WebRTC and a media stack, not just TL. Out of proportion to any agent benefit. |
| Secret chats | Telethon has no E2E implementation; see `.ai/DECISIONS.md`. |

## What this implies for detaching from upstream

The gaps above are **not** caused by the fork constraint — they are simply
unbuilt. What the constraint does cost is visible elsewhere:

- `get_media_label` checks `video` before `gif`, so an animation had to be
  corrected in a layer above instead of at the source.
- `sanitize_name` destroys ZWNJ, ZWJ and emoji tag sequences, which is why the
  whole `text_fidelity` layer exists.
- Seven POSIX-only test failures live in upstream test files and cannot be fixed.
- `tools/messages.py` (2041 lines) and `runtime.py` (1812 lines) cannot be split.

So detaching buys the ability to fix those four things at source. It buys nothing
towards the feature list, and it transfers every upstream defect to us. The
sequence that follows from this data: **detach, fix the four, then add features
from the "worth building" table in order** — not detach in order to rewrite.
