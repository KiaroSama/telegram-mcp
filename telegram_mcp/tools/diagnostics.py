"""Server diagnostics: what is configured, and what to do when it is not.

Separate from `inspection.py`, which inspects *Telegram* — messages, media,
emoji. This module inspects the *server*, and the two answer different questions
for different people.
"""

from telegram_mcp import file_roots
from telegram_mcp.runtime import *

__all__ = [
    "get_file_roots_status",
]


# One rule, stated once (FR-030..FR-034); the lists below are the facts of this machine.
_FOLDER_RULE = (
    "Send files from files/outbox and save them to files/downloads (relative paths start "
    "in files/) - no approval needed. Folders you configured on this machine "
    "(TELEGRAM_FILE_ROOTS or --allowed-root) and folders granted 'always allow' are free "
    "too. Any other folder, the MCP client's own roots included, is allowed only after "
    "the owner answers allow / deny / always allow. The rest of the installation and the "
    "server's state directory are never reachable."
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get File Roots Status",
        openWorldHint=False,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)
async def get_file_roots_status(ctx: Optional[Context] = None) -> str:
    """
    Report which folders the file tools may use, and how another one is allowed.

    `download_media`, `send_file` and the other file tools use `files/outbox` and
    `files/downloads` in the installation freely; any other folder needs the owner's
    approval through the safeguard. This tool lists the folders usable right now so
    an agent can pick one instead of guessing.

    Note: the folders are the owner's configuration, not sender-controlled, so
    nothing in this response is untrusted user content.
    """
    try:
        from telegram_mcp.safeguard import folders, grants

        roots, status = await file_roots._get_effective_allowed_roots_with_status(ctx)
        return format_tool_result(
            {
                "file_tools_enabled": bool(roots),
                "status": status,
                "roots": [str(root) for root in roots],
                "free_folders": [str(folder) for folder in folders.free_folders()],
                "configured_folders": [str(root) for root in file_roots.SERVER_ALLOWED_ROOTS],
                "always_allowed_folders": grants.list_folders(),
                "rule": _FOLDER_RULE,
            }
        )
    except Exception as e:
        return log_and_format_error("get_file_roots_status", e)
