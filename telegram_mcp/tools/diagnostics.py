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
    file_roots.ROOTS_STATUS_CLIENT_DENY_ALL: (
        "File tools are disabled because the MCP client answered `roots/list` with "
        "an empty list. An empty list means 'nothing is permitted' and is obeyed as "
        "written; it is a different state from a client that cannot answer at all. "
        "Add a root in the client, or set TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1 to "
        "let the server's command-line roots stand in."
    ),
    file_roots.ROOTS_STATUS_SERVER_FALLBACK: (
        "The client's roots could not be used, and TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK "
        "is set, so the server's command-line roots are in use."
    ),
    file_roots.ROOTS_STATUS_ERROR: (
        "The client's `roots/list` failed in a way the server could not recover, and "
        "no fallback is enabled, so file tools are disabled. Set "
        "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1 to use the server's command-line "
        "roots instead, or fix the client's roots configuration."
    ),
}


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
                "next_step": _ROOTS_ADVICE.get(status, f"Unrecognised roots status: {status}."),
            }
        )
    except Exception as e:
        return log_and_format_error("get_file_roots_status", e)
