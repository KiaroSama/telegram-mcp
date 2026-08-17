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

First probed by searching for the TL verbs that implement each one, then **verified
per feature** — because that probe is wrong in both directions. A substring match
over-reports (`WebView` matches the button-*description* code; `Search` matches
`contacts.SearchRequest`), and counting only raw `functions.*` calls under-reports,
because Telethon's high-level methods reach TL without ever naming it. Every row
below was confirmed against a real tool or call site.

| Feature | How it is reached |
|---|---|
| Dialog folders / chat lists | `messages.GetDialogFilters`, `UpdateDialogFilter` |
| Drafts | `messages.SaveDraft`, `GetAllDrafts` |
| Forum topics (create / list / edit) | `channels.CreateForumTopic`, `GetForumTopics` |
| Message search, in-chat and global | `search_messages` / `search_global`, through Telethon's `get_messages(search=…)` — no raw call, which is exactly why a raw-only count misses it |
| Read receipts — *who* read a message | `get_message_read_by` → `messages.GetMessageReadParticipants` |
| Reading who reacted | `messages.GetMessageReactionsList` |
| Sending a reaction | `messages.SendReaction` |

Plus everything this fork added: deep structured message access, entity offsets,
custom-emoji and effect resolution, glass-button inspection and pressing, Telegram
Desktop capture, the scheduled queue, and self-destructing media.

## Named features NOT reachable

Twenty-three, ranked by what they are worth to an agent rather than by TL size. Two
of them — Mini App launch and message view counts — were mis-reported as reachable
by the first probe and belong here, which is why every row in the table above was
verified individually.

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

Pinned-dialog ordering, channel statistics, fact-check, todo lists, history
import/export, message-level bot inline queries, message **view counts**
(`messages.GetMessagesViews`, distinct from the read receipts that already work),
and **Mini App launch** (`messages.RequestWebView` — `inspect_buttons` describes such
a button and says plainly that no callback can press it, but nothing launches one).

### Deliberately not building

| Feature | Reason |
|---|---|
| Payments, Stars, gifts, giveaways, boosts | Moving money. This server will not execute financial transfers; reading a balance is the most that could ever be justified, and even that invites the rest. |
| Two-step verification, password settings | Changing account security settings from an agent is the wrong place for that authority. |
| Terminating active sessions | Reading the device list is harmless; ending sessions is a security action for a human. |
| Login / QR / auth flow | Session creation already belongs to the operator's setup, and putting it behind a tool widens what a compromised agent can do. |
| Group and video calls | Needs WebRTC and a media stack, not just TL. Out of proportion to any agent benefit. |
| Secret chats | Telethon has no E2E implementation; see `.ai/DECISIONS.md`. |

## Planned work

Requested explicitly, in the order that keeps each step verifiable. Counts come
from the same measurement as the tables above.

### Phase 0 — detach, then fix at source

Prerequisite for the rest, because three of the four items below want changes in
upstream files. Stop treating them as untouchable, keep upstream as a reference
remote, and review its changes by hand.

1. `get_media_label`: check `gif` before `video`, and delete the correction layered
   over it in `message_view.py`.
2. `sanitize_name`: stop destroying ZWNJ, ZWJ and emoji tag sequences. Much of
   `text_fidelity` exists only to work around this and can then shrink.
3. The seven POSIX-only test failures: gate them on platform instead of leaving the
   suite permanently red on Windows.
4. Split `tools/messages.py` (2041 lines) and `runtime.py` (1812 lines).

### Phase 1 — full channel and group settings

**45 of 59 `channels.*` requests are unreached.** What exists today is title, photo,
admin rights, bans, slow mode, forum toggle, invite, join/leave and the admin log.
The settings an operator actually reaches for are all missing:

| Group | Requests |
|---|---|
| Usernames | `UpdateUsername`, `CheckUsername`, `ToggleUsername`, `ReorderUsernames`, `DeactivateAllUsernames` |
| Join gates | `ToggleJoinToSend`, `ToggleJoinRequest` |
| Visibility | `TogglePreHistoryHidden`, `ToggleParticipantsHidden`, `ToggleSignatures`, `ToggleViewForumAsMessages` |
| Discussion linking | `SetDiscussionGroup`, `GetGroupsForDiscussion` |
| Moderation | `ToggleAntiSpam`, `ReportAntiSpamFalsePositive`, `SetBoostsToUnblockRestrictions` |
| Appearance | `UpdateColor`, `UpdateEmojiStatus`, `SetStickers`, `SetEmojiStickers` |
| Structural | `ConvertToGigagroup`, `EditLocation`, `DeleteChannel`, `UpdatePaidMessagesPrice`, `ToggleAutotranslation` |

Self-contained, no new dependency, and every one is a single request. `DeleteChannel`
and `ConvertToGigagroup` are irreversible and must be annotated `destructiveHint`
and require explicit confirmation in their own docstrings.

### Phase 2 — files and chat transfer, made usable

`download_media` and `upload_file` already exist but are **disabled until allowed
roots are configured** — which is why saving a disappearing message needed its own
path. The work is not new TL, it is making the existing gate usable:

1. A tool that reports the current roots status and says exactly what to configure,
   so an agent hitting the gate can explain the fix instead of failing.
2. Bulk chat export: iterate a chat's history and write messages plus media to a
   directory under the roots. No TL beyond what is already used.
3. History **import** is genuinely absent — `messages.InitHistoryImport`,
   `StartHistoryImport`, `CheckHistoryImportPeer` — for pulling an exported archive
   from another app into Telegram.

### Phase 3 — Telegram Business

**11 unreached requests, all in `account`**, and a clean self-contained group:

`UpdateBusinessWorkHours`, `UpdateBusinessAwayMessage`, `UpdateBusinessGreetingMessage`,
`UpdateBusinessIntro`, `UpdateBusinessLocation`, plus business chat links
(`Create/Edit/Delete/Get/ResolveBusinessChatLink`) and `GetBotBusinessConnection`.

Expect a subscription gate exactly like `schedule_repeat_period`: the value is
accepted and the account is refused. Whatever that error turns out to be, report it
as a plain sentence rather than a raw RPC name — and **measure the gate rather than
assuming it**, the way the repeat periods were measured.

### Phase 4 — secret chats, if still wanted

Kept in the plan at your request. The position has not changed, and one new fact
makes it sharper: the only third-party layer for Telethon,
`telethon-secret-chat`, last released **2020-11-29** — roughly six years and many TL
layers behind the 227 this project runs on. An unmaintained crypto plugin is worse
than none, because it looks like a solution.

So there is no "add a library and expose a tool" route. The two honest options:

- **Audit and update that library** against layer 227 — read it fully, fix the
  message-layer wrapping, and re-verify the DH exchange, key fingerprints, sequence
  numbering and rekeying.
- **Implement the E2E layer here**, with the same work plus ownership of it.

Either way this is its own project with its own review, not a phase of this one, and
it needs three things decided first: where per-device keys live (not the shared
session file), what happens when a key is lost, and who reviews the crypto. Until
those exist, `start_secret_chat` would be a tool that promises encryption this
codebase cannot vouch for.

### Not in the plan

Unchanged from the table above: payments and Stars, password and two-step settings,
terminating sessions, the login flow, and group/video calls.

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
