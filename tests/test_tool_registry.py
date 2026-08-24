"""What `import pytest
import *` in ``tools/__init__.py`` does to the package namespace.

Registration works by side effect: importing a tool module runs its decorators. The
star import that triggers that also binds every tool NAME into ``telegram_mcp.tools``,
and this is the test for what that costs.

This file is what remains of ``test_merge_contract.py``. That file checked the fork
could still be merged with its upstream cleanly; the project no longer tracks one, so
those assertions were retired rather than left to rot into doc-maintenance busywork.
The check below was never about merging.
"""

import pytest

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_no_tool_name_shadows_the_module_it_lives_in():
    """`import *` binds tool names into the package, so a module whose name matches
    one of its own tools stops being reachable as an attribute: `tools.translate`
    returned the FUNCTION, not the module. That is silent - the server still starts,
    registration still works - and it only surfaces when something reaches for the
    module. Naming is the whole fix, so the check is on the names."""
    import importlib
    import inspect

    package = importlib.import_module("telegram_mcp.tools")
    init = (REPO / "telegram_mcp" / "tools" / "__init__.py").read_text(encoding="utf-8")
    modules = re.findall(r"^from telegram_mcp\.tools\.(\w+) import \*", init, re.M)
    assert modules, "no tool imports found"

    for name in modules:
        importlib.import_module(f"telegram_mcp.tools.{name}")
        bound = getattr(package, name)
        assert inspect.ismodule(bound), (
            f"telegram_mcp.tools.{name} resolves to {type(bound).__name__} "
            f"{getattr(bound, '__name__', bound)!r}, not the module - a tool inside it "
            f"is named {name!r} and `import *` overwrote the submodule attribute."
        )


def test_no_module_in_the_package_is_shadowed_by_a_name_it_exports():
    """The same trap, one level up. `runtime` star-imports its subsystem modules, so a
    global named after its own module - `clients.clients` was one - makes
    `telegram_mcp.clients` resolve to a dict for anyone who star-imported it. That is
    how the module named `clients` got renamed to `connection`.

    Checked across the whole package rather than just `tools`, because the first guard
    only covered `tools` and this one bit in `telegram_mcp/` a day later."""
    import importlib
    import inspect
    import pkgutil

    import telegram_mcp

    for info in pkgutil.walk_packages(telegram_mcp.__path__, "telegram_mcp."):
        module = importlib.import_module(info.name)
        own = info.name.rsplit(".", 1)[-1]
        exported = getattr(module, own, None)
        if exported is None or inspect.ismodule(exported):
            continue
        raise AssertionError(
            f"{info.name} exports a {type(exported).__name__} named {own!r}, the same as "
            f"its own module. Anything doing `from {info.name} import *` binds that value "
            f"over the submodule, and `{info.name}` then resolves to the value, not the module."
        )


# --- the whole registry, as a contract ----------------------------------------
#
# The two tests above catch one specific collision. These cover the surface every
# tool has to satisfy, because a registry is exactly the kind of thing that is
# correct 164 times and wrong once.


@pytest.fixture(scope="module")
def registered_tools():
    import asyncio

    import telegram_mcp.tools  # noqa: F401  (importing it is what registers them)
    from telegram_mcp.runtime import mcp

    return asyncio.run(mcp.list_tools())


def test_every_tool_name_is_unique(registered_tools):
    """A duplicate name does not error at registration; the later one silently
    replaces the earlier, and the lost tool is simply never callable again."""
    from collections import Counter

    duplicates = [name for name, n in Counter(t.name for t in registered_tools).items() if n > 1]

    assert duplicates == [], f"these names are registered more than once: {duplicates}"


def test_every_tool_has_a_description_a_title_and_a_schema(registered_tools):
    """The model picks tools from this text. A tool with no description is a tool
    that gets called by accident or not at all."""
    missing = {
        t.name: [
            field
            for field, value in (
                ("description", t.description),
                ("title", t.annotations.title if t.annotations else None),
                ("inputSchema", t.inputSchema),
            )
            if not value
        ]
        for t in registered_tools
    }
    incomplete = {name: fields for name, fields in missing.items() if fields}

    assert incomplete == {}, f"tools missing required registry fields: {incomplete}"


def test_a_read_only_tool_says_so_explicitly(registered_tools):
    """MCP's `readOnlyHint` defaults to None, which MEANS false - so a write tool
    may legitimately omit it, and 70 of them do. The direction that loses
    information is the other one: a genuinely read-only tool that stays silent
    reads to every client as a write, and the client asks for confirmation it did
    not need. Only tools the router treats as read-only are required to say so.
    """
    import telegram_mcp.tools as tools_package

    silent = [
        t.name
        for t in registered_tools
        if getattr(getattr(tools_package, t.name, None), "__telegram_readonly__", None) is True
        and not (t.annotations and t.annotations.readOnlyHint)
    ]

    assert silent == [], f"routed read-only but not declared read-only: {silent}"


def test_the_annotation_and_the_router_agree_about_what_each_tool_does(registered_tools):
    """`readOnlyHint` tells the client; `with_account(readonly=)` decides whether the
    tool may fan out across every account unattended. When they disagree, one of
    them is lying, and it is the router that has the real consequence: a write
    tool routed as read-only runs on every configured account at once.

    `save_disappearing_media` was found declaring readOnlyHint=False while routed
    readonly=True; only a separate decorator kept it out of the fan-out.
    """
    import telegram_mcp.tools as tools_package

    disagreements = {}
    for tool in registered_tools:
        function = getattr(tools_package, tool.name, None)
        routed_readonly = getattr(function, "__telegram_readonly__", None)
        if routed_readonly is None:
            continue  # not routed through with_account at all
        # None is not 'unset' here - MCP defines it as false, so it agrees with a
        # router that routes the tool as a write.
        declared = bool(tool.annotations.readOnlyHint) if tool.annotations else False
        if declared is not routed_readonly:
            disagreements[tool.name] = {
                "readOnlyHint": declared,
                "with_account(readonly=)": routed_readonly,
            }

    assert disagreements == {}, f"annotation and routing disagree: {disagreements}"


def test_every_registered_tool_is_reachable_by_name(registered_tools):
    """The registry and the package surface have to agree, or a name that a client
    can see is a name nothing can dispatch."""
    import telegram_mcp.tools as tools_package

    unreachable = [
        t.name for t in registered_tools if not callable(getattr(tools_package, t.name, None))
    ]

    assert unreachable == [], f"registered but not callable from the package: {unreachable}"


def test_a_destructive_tool_is_never_also_marked_read_only(registered_tools):
    """Contradictory hints are worse than missing ones: a client that trusts
    readOnlyHint would skip confirmation on a tool that deletes."""
    contradictory = [
        t.name
        for t in registered_tools
        if t.annotations and t.annotations.readOnlyHint and t.annotations.destructiveHint
    ]

    assert contradictory == [], f"marked both read-only and destructive: {contradictory}"
