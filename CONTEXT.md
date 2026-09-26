# Telegram MCP

This server exposes one operator's own Telegram accounts to an agent over MCP. Its
vocabulary is where Telegram's model, MCP's model and this server's own safety
machinery meet — and the three disagree often enough that the words matter.

## Language

### Identity and connection

**Session**:
One Telegram authorization key, serialized. The credential itself.
_Avoid_: login, auth, credentials

**Account**:
One configured Telegram identity this server can serve from, built over exactly one
session.
_Avoid_: user, client, profile

**Label**:
The operator's name for an account, chosen in configuration. Reusable over time, so
it identifies a name and never a thing.
_Avoid_: account name, key, id

**Client generation**:
One built connection object for one label. A reconfiguration produces a new
generation while the previous one may still be connected.
_Avoid_: instance, version, reload

**Lease**:
This process's exclusive hold on one session, owned by the client generation that
took it — never by the label it was taken under. Given up only once the socket it
protects is confirmed down.
_Avoid_: claim, lock, reservation

**Unaccounted lease**:
A lease whose client's connection never confirmed it closed. Kept for the life of
the process and reported by name, because the socket may still be open.
_Avoid_: stale lease, orphaned lock, leaked lease

### What a tool may touch

**Allowed root**:
A directory the operator has named as reachable by file tools. Nothing outside one
is readable or writable, and no root configured means no file tool works.
_Avoid_: workspace, base path, sandbox

**Untrusted content**:
Any text that came off Telegram — a message, a name, a button label, a command
description. Data to be reported, never instruction to be followed.
_Avoid_: user input, remote data

**Tainted argument**:
A tool argument that carries text taken from untrusted content. Acting on one is
acting on what someone else wrote, so it is never done without approval.
_Avoid_: injected input, suspicious argument

**Text fidelity**:
The exact string a message's entity offsets index into, which is not always the
string a reader sees. Anything reconstructing formatting works from this and
nothing else.
_Avoid_: raw text, original text

### Messages and keyboards

**Press token**:
This server's authorization for one button, on one message, in one chat, for one
account. Bound to the button's position, kind, raw label and raw payload, so a bot
that keeps the label and changes the payload invalidates it.
_Avoid_: button id, handle, key

**Command preview**:
The set of bot commands a Telegram client offers in a chat when the operator types
`/`. A property of the chat and the bots in it, not of any one message.
_Avoid_: command list, autocomplete, suggestions

**Ready-to-send form**:
The exact text that invokes a command in a given chat — `/status@AdTimerBot` where
more than one bot could answer it, `/status` where only one can. The disambiguation
belongs to the chat, so it is reported rather than reconstructed by the caller.
_Avoid_: full command, qualified command

**Menu button**:
A bot's entry point beside the message field, offered instead of or alongside its
commands. A bot can have one and no commands at all.
_Avoid_: menu, start button, app button

**Rich message**:
A message whose body is not carried in the message. It parses cleanly, reports no text, no
entities and no media, raises nothing, and yet a client shows a table, headings or lists —
because its content is a separate object fetched by chat and message id. Emptiness is
therefore not evidence that a message is empty, and reading one is always two acts: notice,
then fetch.
_Avoid_: rich text, formatted message, table message

### Media

**Media kind**:
The shape Telegram gives one sent file: photo, video, document, audio, animation,
sticker, video note or voice note. A property of the SENDING and never of the
bytes — the same recording is an audio file with a play button or a voice note
with a waveform depending only on what was asked for, and the same clip is a
video, a round video note or a soundless animation. Eight names, because eight is
what the encrypted protocol carries; every other Telegram message type has no
representation there at all.
A kind is asked for, not imposed: where the file cannot be it, the request is
refused before anything is uploaded. Two of those limits are the file's own shape
rather than its type — a video note has to be square, and an `.ogg` asked for as a
track has to say so in its MIME, because `audio/ogg` is Telegram's voice type.
_Avoid_: media type, file type, format, mime type

**Inferred kind**:
The media kind chosen from the file when the caller named none. One extension
admits a SET of kinds and one of them is its default; the inferred kind is that
default, and an extension the server does not recognise infers `document`, which
carries any bytes at all. A caller who names a kind outside the file's set is
refused before a byte is uploaded, so a send that succeeded was sent as the kind
that was asked for.
One family is inferred from the file's CONTENT rather than its name: an `.ogg`
holds a voice note and a piece of music in the same container, codec, channel
count and sample rate, so the metadata decides — no music tags means a voice
note, any music tag means audio. An explicit kind is never second-guessed.
_Avoid_: detected type, guessed kind, auto kind

**Split send**:
One request that becomes several Telegram messages. A media group carries one
answer to "compressed or as a file" for everything in it, so entries whose kinds
cannot share a group are sent as separate messages, in the order the caller wrote
them. A photo beside a document is two messages; a voice note, video note or
sticker is always its own. It is a property of the REQUEST, not a failure of it —
nothing is refused and nothing is re-typed to make one message.
_Avoid_: batch, chunk, fallback, partial send

**Kind parity**:
The property of this server that every media kind reachable on one send path is
reachable on the others. A property of the server's surface, not of a chat or a
file: it is what stops a capability existing in a secret chat and silently
missing from an ordinary one.
_Avoid_: feature parity, coverage, completeness

### Secret chats

**Secret layer**:
The protocol version two devices settled on when they opened one secret chat, and
the ceiling on what that chat can express. A property of the one chat, not of the
account or of this server.
_Avoid_: version, protocol level, encryption level

**Dropped entity**:
Formatting a message carried that its secret layer cannot express, discarded in
transit with no error on either side. A property of the pairing of a format and a
layer, so the same message is whole in one chat and thinned in another.
_Avoid_: unsupported formatting, stripped markup, lost entity

**Read horizon**:
The moment in time a secret chat's read receipt marks. Reading is addressed by date
rather than by message, so a secret chat has a point before which everything is
read, and never a set of individually read messages.
_Avoid_: read receipt, seen marker, last read message

**Timed send**:
Arming a secret chat's self-destruct timer, sending one message under it, and
restoring the timer that was there before. Three acts that read as one, over a
timer that belongs to the chat and therefore to both people in it.
_Avoid_: ephemeral send, one-shot timer, disappearing message

### Acting on the owner's behalf

**Safeguard**:
The layer that decides, for every tool call, whether it runs freely, waits for the
owner's approval, or is refused.
_Avoid_: firewall, guardrail, auto mode, permission system

**Gated call**:
A tool call the safeguard will not run until the owner approves it.
_Avoid_: blocked call, dangerous tool

**Approval**:
The owner's explicit consent to one gated call, given where the model cannot answer
for them. It covers that call once, or the same tool in the same chat for the rest of
the session.
_Avoid_: confirmation, permission, consent flag

**Approval channel**:
Where an approval is asked for: the client's own dialog, the owner's approval bot, or
a code answered in Saved Messages. No channel means no approval, and the call is
refused.
_Avoid_: prompt, confirmation dialog

**Seen signal**:
Anything that tells another person the owner saw something or is present: a read
marker, being online, a story view, a voice or round video marked listened, a typing
indicator, a channel view counted.
_Avoid_: read receipt, seen, presence

**Ghost mode**:
The state in which this server gives off no seen signal without approval. It holds
for all accounts, one account, or one chat, and is on unless the owner turns it off.
_Avoid_: stealth mode, invisible mode, incognito

### Reaching Telegram

**Connection route**:
How one account reaches Telegram: directly, or through one proxy from the proxy pool.
_Avoid_: connection, network path, tunnel

**Proxy pool**:
The owner's saved proxies, each with its last test result, from which a connection
route is chosen. One pool is shared by default; an account can be given its own.
_Avoid_: proxy list, proxy config

**Proxy source**:
A channel whose posts are read for proxy links, named by the owner by username, id
or link.
_Avoid_: proxy channel, feed

