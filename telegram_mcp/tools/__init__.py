"""Import tool modules so their MCP decorators register with the shared server."""

from telegram_mcp.tools.accounts import *
from telegram_mcp.tools.contacts import *
from telegram_mcp.tools.contact_aliases import *
from telegram_mcp.tools.chats import *
from telegram_mcp.tools.topics import *
from telegram_mcp.tools.chat_state import *
from telegram_mcp.tools.messages import *
from telegram_mcp.tools.messages_read import *
from telegram_mcp.tools.messages_state import *
from telegram_mcp.tools.messages_queue import *
from telegram_mcp.tools.groups import *
from telegram_mcp.tools.moderation import *
from telegram_mcp.tools.invites import *
from telegram_mcp.tools.media import *
from telegram_mcp.tools.profile import *
from telegram_mcp.tools.photos import *
from telegram_mcp.tools.folders import *
from telegram_mcp.tools.events import *
from telegram_mcp.tools.diagnostics import *
from telegram_mcp.tools.channel_settings import *

# Visual and deep structured access. Kept last because these layer on top of the
# modules above; the grouping is historic (they were once the only fork-authored
# files) but the ordering still reflects the dependency direction.
from telegram_mcp.tools.inspection import *
from telegram_mcp.tools.visual import *
from telegram_mcp.tools.effects import *
from telegram_mcp.tools.buttons import *
from telegram_mcp.tools.scheduled import *
from telegram_mcp.tools.ephemeral import *
from telegram_mcp.tools.secret_chats import *
from telegram_mcp.tools.later_rights import *
from telegram_mcp.tools.rich_messages import *
from telegram_mcp.tools.mini_apps import *
from telegram_mcp.tools.polls import *
from telegram_mcp.tools.stories import *
from telegram_mcp.tools.channel_admin import *
from telegram_mcp.tools.saved import *
from telegram_mcp.tools.stickers import *

# translation.py, not translate.py: `import *` binds the tool name `translate`
# into this package, which would otherwise shadow the submodule of the same name.
from telegram_mcp.tools.translation import *

__all__ = [name for name in globals() if not name.startswith("_")]
