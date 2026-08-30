# Should `TELEGRAM_EXPOSED_TOOLS` be able to name a group?

A design spike, not a proposal to build. Measured against commit `2f143e6`,
2026-08-30, when the server registered **180** tools. The surface has grown
since; the counts below are a snapshot, and the scope note says how to
re-derive them.

`TELEGRAM_EXPOSED_TOOLS` is the only lever an operator has for narrowing what an
agent can do with their Telegram account. Its grammar is
`read-only+<tool>,<tool>` parsed against literal tool names
(`telegram_mcp/runtime.py:180-246`), and an unknown name aborts startup
(`:237`). That abort is correct and must survive any change here.

The question is whether a token like `+module:messages` or `+safe` should be
allowed beside a literal name.

## What an operator has to type today

Three realistic postures, and the number of tool names each needs spelled out by
hand:

| Posture | Value | Names |
|---|---|---|
| Read-only plus the ability to send a message | `read-only+send_message` | **1** |
| Read-only plus messaging generally | `read-only+send_message,reply_to_message,forward_message,copy_message,edit_message,delete_message,send_file,send_album,send_voice,send_sticker,send_gif,save_draft,clear_draft,send_reaction,remove_reaction` | **15** |
| Everything except anything destructive | every non-destructive tool named individually | **107** |

That is the argument, and it is weaker than it first looks. The common case —
read-only plus one or two specific writes — costs one or two names, which nobody
would call friction. The list only becomes unmanageable at "everything except
destructive", which is precisely the posture an annotation could express in one
word.

The sharper cost is not typing it once. It is that **a stale list is a hard
startup failure**: rename or remove a tool and every operator who named it is
now unable to start the server until they edit `.env`. A group token would not
have that failure mode, which is a real argument for one.

## Are the annotations trustworthy enough to key on?

This is the question that decides everything, and it was measured rather than
assumed. Scanning every `@mcp.tool` decorator:

| | Count |
|---|---|
| Tools | 180 |
| `readOnlyHint=True` | 83 |
| `destructiveHint=True` | 73 |
| **Neither hint set** | **4** |

The four with no hint at all are `set_contact_alias`, `delete_contact_alias`,
`enable_incoming_feed`, `disable_incoming_feed`. None is read-only; none is
destructive in the "destroys data at Telegram" sense; all four change durable
local state. So the gap is not sloppiness — it is a third category the current
two booleans cannot express, and an annotation-keyed design would silently place
all four on whichever side its default chose.

**That is the finding.** The annotations are accurate where they are set, and
83 + 73 = 156 of 180 carry one. But `readOnlyHint=False` and
`destructiveHint=False` do not mean the same thing, and 24 tools sit in the gap
between them.

## The candidate designs

**By module** — `+module:messages`. Precise, and every tool already lives in
exactly one module. Two objections: it leaks the internal file layout into a
public configuration surface, and a module split silently changes what an
existing token means. This project split `tools/messages.py` and
`telegram_mcp/connection.py` within one week; that is not hypothetical.

**By annotation** — `+read-only`, `+non-destructive`. Follows intent rather than
layout, survives refactoring, and reads well. Its problem is the 24-tool gap
above: `+non-destructive` would have to decide about `enable_incoming_feed`
without being told, and whichever way it decided would be invisible.

**Both, with a precedence rule.** Doubles the surface for a lever whose whole
value is that it is simple enough to reason about.

## What any of them must not break

`runtime.py:237` aborts startup on an unknown tool name. Whatever a group token
is, the same has to hold: **an unknown or empty group must fail loudly at
startup, never resolve to "nothing" and never to "everything".** A token that
silently matched zero tools would narrow the surface without saying so; one that
silently matched all of them would widen it. Both failures are invisible from
the outside, which is the worst property a security lever can have.

## Recommendation

**Do not build group tokens yet. Fix the annotations first.**

The friction this would solve is real only for the "everything except
destructive" posture, and that is exactly the posture an annotation-keyed token
cannot express correctly today, because 24 tools sit in a gap the two booleans
do not cover. Building the resolver first would ship a lever that is confidently
wrong about an eighth of the surface.

The ordered work, if this is wanted:

1. **Give the annotations a third state, or a rule for the gap.** Decide what
   `enable_incoming_feed` and `set_contact_alias` are, write it down, and make
   the choice checkable — a test that every registered tool carries an explicit
   classification, the way `tests/test_tool_registry.py` guards the star-import
   trap. This is worth doing on its own merits, whether or not a group token
   ever exists.
2. **Then** add `+read-only` and `+non-destructive` as tokens keyed on those
   annotations, with the same fail-loud-on-unknown behaviour as a tool name.
3. Leave module tokens out. The layout is not a public contract, and this
   project moves it.

What would change this recommendation: if step 1 revealed the gap were two or
three tools rather than 24, the annotation design would be safe immediately and
worth building in the same change.

## Scope note

This spike inspected `runtime.py:180-246`, `README.md:212-226`, every
`@mcp.tool` decorator in `telegram_mcp/tools/`, and nothing else. It changed no
code. The tool counts go stale as the surface grows — re-derive them with
`grep -rc "@mcp.tool" telegram_mcp/tools/*.py` before relying on the tables above.
