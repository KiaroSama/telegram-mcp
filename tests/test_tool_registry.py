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
