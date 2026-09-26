"""Folder access: which folders a file tool may touch without the owner, and which never.

The owner's rule (2026-09-26): only `files/downloads` and `files/outbox` inside the
installation are free, with the folders the owner configured on this machine and the ones
granted "always allow". Every other folder - the MCP client's roots included - waits for
allow / deny / always allow, for reads and writes alike. The rest of the installation
(kernel, code, .env, secrets.md) and the state directory are never reachable.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_mcp import safeguard
from telegram_mcp.safeguard import folders, grants, policy, taint


@pytest.fixture
def layout(tmp_path, monkeypatch):
    install = tmp_path / "install"
    state = tmp_path / "state"
    outside = tmp_path / "elsewhere"
    configured = tmp_path / "configured"
    for directory in (install / "telegram_mcp" / "safeguard", state, outside, configured):
        directory.mkdir(parents=True)
    (install / ".env").write_text("X=1", encoding="utf-8")
    monkeypatch.setattr(folders, "install_dir", lambda: install)
    monkeypatch.setattr(folders, "state_dir", lambda: state)
    monkeypatch.setattr(folders, "configured_folders", lambda: [configured])
    monkeypatch.setattr(grants, "grants_path", lambda: state / "always-approvals.json")
    grants.reset_cache()
    taint.clear()
    yield SimpleNamespace(
        install=install,
        state=state,
        outside=outside,
        configured=configured,
        files=install / "files",
    )
    grants.reset_cache()


def _same(a, b):
    return Path(a).resolve() == Path(b).resolve()


# --- which arguments are paths, and where a relative one points -----------------------


def test_path_arguments_are_found_in_every_file_parameter(layout):
    arguments = {
        "file_path": "a.jpg",
        "file_paths": ["b.jpg", "c.jpg"],
        "destination": "d.jpg",
        "rich_files": {"cover": "e.jpg"},
        "message": "/not/a/path/argument",
    }
    assert sorted(Path(p).name for p in folders.path_arguments(arguments)) == [
        "a.jpg",
        "b.jpg",
        "c.jpg",
        "d.jpg",
        "e.jpg",
    ]


def test_a_relative_path_resolves_inside_files(layout):
    assert _same(folders.resolve("downloads/x.jpg"), layout.files / "downloads" / "x.jpg")
    assert _same(folders.resolve("outbox/y.png"), layout.files / "outbox" / "y.png")


def test_the_default_download_folder_is_files_downloads(layout):
    assert _same(folders.default_download_dir(), layout.files / "downloads")


# --- the verdicts --------------------------------------------------------------------


def test_the_two_free_folders_and_their_subfolders_need_nothing(layout):
    for raw in ("downloads/x.jpg", "outbox/y.png", "downloads/2026/z.jpg"):
        verdict = folders.judge({"file_path": raw})
        assert (verdict.protected, verdict.outside) == (False, ()), raw


def test_configured_folders_count_as_always_allowed(layout):
    verdict = folders.judge({"file_path": str(layout.configured / "sub" / "a.jpg")})
    assert (verdict.protected, verdict.outside) == (False, ())


def test_any_other_folder_waits_for_the_owner_and_is_named(layout):
    target = layout.outside / "a.jpg"
    verdict = folders.judge({"file_path": str(target)})
    assert verdict.protected is False
    assert [Path(f).resolve() for f in verdict.outside] == [layout.outside.resolve()]


def test_another_folder_under_files_is_part_of_the_installation(layout):
    assert folders.judge({"file_path": "elsewhere/a.jpg"}).protected is True


@pytest.mark.parametrize(
    "raw",
    [
        "../telegram_mcp/safeguard/policy.py",
        "../.env",
        "../secrets.md",
        "../telegram_mcp/server.py",
    ],
)
def test_the_rest_of_the_installation_is_refused(layout, raw):
    assert folders.judge({"file_path": raw}).protected is True


def test_the_state_directory_is_refused(layout):
    verdict = folders.judge({"destination": str(layout.state / "main.session")})
    assert verdict.protected is True


def test_no_grant_or_configured_root_opens_a_protected_folder(layout, monkeypatch):
    grants.add_folder(layout.install)
    monkeypatch.setattr(folders, "configured_folders", lambda: [layout.install, layout.state])
    assert folders.judge({"file_path": "../.env"}).protected is True
    assert folders.judge({"file_path": str(layout.state / "x")}).protected is True


# --- folder grants -------------------------------------------------------------------


def test_an_always_grant_covers_the_folder_and_its_subfolders(layout):
    grants.add_folder(layout.outside)
    for target in (layout.outside / "a.jpg", layout.outside / "deep" / "b.jpg"):
        assert folders.judge({"file_path": str(target)}).outside == ()
    sibling = layout.outside.parent / "elsewhere-too"
    sibling.mkdir()
    assert folders.judge({"file_path": str(sibling / "a.jpg")}).outside


def test_folder_grants_survive_a_restart_and_can_be_revoked(layout):
    grants.add_folder(layout.outside)
    grants.reset_cache()
    assert grants.folder_granted(layout.outside / "x.jpg")
    assert [Path(p).resolve() for p in grants.list_folders()] == [layout.outside.resolve()]
    assert grants.revoke_folder(layout.outside) is True
    assert not grants.folder_granted(layout.outside / "x.jpg")
    assert grants.revoke_folder(layout.outside) is False


def test_folder_grants_do_not_disturb_tool_grants(layout):
    grants.add("main", "delete_message", "-100")
    grants.add_folder(layout.outside)
    grants.reset_cache()
    assert grants.is_granted("main", "delete_message", "-100")
    assert grants.list_all() == [{"account": "main", "tool": "delete_message", "chat": "-100"}]


# --- what the file tool may open ------------------------------------------------------


def test_allowed_roots_are_free_configured_granted_and_this_call_only(layout):
    grants.add_folder(layout.outside / "granted")
    roots = {Path(r).resolve() for r in folders.allowed_roots()}
    assert (layout.files / "downloads").resolve() in roots
    assert (layout.files / "outbox").resolve() in roots
    assert layout.configured.resolve() in roots
    assert (layout.outside / "granted").resolve() in roots

    once = layout.outside / "once"
    with folders.approved_for_this_call([once]):
        assert once.resolve() in {Path(r).resolve() for r in folders.allowed_roots()}
    assert once.resolve() not in {Path(r).resolve() for r in folders.allowed_roots()}


# --- the policy and the middleware ----------------------------------------------------


def test_policy_refuses_a_protected_folder_and_asks_for_an_outside_one():
    facts = policy.Facts(protected_folder=True)
    assert policy.decide("send_file", False, False, {}, facts).reasons == [
        "touches_protected_folder"
    ]
    facts = policy.Facts(outside_folders=("/x",))
    decision = policy.decide("send_file", False, False, {}, facts)
    assert (decision.outcome, decision.reasons) == ("ask", ["outside_project_folder"])


class _Channel:
    kind = "dialog"

    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def available(self):
        return True

    async def ask(self, request, timeout):
        self.requests.append(request)
        return self.outcome


def _run(outcome, arguments, name="send_file"):
    channel = _Channel(outcome)

    async def _first(account, chat):
        return False

    async def _identity(account):
        return account

    guard = safeguard.Safeguard(
        hints={"send_file": (False, False), "download_media": (False, False)}.get,
        channels=lambda ctx, account: [channel],
        first_message=_first,
        ghost_on=lambda account, chat: False,
        approval_chats=lambda: frozenset(),
        account_of=lambda arguments: "main",
        after=lambda account: None,
        identity=_identity,
        timeout=300,
    )
    seen = []

    async def call_next(ctx):
        seen.append({Path(r).resolve() for r in folders.allowed_roots()})
        return "RESULT"

    ctx = SimpleNamespace(
        method="tools/call", params={"name": name, "arguments": arguments}, request_id=1
    )
    return asyncio.run(guard(ctx, call_next)), channel, seen


def test_a_free_path_runs_without_asking(layout):
    result, channel, seen = _run("approved_once", {"chat_id": 5, "file_path": "outbox/a.jpg"})
    assert (result, channel.requests) == ("RESULT", [])


def test_allow_once_opens_the_folder_for_that_call_only(layout):
    target = layout.outside / "a.jpg"
    result, channel, seen = _run("approved_once", {"chat_id": 5, "file_path": str(target)})
    assert result == "RESULT"
    assert "outside_project_folder" in channel.requests[0].reasons
    assert str(layout.outside.resolve()) in channel.requests[0].effect
    assert layout.outside.resolve() in seen[0]
    assert not grants.folder_granted(target)


def test_always_allow_stores_the_folder_and_no_tool_grant(layout):
    target = layout.outside / "a.jpg"
    result, channel, seen = _run("approved_always", {"chat_id": 5, "file_path": str(target)})
    assert result == "RESULT"
    assert grants.folder_granted(target)
    assert grants.list_all() == []
    result, channel, seen = _run("approved_once", {"chat_id": 6, "file_path": str(target)})
    assert channel.requests == []


def test_deny_runs_nothing(layout):
    result, channel, seen = _run(
        "declined", {"chat_id": 5, "file_path": str(layout.outside / "a")}
    )
    assert result.is_error and seen == []


def test_a_protected_folder_is_refused_without_asking(layout):
    result, channel, seen = _run("approved_once", {"chat_id": 5, "file_path": "../.env"})
    assert result.is_error and channel.requests == [] and seen == []
    assert "installation" in result.content[0].text


# --- the owner's view and the way back -------------------------------------------------


def test_status_lists_folder_grants_and_revoke_removes_one(layout):
    import json

    from telegram_mcp.tools import ghost_tools

    grants.add_folder(layout.outside)
    status = json.loads(asyncio.run(ghost_tools.safeguard_status()))
    assert [Path(p).resolve() for p in status["always_allowed_folders"]] == [
        layout.outside.resolve()
    ]
    answer = json.loads(
        asyncio.run(ghost_tools.revoke_always_approval(folder=str(layout.outside)))
    )
    assert answer["revoked"] is True
    assert grants.list_folders() == []


def test_revoke_without_a_tool_or_a_folder_says_what_it_needs(layout):
    from telegram_mcp.tools import ghost_tools

    answer = asyncio.run(ghost_tools.revoke_always_approval())
    assert "folder" in answer and "tool" in answer
