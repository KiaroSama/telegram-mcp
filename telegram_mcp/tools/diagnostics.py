"""Server diagnostics: what is configured, and what to do when it is not.

Separate from `inspection.py`, which inspects *Telegram* — messages, media,
emoji. This module inspects the *server*, and the two answer different questions
for different people.
"""

from telegram_mcp import file_roots
from telegram_mcp.runtime import *

# What each status means to somebody trying to use `download_media`, and the one
# concrete thing that changes it. The status strings are file_roots' own; the
# advice is written here because file_roots must not know about tools.
#
# Phrased as sentences rather than error codes on purpose: this tool exists
# precisely because an agent that hits the roots gate cannot currently explain it,
# and an opaque status would reproduce the problem it was built to solve.
_ROOTS_ADVICE = {
    file_roots.ROOTS_STATUS_READY: (
        "File tools are enabled. Paths must resolve inside one of the roots listed below."
    ),
    file_roots.ROOTS_STATUS_NOT_CONFIGURED: (
        "File tools are disabled: no allowed root is configured. Either start the "
        "server with one or more directories as positional arguments, or configure "
        "roots in the MCP client so it answers `roots/list`."
    ),
    file_roots.ROOTS_STATUS_UNSUPPORTED_FALLBACK: (
        "This MCP client does not implement `roots/list`, so the server's own "
        "command-line roots are in use. That is the expected arrangement for a "
        "client without roots support."
    ),
    # The two fallback-dependent states are not in this map; see `_roots_advice`.
    # Their advice depends on whether the server HAS command-line roots, which a
    # status string cannot carry.
    file_roots.ROOTS_STATUS_SERVER_FALLBACK: (
        "The client's roots could not be used, and TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK "
        "is set, so the server's command-line roots are in use."
    ),
}


# Why the client's roots are unusable, for the two states where that is only half
# the answer. The other half - what to actually do - depends on the server's own
# configuration and is decided in `_roots_advice`.
_WHY_CLIENT_ROOTS_FAILED = {
    file_roots.ROOTS_STATUS_CLIENT_DENY_ALL: (
        "File tools are disabled because the MCP client answered `roots/list` with an "
        "empty list. An empty list means 'nothing is permitted' and is obeyed as "
        "written; it is a different state from a client that cannot answer at all."
    ),
    file_roots.ROOTS_STATUS_ERROR: (
        "File tools are disabled because the client's `roots/list` failed in a way the "
        "server could not recover."
    ),
}


def _roots_advice(status: str) -> str:
    """The one concrete thing to do next, given the FACTS as well as the status.

    A flat status-to-sentence map was wrong here in a specific and expensive way.
    Two of these states used to recommend the server's own command-line roots as
    the way out, unconditionally - and that is only a way out when the server
    actually has some. Started without any, which is this launcher's default, the
    advice read "no fallback is enabled, set
    TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1" even when that variable was already
    set, and following it meant a server restart that landed in exactly the same
    state with the real remedy still unmentioned.

    That is precisely the failure this tool exists to prevent, so the advice for
    those two states is computed rather than looked up.
    """
    if status not in _WHY_CLIENT_ROOTS_FAILED:
        return _ROOTS_ADVICE.get(status, f"Unrecognised roots status: {status}.")

    why = _WHY_CLIENT_ROOTS_FAILED[status]
    if not file_roots.SERVER_ALLOWED_ROOTS:
        return (
            f"{why} The server has no command-line roots either, so enabling the "
            "fallback would have nothing to fall back to. Restart the server with one "
            "or more directories as positional arguments, or fix the client's roots "
            "configuration."
        )
    if file_roots._server_roots_fallback_enabled():
        return (
            f"{why} The server does have command-line roots and the fallback is "
            "already enabled, so this state should not persist - check the server's "
            "startup log rather than changing configuration."
        )
    return (
        f"{why} Set TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1 to use the server's "
        "command-line roots instead, or fix the client's roots configuration."
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get File Roots Status", openWorldHint=False, readOnlyHint=True
    )
)
async def get_file_roots_status(ctx: Optional[Context] = None) -> str:
    """
    Report whether the file tools are usable, and exactly what to configure if not.

    `download_media` and `upload_file` refuse every path until an allowed root is
    configured. Without this tool an agent that hits that refusal cannot explain
    it or route around it, so it retries or gives up - even though the server
    already knows the answer.

    Returns the current status, the effective roots, which mechanism supplied
    them, and the concrete next step.

    Note: the roots are operator-configured, not sender-controlled, so nothing in
    this response is untrusted user content.
    """
    try:
        roots, status = await file_roots._get_effective_allowed_roots_with_status(ctx)
        return format_tool_result(
            {
                "file_tools_enabled": bool(roots),
                "status": status,
                "roots": [str(root) for root in roots],
                "source": (
                    "client"
                    if status == file_roots.ROOTS_STATUS_READY and ctx is not None
                    else "server_command_line" if roots else "none"
                ),
                "server_command_line_roots": [
                    str(root) for root in file_roots.SERVER_ALLOWED_ROOTS
                ],
                "server_fallback_allowed": file_roots._server_roots_fallback_enabled(),
                "next_step": _roots_advice(status),
            }
        )
    except Exception as e:
        return log_and_format_error("get_file_roots_status", e)
