"""What `import *` in ``tools/__init__.py`` does to the package namespace.

Registration works by side effect: importing a tool module runs its decorators. The
star import that triggers that also binds every tool NAME into ``telegram_mcp.tools``,
and this is the test for what that costs.

This file is what remains of ``test_merge_contract.py``. That file checked the fork
could still be merged with its upstream cleanly; the project no longer tracks one, so
those assertions were retired rather than left to rot into doc-maintenance busywork.
The check below was never about merging.
"""

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
