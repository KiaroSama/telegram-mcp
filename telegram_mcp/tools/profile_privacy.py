"""Who can see what: reading and writing Telegram's privacy rules.

Split from ``profile`` because it is a different subject with its own
vocabulary. Telegram expresses one setting as a base policy plus a list of
exceptions, and the four tables below are the whole translation layer between
that and the words a caller uses — which is why they belong next to the two
tools that read them and nowhere else.

Self-contained: it needs nothing from ``profile`` and ``profile`` needs nothing
from it. The re-export at the foot of that module only keeps the import path
callers already use resolving; a test that patches a seam here must patch THIS
module, per ``tests/conftest.py``.
"""

from telegram_mcp.runtime import *

# The privacy keys this server exposes, and the argument name each is reached
# by. Telegram has more; these are the three that were already supported.
_PRIVACY_KEYS = {
    "status": "InputPrivacyKeyStatusTimestamp",
    "phone": "InputPrivacyKeyPhoneNumber",
    "profile_photo": "InputPrivacyKeyProfilePhoto",
}

# account.setPrivacy REPLACES every rule for a key; there is no patch form. So a
# base policy is not optional -- omitting it just means the tool picks one, and
# the one it used to pick was "everyone".
_PRIVACY_BASE_POLICIES = {
    "everyone": "InputPrivacyValueAllowAll",
    "contacts": "InputPrivacyValueAllowContacts",
    "nobody": "InputPrivacyValueDisallowAll",
}

# How Telegram's answering rules read back out. Anything not listed is reported
# by its constructor name rather than guessed at.
_PRIVACY_RULE_NAMES = {
    "PrivacyValueAllowAll": "everyone_allowed",
    "PrivacyValueAllowContacts": "contacts_allowed",
    "PrivacyValueAllowCloseFriends": "close_friends_allowed",
    "PrivacyValueAllowPremium": "premium_allowed",
    "PrivacyValueDisallowAll": "everyone_disallowed",
    "PrivacyValueDisallowContacts": "contacts_disallowed",
    "PrivacyValueAllowUsers": "users_allowed",
    "PrivacyValueDisallowUsers": "users_disallowed",
    "PrivacyValueAllowChatParticipants": "chats_allowed",
    "PrivacyValueDisallowChatParticipants": "chats_disallowed",
    "PrivacyValueAllowBots": "bots_allowed",
    "PrivacyValueDisallowBots": "bots_disallowed",
}


# The same table read backwards. Every kind `get_privacy_settings` can REPORT
# has an InputPrivacyValue counterpart, so a full round trip is possible - and
# without this it was not: the writer built only allow/disallow-users plus a base
# policy, while `account.setPrivacy` REPLACES. Reading "Contacts, plus close
# friends, except Bob" and passing back what the tool could express silently
# deleted the close-friends rule, permanently and with no warning.
_PRIVACY_INPUT_RULES = {
    "everyone_allowed": "InputPrivacyValueAllowAll",
    "contacts_allowed": "InputPrivacyValueAllowContacts",
    "close_friends_allowed": "InputPrivacyValueAllowCloseFriends",
    "premium_allowed": "InputPrivacyValueAllowPremium",
    "everyone_disallowed": "InputPrivacyValueDisallowAll",
    "contacts_disallowed": "InputPrivacyValueDisallowContacts",
    "users_allowed": "InputPrivacyValueAllowUsers",
    "users_disallowed": "InputPrivacyValueDisallowUsers",
    "chats_allowed": "InputPrivacyValueAllowChatParticipants",
    "chats_disallowed": "InputPrivacyValueDisallowChatParticipants",
    "bots_allowed": "InputPrivacyValueAllowBots",
    "bots_disallowed": "InputPrivacyValueDisallowBots",
}


async def _rules_from_described(described, cl, telethon_utils, tl_types):
    """`(input_rules, error)` from what `get_privacy_settings` reported.

    Order is preserved because Telegram applies the first matching rule: moving a
    base policy ahead of its own exceptions makes it swallow them.
    """
    built = []
    for index, entry in enumerate(described or []):
        if not isinstance(entry, dict):
            return None, f"Error: rules[{index}] is not an object; no privacy rule was changed."
        name = entry.get("rule")
        class_name = _PRIVACY_INPUT_RULES.get(name)
        if class_name is None:
            return None, (
                f"Error: rules[{index}] names {name!r}, which is not a rule kind this "
                f"account can be given. Valid kinds: {', '.join(sorted(_PRIVACY_INPUT_RULES))}."
            )
        rule_class = getattr(tl_types, class_name)
        if name in ("users_allowed", "users_disallowed"):
            resolved = []
            for identifier in entry.get("users") or []:
                try:
                    entity = await resolve_entity(identifier, cl)
                    resolved.append(telethon_utils.get_input_user(entity))
                except Exception as error:
                    return None, (
                        f"Error: could not resolve {identifier!r} in rules[{index}] "
                        f"({type(error).__name__}); no privacy rule was changed."
                    )
            built.append(rule_class(users=resolved))
        elif name in ("chats_allowed", "chats_disallowed"):
            built.append(rule_class(chats=[int(c) for c in entry.get("chats") or []]))
        else:
            built.append(rule_class())
    if not built:
        return None, "Error: rules was empty; account.setPrivacy would clear every rule."
    return built, None


def _describe_privacy_rule(rule) -> dict:
    """One answering rule as data, rather than as `str(TLObject)`."""
    name = type(rule).__name__
    described = {"rule": _PRIVACY_RULE_NAMES.get(name, name)}
    for field in ("users", "chats"):
        ids = getattr(rule, field, None)
        if ids:
            described[field] = list(ids)
    return described


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Privacy Settings", openWorldHint=True, readOnlyHint=True
    )
)
@with_account(readonly=True)
async def get_privacy_settings(key: str = "status", account: str = None) -> str:
    """
    Read the privacy rules currently applied to one key.

    The rules come back in the order Telegram applies them, which is the order
    set_privacy_settings has to send them back in.

    Args:
        key: Which setting to read: 'status' (last seen), 'phone' or
            'profile_photo'. Defaults to 'status'.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)

        from telethon.tl import types as tl_types

        if key not in _PRIVACY_KEYS:
            return (
                f"Error: Unsupported privacy key '{key}'. Supported keys: "
                f"{', '.join(_PRIVACY_KEYS)}."
            )
        privacy_key = getattr(tl_types, _PRIVACY_KEYS[key])()

        try:
            settings = await cl(functions.account.GetPrivacyRequest(key=privacy_key))
        except TypeError as e:
            if "TLObject was expected" in str(e):
                return (
                    "Error: Privacy settings API call failed due to type mismatch. This is "
                    "likely a version compatibility issue with Telethon."
                )
            raise

        rules = [_describe_privacy_rule(rule) for rule in getattr(settings, "rules", None) or []]
        return format_tool_result(rules, {"key": key, "rule_count": len(rules)})
    except Exception as e:
        return log_and_format_error("get_privacy_settings", e, key=key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Privacy Settings", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("allow_users", "disallow_users")
async def set_privacy_settings(
    key: str,
    allow_users: Optional[List[Union[int, str]]] = None,
    disallow_users: Optional[List[Union[int, str]]] = None,
    base_policy: Optional[str] = None,
    rules: Optional[List[dict]] = None,
    account: str = None,
) -> str:
    """
    Replace the privacy rules for one key.

    This is a REPLACEMENT, not a patch: Telegram's account.setPrivacy discards
    whatever was there and applies exactly the rules sent. base_policy is
    therefore required -- with it omitted the tool would be choosing a policy on
    the caller's behalf, and the choice it used to make was "everyone".

    Read the current rules with get_privacy_settings first if the intent is to
    adjust rather than replace.

    Args:
        key: Which setting to change: 'status' (last seen), 'phone' or
            'profile_photo'.
        allow_users: Users allowed regardless of base_policy. Exceptions are sent
            ahead of the base rule, which is the order Telegram applies them in.
        disallow_users: Users denied regardless of base_policy.
        base_policy: Required unless `rules` is given. 'everyone', 'contacts' or
            'nobody' -- who the setting is visible to before the exception lists
            are applied.
        rules: The complete rule list, in exactly the shape `get_privacy_settings`
            returns it: `[{"rule": "close_friends_allowed"}, {"rule":
            "users_disallowed", "users": [123]}, ...]`, in the order Telegram
            applies them. Supplying this sends those rules verbatim and ignores
            the three arguments above.

            **This is the only way to adjust a setting without destroying part of
            it.** `allow_users`/`disallow_users`/`base_policy` can express three
            of the twelve rule kinds Telegram has; an account whose Last Seen is
            "contacts, plus close friends, except Bob" could be READ in full and
            not written back, so a round trip through this tool silently deleted
            the close-friends, premium, bot and chat-participant rules.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)

        from telethon import utils as telethon_utils
        from telethon.tl import types as tl_types

        if key not in _PRIVACY_KEYS:
            return (
                f"Error: Unsupported privacy key '{key}'. Supported keys: "
                f"{', '.join(_PRIVACY_KEYS)}."
            )
        if base_policy is None and not rules:
            return (
                "Error: base_policy is required. account.setPrivacy replaces the whole "
                f"rule set for '{key}', so leaving it out would silently pick one. Choose "
                f"{', '.join(_PRIVACY_BASE_POLICIES)}, or read the current rules with "
                "get_privacy_settings and pass them back explicitly."
            )
        policy = str(base_policy).strip().lower() if base_policy is not None else None
        if policy is not None and policy not in _PRIVACY_BASE_POLICIES:
            return (
                f"Error: Unknown base_policy '{base_policy}'. Valid values: "
                f"{', '.join(_PRIVACY_BASE_POLICIES)}."
            )

        allow_list = list(allow_users or [])
        disallow_list = list(disallow_users or [])
        overlap = [user for user in allow_list if user in disallow_list]
        if overlap:
            return (
                f"Error: {overlap} appear on both allow_users and disallow_users. Telegram "
                "applies the first matching rule, so the result would depend on ordering."
            )

        async def _input_users(identifiers):
            """Resolve to InputUser, or name the one that could not be resolved.

            Dropping an unresolvable name and sending the rest is fail-open: the
            caller asked for a rule that would then not exist.
            """
            resolved = []
            for identifier in identifiers:
                try:
                    entity = await resolve_entity(identifier, cl)
                    # InputPrivacyValue*Users takes a vector of InputUser. A
                    # resolved User is a different constructor and does not
                    # serialise into that vector.
                    resolved.append(telethon_utils.get_input_user(entity))
                except Exception as error:
                    return None, (
                        f"Error: could not resolve '{identifier}' to a user "
                        f"({type(error).__name__}); no privacy rule was changed."
                    )
            return resolved, None

        if rules:
            built, error = await _rules_from_described(rules, cl, telethon_utils, tl_types)
            if error:
                return error
            await cl(
                functions.account.SetPrivacyRequest(
                    key=getattr(tl_types, _PRIVACY_KEYS[key])(), rules=built
                )
            )
            return format_tool_result(
                [{"key": key, "rules": [r.get("rule") for r in rules]}],
                {"replaced": True, "round_tripped": True},
            )

        rules = []
        if allow_list:
            users, error = await _input_users(allow_list)
            if error:
                return error
            rules.append(tl_types.InputPrivacyValueAllowUsers(users=users))
        if disallow_list:
            users, error = await _input_users(disallow_list)
            if error:
                return error
            rules.append(tl_types.InputPrivacyValueDisallowUsers(users=users))
        # Last: Telegram applies the rules in order, so a base policy placed ahead
        # of its own exceptions would match first and swallow them.
        rules.append(getattr(tl_types, _PRIVACY_BASE_POLICIES[policy])())

        try:
            await cl(
                functions.account.SetPrivacyRequest(
                    key=getattr(tl_types, _PRIVACY_KEYS[key])(), rules=rules
                )
            )
        except TypeError as type_err:
            if "TLObject was expected" in str(type_err):
                return (
                    "Error: Privacy settings API call failed due to type mismatch. This is "
                    "likely a version compatibility issue with Telethon."
                )
            raise

        return format_tool_result(
            [
                {
                    "key": key,
                    "base_policy": policy,
                    "allowed_count": len(allow_list),
                    "disallowed_count": len(disallow_list),
                }
            ],
            {"replaced": True},
        )
    except Exception as e:
        return log_and_format_error("set_privacy_settings", e, key=key)
