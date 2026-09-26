# 7. An approval comes from where the model cannot answer

Date: 2026-09-26

## Status

Accepted

## Context

Every tool here acts as the owner, on the owner's own Telegram account. The owner asked
for a safeguard that refuses by default what they did not ask for — a deletion, leaving a
group, a message copied out of a stranger's post — and still lets the action happen once
they explicitly approve it. It has to hold in every client this server is used from, not
only one.

The obvious design is an argument: `confirm=true`, or a phrase the agent passes after
"asking the user". It protects nothing. The same model that decided to delete a chat
writes the argument, and a message crafted to make it delete the chat can tell it to
write the argument too. An approval the model can produce is an approval the model can
forge.

## Decision

**An approval is only ever read from a channel the model cannot answer.** In order of
preference:

1. the client's own dialog, through MCP elicitation, where the client supports it;
2. an approval bot the owner creates, which messages their phone with buttons;
3. a short code the owner answers in Saved Messages from another device.

No channel available means the gated call is refused — never run, never downgraded to a
warning. That is what the established MCP firewalls do, and it is the only behaviour that
stays safe in a client nobody tested.

The safeguard refuses every tool call that would touch the approval bot's chat, or write
a pending approval code anywhere, whatever the model's reason. Without that one fixed
rule, the channels in 2 and 3 are reachable through the very tools they guard.

## Consequences

**Some clients will not be able to do dangerous things at all** until the owner sets up
the approval bot or answers in Saved Messages. That is the intended cost.

**The boundary is the MCP server, and it says so.** An agent that also has a shell can run
its own Telegram client with the session string from `.env` and bypass every rule here. No
safeguard inside an MCP server can prevent that; keeping the session out of an agent's
reach is the operator's job, and the documentation states it rather than implying more.

**A client-side hook can answer a dialog automatically.** Claude Code's `Elicitation` hook
does exactly that. It is configuration the owner writes, not something the model can do
from inside a tool call, and it is documented as the one way to switch this off.

## Alternatives considered

**`confirm=true` or an approval phrase in the arguments.** Rejected above: forgeable by
construction.

**A separate firewall process between client and server** (ressl/mcp-firewall, preloop).
Rejected for this project: it needs its own install and approval UI, and elicitation
already provides the human channel inside the protocol.

**Refuse dangerous tools outright, no approvals.** Rejected by the requirement: the owner
must be able to say yes.

## Addendum 2026-09-26: the kernel is its own package

The owner asked that the rules deciding what may run live apart from the tools and say,
where an agent will see it, that they are not an agent's to change. They live in
`telegram_mcp/safeguard/`. Every file there opens with a do-not-edit notice, the
package README repeats it, `.github/CODEOWNERS` makes the owner the reviewer of that
path, and `docs/INSTALL.md` tells agents the same: an AI agent may change the kernel
only when the owner explicitly asked for that exact change in the current conversation.
The server also refuses any tool call whose arguments point into that folder, so no
tool of this server can overwrite it. A notice cannot stop an agent with a shell; it
makes an unasked-for edit a visible breach of an instruction rather than an accident.

## Addendum 2026-09-26: the approval itself is sealed

The approval bot trusts presses from the owner's own accounts, and a Saved Messages reply
is written by the same account the agent acts as. An approval is therefore only as good as
the agent's inability to reach it, so the owner asked for that to be complete: a tool call
naming the bot's chat in any spelling (id, `@name`, `t.me/` or `tg://resolve` link) is
refused; a tool call aimed at an approval message the server posted in Saved Messages is
refused whatever it would do to it; and every tool result is scrubbed of approval requests
and of every approval code this server issued (`telegram_mcp/safeguard/sealed.py`). The
agent can neither see a code nor touch the message that carries it, so an answer can only
come from a person on another device.
