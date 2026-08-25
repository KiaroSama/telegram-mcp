# API coverage: what this server exposes, and what Telegram has

Measured, not estimated. Snapshot: **Telethon 1.44.0, TL layer 227, 2026-08-22.**

Reproduce the tool count by `await mcp.list_tools()` after importing
`telegram_mcp.tools`, not by grepping for `@mcp.tool` — the two agreed here, but only
the first survives a name collision. Reproduce the TL count by walking
`telethon.tl.functions` recursively and collecting *unique classes* that subclass
`TLRequest`; counting names that end in `Request` with `dir()` double-counts
re-exports, which is where the earlier 802 came from.

| | Count |
|---|---|
| MCP tools registered | **165** |
| TL namespaces | 23, plus the root `functions` module |
| Unique `TLRequest` classes in layer 227 | **800** |
| Raw TL requests this codebase calls | 97 |

The tool count was **148** in the previous revision of this document. That number was
wrong, not stale: the server registered 142 at that commit. It is corrected here
rather than quietly overwritten, because the whole point of this file is that its
numbers can be re-derived.

## Why "100% of Telegram" is the wrong target

800 request classes is the whole protocol, including `langpack`, `smsjobs`,
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
per feature** — because that probe was wrong three separate times, in both
directions. A substring match
over-reports (`WebView` matches the button-*description* code; `Search` matches
`contacts.SearchRequest`), and counting only raw `functions.*` calls under-reports,
because Telethon's high-level methods reach TL without ever naming it. Every row
below was confirmed against a real tool or call site.

Every claim in this document was checked that way in the end, and it kept mattering:
Mini App launch and view counts were wrongly listed as present, poll *creation* was
wrongly listed as absent, and message search is present through a path no raw count
can see. Treat a TL-verb grep as a starting point, never as the answer.

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

## The "worth building" list, now built

Every row of what this document previously listed as worth building has been built,
in the modules named beside it. The list is kept rather than deleted, because a
coverage document that only shows the current state cannot be checked against what it
predicted.

| Feature | Tools | Module |
|---|---|---|
| Polls: vote and read results | `get_poll_results`, `vote_in_poll`, `get_poll_voters` | `tools/polls.py` |
| Stories: read, react, post | `list_peer_stories`, `get_stories`, `react_to_story`, `post_story` | `tools/stories.py` |
| Saved messages: tags and saved dialogs | `list_saved_dialogs`, `get_saved_history`, `list_saved_tags`, `name_saved_tag` | `tools/saved.py` |
| Quick-reply shortcuts | `list_quick_replies`, `send_quick_reply` | `tools/saved.py` |
| Translation | `translate` | `tools/translation.py` |
| Sticker-set management | `inspect_sticker_set`, `suggest_sticker_set_name`, `add_sticker_to_set`, `remove_sticker_from_set`, `move_sticker_in_set` | `tools/stickers.py` |
| Channel username (the identity gap) | `check_channel_username`, `set_channel_username` | `tools/channel_admin.py` |
| Channel statistics | `get_channel_statistics` | `tools/channel_admin.py` |
| Similar / recommended channels | `get_similar_channels` | `tools/channel_admin.py` |

That is **23 new tools**, 142 → 165, and it closes Phase 1's identity gap and all of
Phase 1b. Two things learned while building them are worth more than the count:

- A tool named the same as its own module is silently unreachable. `import *` in
  `tools/__init__.py` binds the tool name over the submodule attribute, so
  `telegram_mcp.tools.translate` resolved to the *function*. The module is
  `translation.py` for that reason, and `test_tool_registry.py` now fails on any
  future collision.
- `stats.*` returns most of its numbers as **graph tokens**, not values. A tool that
  reports them as data would be reporting a token as a statistic, so
  `get_channel_statistics` resolves what it can and labels what it did not.

### Still not reachable, lower value, build on demand

Pinned-dialog ordering, fact-check, todo lists, history
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

## Administering a channel or group: what already ships

Checked by tool name against the registered tools, because most of this was
assumed missing and is not. Nothing below needs building.

| Operation | Tool |
|---|---|
| Create a channel | `create_channel` |
| Create a group / community | `create_group`, then `enable_forum_topics` for topic mode |
| Ban, unban, list bans | `ban_user`, `unban_user`, `get_banned_users` |
| Block / unblock a user | `block_user`, `unblock_user`, `get_blocked_users` |
| Promote, demote, set rights | `promote_admin`, `demote_admin`, `edit_admin_rights`, `get_admins` |
| Default member permissions | `set_default_chat_permissions` |
| Add members, invite links | `invite_to_group`, `export_chat_invite`, `get_invite_link`, `import_chat_invite` |
| Rename a channel or group | `edit_chat_title` |
| Set its description / bio | `edit_chat_about` |
| Its photo | `edit_chat_photo`, `delete_chat_photo` |
| Own name and bio | `update_profile` |
| Own profile photo | `set_profile_photo`, `delete_profile_photo` |
| Slow mode, forum mode | `toggle_slow_mode`, `enable_forum_topics` |
| Admin log (recent actions) | `get_recent_actions` |
| Participants | `get_participants` |
| Archive, mute, pin | `archive_chat`, `mute_chat`, `pin_message`, `unpin_all_messages` |
| Leave, clear history | `leave_chat`, `delete_chat_history` |

**Only two things from that whole area were genuinely missing, and both are now
built**: channel/group **statistics** (`get_channel_statistics`) and changing a
channel's **public username** (`set_channel_username`, with `check_channel_username`
to test a name first). Until those landed, the ID a channel is reached by could not be
changed at all — the one identity-level setting with no route.

## Planned work

Requested explicitly, in the order that keeps each step verifiable. Counts come
from the same measurement as the tables above.

### Phase 0 — fix at source (now unblocked)

The project has detached: it no longer tracks an upstream, and inherited files are no
longer treated as untouchable. That was the prerequisite for all four items here, and
none of them is started. Each one currently exists as a workaround somewhere else in
the tree, which is the cost of having deferred them.

1. `get_media_label`: check `gif` before `video`, and delete the correction layered
   over it in `message_view.py`.
2. `sanitize_name`: stop destroying ZWNJ, ZWJ and emoji tag sequences. Much of
   `text_fidelity` exists only to work around this and can then shrink.
3. ~~The five POSIX-only test failures~~ **done.** They asserted `os.chmod` mode bits,
   which Windows does not implement — `chmod` there toggles only the read-only flag, so
   a file made "unreadable" stays readable and `st_mode` never reports `0o600`. They now
   skip on non-POSIX, which keeps the security check where it holds and stops a
   permanently-red suite from hiding the next real regression.
4. ~~Split `tools/messages.py` and `runtime.py`~~ **done**, along with `tools/groups.py`,
   `tools/chats.py` and `tests/test_runtime.py`. Nothing in the tree is over 800 lines
   except `tools/contacts.py` at 802, which is one cohesive module and was left alone.
   Splitting surfaced three instances of the same trap - a star import creates a second
   name for one object, and the two drift the moment either is rebound - so
   `tests/test_tool_registry.py` now guards it across the whole package.

### Phase 1 — full channel and group settings

**42 of 59 `channels.*` requests are unreached** (17 raw-called, remeasured after the
username work). What exists today is title, photo,
admin rights, bans, slow mode, forum toggle, invite, join/leave and the admin log.
The settings an operator actually reaches for are all missing:

| Group | Requests |
|---|---|
| Usernames | ~~`UpdateUsername`, `CheckUsername`~~ **built**; still open: `ToggleUsername`, `ReorderUsernames`, `DeactivateAllUsernames` |
| Join gates | `ToggleJoinToSend`, `ToggleJoinRequest` |
| Visibility | `TogglePreHistoryHidden`, `ToggleParticipantsHidden`, `ToggleSignatures`, `ToggleViewForumAsMessages` |
| Discussion linking | `SetDiscussionGroup`, `GetGroupsForDiscussion` |
| Moderation | `ToggleAntiSpam`, `ReportAntiSpamFalsePositive`, `SetBoostsToUnblockRestrictions` |
| Appearance | `UpdateColor`, `UpdateEmojiStatus`, `SetStickers`, `SetEmojiStickers` |
| Structural | `ConvertToGigagroup`, `EditLocation`, `DeleteChannel`, `UpdatePaidMessagesPrice`, `ToggleAutotranslation` |

Self-contained, no new dependency, and every one is a single request. `DeleteChannel`
and `ConvertToGigagroup` are irreversible and must be annotated `destructiveHint`
and require explicit confirmation in their own docstrings.

**Not in this phase because it already ships:** creating a channel or group, banning
and unbanning, admin rights, adding members and invite links, renaming, the
description, the photo, slow mode, forum mode, the admin log and participants — see
the table above.

The highest-value single item was `UpdateUsername`, because a channel's public ID was
the one identity-level setting with no route at all. It is now `set_channel_username`,
paired with `check_channel_username` so a taken name is reported *before* the attempt
rather than as an error afterwards. The rest of this phase is unbuilt.

### Phase 1b — statistics — **done**

Shipped as `get_channel_statistics`. What the plan predicted turned out to be the
whole difficulty: Telegram returns most of `stats.*` as **graph tokens**, not values,
and a token needs a second `LoadAsyncGraph` that can itself answer `StatsGraphError`.
So the tool resolves what it can and labels what it could not — presenting a token as
if it were the graph would be the one genuinely dishonest outcome.

Two constraints found while building it, both reported as plain sentences rather than
leaked as raw RPC errors: statistics are available only to admins of channels above
Telegram's member threshold, and a graph token is only loadable from the DC that
issued it. Broadcast and megagroup take *different* requests, so the tool picks by
what the chat actually is instead of guessing.

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
accepted and a non-Premium account is refused, while a Premium one goes through — both
halves of that have now been observed. Whatever that error turns out to be, report it
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

## Detaching: what it changed

Decided and done. This section is kept as the record of what the constraint cost,
because the four items it lists are the work that decision unlocked and none of them
is finished yet.

While the project tracked an upstream, every change had to live in a **new file** so
that a merge stayed conflict-free. That discipline held — the final merge was clean,
with no conflicts — but it was paid for in workarounds:

- `get_media_label` checks `video` before `gif`, so an animation has to be corrected
  in a layer above instead of at the source.
- `sanitize_name` destroys ZWNJ, ZWJ and emoji tag sequences, which is the entire
  reason the `text_fidelity` layer exists.
- Five POSIX-only test failures lived in inherited test files and were left red
  (now fixed — the first thing detaching actually bought).
- `tools/messages.py` and `runtime.py` could not be split (now done - the second
  thing detaching bought).

What detaching does **not** buy is any part of the feature list — the gaps measured
above were never caused by the constraint, they were simply unbuilt, and the whole
"worth building" table was in fact delivered *while still merging cleanly*. What it
buys is Phase 0, and what it costs is that every inherited defect is now ours to fix.
