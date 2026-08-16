"""Import tool modules so their MCP decorators register with the shared server."""

from telegram_mcp.tools.accounts import *
from telegram_mcp.tools.contacts import *
from telegram_mcp.tools.chats import *
from telegram_mcp.tools.messages import *
from telegram_mcp.tools.groups import *
from telegram_mcp.tools.media import *
from telegram_mcp.tools.profile import *
from telegram_mcp.tools.folders import *
from telegram_mcp.tools.events import *

# Fork additions (visual + deep structured access). Kept last and in their own
# modules so upstream merges touch only these four lines.
from telegram_mcp.tools.inspection import *
from telegram_mcp.tools.visual import *
from telegram_mcp.tools.effects import *
from telegram_mcp.tools.buttons import *

__all__ = [name for name in globals() if not name.startswith("_")]
