"""Every tool a module defines is a tool the package exports.

`tools/__init__.py` collects the surface with `from ... import *`, so a module
that declares `__all__` publishes exactly that list and nothing else. Drop a name
from one and the tool does not break, does not warn, and does not appear: it
simply stops existing for every MCP client, and the only test that would have
noticed is one that names it.

`tests/test_groups_split.py` guards that for the groups/moderation/invites/
admin_rights family by pinning the historical tool set. This file makes the same
guarantee structural instead of enumerated, so it covers the 2026-09-05 splits
(`secret_messaging`, `messages_delete`, `admin_rights`) and every split after
them without anyone remembering to add a list.

Seventeen modules deliberately have no `__all__` and are not required to grow
one - `import *` without it publishes every public name, which is a different
(looser) contract, not a broken one. They are still checked for the shadowing
trap below.
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import telegram_mcp.tools as tools_package

MODULES = [
    importlib.import_module(f"telegram_mcp.tools.{info.name}")
    for info in sorted(pkgutil.iter_modules(tools_package.__path__), key=lambda i: i.name)
]
DECLARING = [m for m in MODULES if hasattr(m, "__all__")]


def _tools_defined_in(module) -> list:
    """The `@mcp.tool`-decorated functions this FILE defines.

    Read from source rather than from the module object on purpose: a name
    imported from a sibling is present at runtime but is not this module's to
    export, and reading `__dict__` cannot tell the two apart.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and any("mcp.tool" in ast.unparse(decorator) for decorator in node.decorator_list)
    ]


@pytest.mark.parametrize("module", DECLARING, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_tool_the_file_defines_is_missing_from_all(module):
    """One direction, deliberately.

    A tool absent from `__all__` disappears from the MCP surface silently, which
    is the failure this file exists to catch. The other direction is NOT an
    error: `__all__` is the module's public API, and several modules rightly
    publish helpers that are not tools - `events.register_incoming_handlers`
    wires up the feed, `events_store` exports four accessors for it. Demanding
    equality would fail those for being well-formed.
    """
    declared = set(module.__all__)
    defined = set(_tools_defined_in(module))

    assert defined <= declared, (
        f"{module.__name__} defines tools that __all__ does not export, so they are "
        f"invisible to every MCP client: {sorted(defined - declared)}"
    )


@pytest.mark.parametrize("module", DECLARING, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_everything_all_promises_is_actually_callable(module):
    for name in module.__all__:
        assert callable(getattr(module, name, None)), f"{module.__name__}.{name} is not callable"


def test_no_tool_is_defined_by_two_modules():
    """A split that copies a tool instead of moving it leaves two live
    definitions, and which one the package ends up with depends on the import
    order in `tools/__init__.py`."""
    owners = {}
    for module in MODULES:
        for name in _tools_defined_in(module):
            owners.setdefault(name, []).append(module.__name__.rsplit(".", 1)[-1])

    duplicated = {name: where for name, where in owners.items() if len(where) > 1}
    assert duplicated == {}, f"defined in more than one module: {duplicated}"


def test_no_module_name_is_shadowed_by_a_tool():
    """`import *` binds tool names into the package namespace, so a tool called
    `translate` would replace the submodule `translate`. That is why the module
    is named `translation.py` - this keeps the next one from repeating it."""
    tool_names = {name for module in MODULES for name in _tools_defined_in(module)}
    collisions = sorted(
        name for name in (m.__name__.rsplit(".", 1)[-1] for m in MODULES) if name in tool_names
    )

    assert collisions == [], f"module name shadowed by a tool of the same name: {collisions}"


def test_every_tool_the_package_defines_is_reachable_through_it():
    """The end of the chain: defined in a module, exported, and present on the
    package a client actually imports."""
    unreachable = [
        name
        for module in MODULES
        for name in _tools_defined_in(module)
        if not callable(getattr(tools_package, name, None))
    ]

    assert unreachable == [], f"defined but not reachable from telegram_mcp.tools: {unreachable}"
