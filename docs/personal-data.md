# Where an account's own data goes

This server talks to more than one Telegram account, and work done for a real
account leaves things behind: a scan of a channel, the ids a pack uses, a
half-finished job's progress file, a script written for one task. None of that
is a project file. It belongs to one person.

It goes here:

```
.personal/<telegram user id>/
```

`.personal/` is git-ignored and stays on the machine that made it. Ask for the
path rather than choosing one:

```python
from telegram_mcp.personal import personal_dir

directory = await personal_dir(client)          # .personal/5899781975/
(directory / "scan.json").write_text(payload, encoding="utf-8")
```

The directory is created the first time it is asked for, along with a short
`README.md` beside it so anyone who finds the folder later understands it
without reading the source.

## Why the folder is a number

**The name is the account's numeric Telegram id, never its label.**

A label is a name in `.env`. It can be renamed, removed and added back, or given
to a different account entirely. The numeric id is Telegram's own and does not
move.

Key the folder by the label and an account that returns as `refx_2` stops
finding the work it did when it was called `refx` — nothing errors, the data is
simply somewhere it no longer looks. Key it by the id and the label can change
as often as you like.

`personal_dir` takes the client and nothing else. There is deliberately no
`label` argument: a caller cannot pass the wrong one, and the label never
reaches the path or the id cache.

The cache behind it is keyed by the client object, not by a name. The first
version keyed it by label, and a second account borrowing a label was handed the
first one's directory - silently, which is what made it worth removing rather
than documenting.

## What does not go here

- **Anything the project itself needs.** If a second checkout would want it, it
  is a project file and belongs in the repository.
- **Secrets.** `secrets.md` and `.env` have their own rules; this is for
  material that is merely personal, not for credentials.
- **Another account's data.** One directory per id, and nothing crosses between
  them. That separation is the reason this exists.

## Relationship to `.ignoreme`

They are different directories with different guarantees, and the difference is
deliberate.

| | `.ignoreme/` | `.personal/<id>/` |
|---|---|---|
| written by the server | never | constantly |
| read by the server | never | yes |
| organised by | nothing | one directory per account id |

`.ignoreme` is read-only by rule: nothing may modify, copy, or transmit what is
in it. A directory that a running job writes a progress file into every
twenty-five messages cannot live under that rule, so it is its own thing.

## For contributors

`telegram_mcp/personal.py` is the only place that builds the path.
`tests/test_personal_dir.py` covers the cases that fail silently: a renamed
label, two accounts, and a label reused by a different account.

The ignore rule ships in `.gitignore`, so a fresh clone has it before anything
is written.
