"""Media MCP tools."""

from contextlib import AsyncExitStack

from telegram_mcp import media_album, media_send, ogg_tags, video_dims
from telegram_mcp.paging import LIMITS, bounded
from telegram_mcp.runtime import *
from telegram_mcp.forum import topic_reply_to
from telegram_mcp.handles import NAME_ATTEMPTS
from telegram_mcp.sent import sent_message_ids

# What one download_media call may write before it is stopped. Telegram files run
# to 2GB (4GB for premium), and the tool had no ceiling at all: a single call
# could fill the disk. This matches the send_file ceiling the project already
# chose for itself, and the caller can raise it per call with max_bytes.
_DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024


def _sent_result(sent, chat_id, note: str) -> str:
    """The confirmation, plus the id everything a caller does next needs.

    `note` survives as `detail` so an existing reader still sees which file went
    where; the ids are what makes the message addressable at all. A send whose
    receipt has no id still reports success - it happened.
    """
    ids = sent_message_ids(sent)
    if not ids:
        return note
    return format_tool_result(
        [{"message_id": ident, "chat_id": str(chat_id)} for ident in ids],
        {"sent": True, "detail": note},
    )


class _DownloadTooLarge(Exception):
    """Raised out of the progress callback to stop an over-cap stream mid-flight."""


def _peek_tail(handle) -> bytes:
    """The END of an open upload, with the position put back.

    An MP4 written without faststart keeps its `moov` box - and so its real size
    and duration - at the end of the file, which is most of what a phone records.
    Reading only the head would have fixed the faststart case and quietly left
    every other video at 1x1.
    """
    try:
        position = handle.tell()
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - video_dims.TAIL_BYTES))
        tail = handle.read()
        handle.seek(position)
        return tail
    except (AttributeError, OSError, ValueError):
        return b""


def _peek(handle) -> bytes:
    """The head of an open upload, with the position put back where it was.

    `resolve_kind` reads it to tell a voice note from a music file, which is the
    one question an `.ogg`'s name cannot answer. Seeking back is the whole risk
    here: Telethon uploads from wherever the pointer is left, so a peek that
    forgets to rewind silently truncates the file it was inspecting.

    A handle that cannot seek gets no peek and no exception - the caller then
    falls back to the extension, exactly as before this existed.
    """
    try:
        position = handle.tell()
        head = handle.read(max(ogg_tags.HEADER_BYTES, video_dims.HEAD_BYTES))
        handle.seek(position)
        return head
    except (AttributeError, OSError, ValueError):
        return b""


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send File",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id", "send_as")
async def send_file(
    chat_id: Union[int, str],
    file_path: Union[str, List[str]],
    caption: str = None,
    kind: Optional[Union[str, List[str]]] = None,
    caption_entities: Optional[List[dict]] = None,
    topic_id: Optional[int] = None,
    send_as: Optional[Union[int, str]] = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Send a file to a chat, in any of the eight shapes Telegram gives one.
    Args:
        chat_id: The chat ID or username.
        file_path: Absolute or relative path to the file under allowed roots.
            Pass a list of 2-10 paths to send them as one Telegram media group.
        caption: Optional caption for the file or media group.
        caption_entities: Formatting for the caption, in the shape
            `inspect_message` returns. This is the ONLY way to put a premium
            emoji in a caption - `parse_mode` has no syntax for one - and it
            travels WITH the send, so the message is never marked "edited" the
            way a send-then-edit leaves it. The offsets are UTF-16 units into
            the `text_fidelity` value the entities came with, not the display
            `text`.
        kind: How the file should ARRIVE, rather than what its bytes are - the
            same recording is `audio` (a track with a play button) or
            `voice_note` (a waveform) purely by what is asked for here, and the
            same clip is `video`, `video_note` (round) or `animation`. One of
            photo, video, document, audio, animation, sticker, video_note,
            voice_note. `document` is "send as file" and takes anything. Leave
            unset to choose from the extension; the reply says which kind went.
            A kind the file cannot be is refused before anything is uploaded,
            and nothing is ever converted to fit one. `sticker` and `video_note`
            carry no caption, so one given with either is refused rather than
            dropped in transit.
            With a list of paths, pass one name for all of them or a list of the
            same length to name each. A list of the wrong length is refused.
        Splitting: `force_document` belongs to the Telegram media group, not to a
            file inside it, so entries that cannot share a group are sent as
            separate messages, in the order given - a photo beside a document is
            two messages, and a voice note, video note or sticker is always its
            own. The reply names every message and the kind each carried, and the
            caption rides the first one, as Telegram shows an album's caption on
            its first item.
        topic_id: Optional forum topic ID (from list_topics). Sends into that topic
            in a forum-enabled community/supergroup. Also works as reply_to for a message.
        send_as: Post under a channel's identity rather than your own. The value
            comes from `list_send_as` for THIS chat - Telegram decides which are
            legal there and refuses anything else.
    """
    try:
        if isinstance(file_path, list):
            return await _send_album(
                chat_id=chat_id,
                file_paths=file_path,
                caption=caption,
                kind=kind,
                caption_entities=caption_entities,
                topic_id=topic_id,
                send_as=send_as,
                ctx=ctx,
                account=account,
            )

        cl = get_client(account)
        async with _open_verified_source(raw_path=file_path, ctx=ctx, tool_name="send_file") as (
            source,
            path_error,
        ):
            if path_error:
                return path_error
            # Before the entity is resolved and before a byte moves: an
            # impossible kind costs nothing to refuse here and an upload to
            # refuse at Telegram, which names neither the file nor the kind.
            head = _peek(source.handle)
            sending_as = media_send.resolve_kind(
                source.path.name, kind, caption or "", header=head
            )
            entity = await resolve_entity(chat_id, cl)
            posting_as = await resolve_entity(send_as, cl) if send_as else None
            sent = await cl.send_file(
                entity,
                source.handle,
                caption=caption,
                reply_to=topic_reply_to(topic_id),
                # Omitted entirely when unused: passing `send_as=None` changes the
                # call every existing caller makes, and an unused feature that
                # alters the call is not unused.
                **({"send_as": posting_as} if posting_as is not None else {}),
                **await media_send.caption_flags(caption_entities, caption, account),
                **media_send.flags_for(
                    sending_as, head, source.path.name, _peek_tail(source.handle)
                ),
            )
            return _sent_result(
                sent, chat_id, f"File sent to chat {chat_id} from {source.path} as {sending_as}."
            )
    except media_send.MediaKindError as refusal:
        # Handed back verbatim. It already names the file and the kind, which is
        # the whole point of refusing here rather than letting Telegram refuse
        # after the upload with a message that names neither.
        return str(refusal)
    except Exception as e:
        return log_and_format_error(
            "send_file",
            e,
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            topic_id=topic_id,
        )


async def _send_album(
    chat_id: Union[int, str],
    file_paths: List[str],
    caption: str = None,
    kind: Optional[Union[str, List[str]]] = None,
    caption_entities: Optional[List[dict]] = None,
    topic_id: Optional[int] = None,
    send_as: Optional[Union[int, str]] = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    if not 2 <= len(file_paths) <= 10:
        return "Albums must contain between 2 and 10 files."
    # A list applies pairwise; one name applies to all. A list of the wrong
    # length is refused rather than zipped short, which would silently send the
    # tail as something nobody asked for.
    kinds_asked = kind if isinstance(kind, list) else [kind] * len(file_paths)
    if len(kinds_asked) != len(file_paths):
        return (
            f"kind has {len(kinds_asked)} entries for {len(file_paths)} files. "
            "Pass one name for all of them, a list the same length, or nothing "
            "at all to choose from each file. Nothing was sent."
        )

    cl = get_client(account)
    # Every member stays open for the whole upload: an album authorised one
    # name at a time and then re-read by Telethon is the same defect N times.
    async with AsyncExitStack() as stack:
        sources, names, kinds, headers = [], [], [], []
        for file_path, asked in zip(file_paths, kinds_asked):
            source, path_error = await stack.enter_async_context(
                _open_verified_source(raw_path=file_path, ctx=ctx, tool_name="send_file")
            )
            if path_error:
                return path_error
            sources.append(source.handle)
            names.append(source.path.name)
            # Before the entity is resolved and before a byte moves, for every
            # member: one impossible kind must not leave the others sent.
            head = _peek(source.handle)
            headers.append(head)
            kinds.append(
                media_send.resolve_kind(
                    source.path.name,
                    asked,
                    caption or "",
                    header=head,
                    tail=_peek_tail(source.handle),
                )
            )

        entity = await resolve_entity(chat_id, cl)
        posting_as = await resolve_entity(send_as, cl) if send_as else None
        receipts, plan = await media_album.send_planned(
            client=cl,
            entity=entity,
            sources=sources,
            kinds=kinds,
            headers=headers,
            names=names,
            caption=caption,
            caption_flags=await media_send.caption_flags(caption_entities, caption, account),
            reply_to=topic_reply_to(topic_id),
            posting_as=posting_as,
        )
        return _sent_result(receipts, chat_id, media_album.describe(chat_id, names, kinds, plan))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Album",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def send_album(
    chat_id: Union[int, str],
    file_paths: List[str],
    caption: str = None,
    topic_id: Optional[int] = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Send multiple photos/videos as one Telegram media group (album).

    Args:
        chat_id: The chat ID or username.
        file_paths: 2-10 absolute or relative file paths under allowed roots.
        caption: Optional caption for the album. Telegram displays it on the first item.
        topic_id: Optional forum topic ID (from list_topics). Sends into that topic
            in a forum-enabled community/supergroup. Also works as reply_to for a message.
    """
    try:
        if not isinstance(file_paths, list):
            return "file_paths must be a list of file paths."
        return await _send_album(
            chat_id=chat_id,
            file_paths=file_paths,
            caption=caption,
            topic_id=topic_id,
            ctx=ctx,
            account=account,
        )
    except media_send.MediaKindError as refusal:
        return str(refusal)
    except Exception as e:
        return log_and_format_error(
            "send_album",
            e,
            chat_id=chat_id,
            file_paths=file_paths,
            caption=caption,
            topic_id=topic_id,
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Download Media",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=True,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def download_media(
    chat_id: Union[int, str],
    message_id: int,
    file_path: Optional[str] = None,
    max_bytes: Optional[int] = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Download media from a message in a chat.

    The transfer runs inside a private directory this call creates for itself
    under the resolved destination, and the file is moved into place only once it
    has finished, been size-checked and been flushed to storage. A failure, a
    cancellation or an over-cap stream therefore leaves nothing behind, and an
    existing file is never overwritten -- a colliding name gets a `-1`, `-2`
    suffix.

    Args:
        chat_id: The chat ID or username.
        message_id: The message ID containing the media.
        file_path: Optional absolute or relative path under allowed roots.
            If omitted, saves into `<first_root>/downloads/`.
        max_bytes: Ceiling for this download, in bytes. Defaults to 200 MB.
            Telegram advertises the size up front, so an oversized file is
            refused before anything is fetched; a stream that outgrows the cap
            anyway is stopped mid-flight.
    """
    try:
        # A ceiling that is zero, negative or not a number is not a smaller
        # ceiling, it is a broken one: zero used to be falsy enough to fall
        # through to the default, and a negative one became a cap nothing could
        # satisfy. Both are argument errors and are answered as such.
        if max_bytes is None:
            cap = _DOWNLOAD_MAX_BYTES
        else:
            try:
                cap = int(max_bytes)
            except (TypeError, ValueError):
                return "max_bytes must be a whole number of bytes."
            if cap <= 0:
                return f"max_bytes must be a positive number of bytes, not {cap}."

        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        msg = await cl.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return "No media found in the specified message."

        advertised = getattr(getattr(msg, "file", None), "size", None)
        if advertised and advertised > cap:
            return (
                f"Download refused: the media is {advertised} bytes, over the "
                f"{cap}-byte limit. Raise max_bytes to fetch it anyway."
            )

        default_name = f"telegram_{chat_id}_{message_id}_{int(time.time())}"
        out_path, path_error = await _resolve_writable_file_path(
            raw_path=file_path,
            default_filename=default_name,
            ctx=ctx,
            tool_name="download_media",
        )
        if path_error:
            return path_error

        # The destination directory is OPENED before a single byte is fetched,
        # and every step after this -- staging, size check, install, cleanup --
        # goes through that handle. Re-resolving the name at each step is what
        # let a directory swapped mid-transfer redirect the finished file.
        async with _open_verified_directory(
            path=out_path.parent, ctx=ctx, tool_name="download_media"
        ) as (parent, dir_error):
            if dir_error:
                return dir_error

            # The transfer gets a directory of its own, created through the held
            # parent. mkdir is the reservation -- it fails on a name that exists
            # -- and the result is opened straight away, so nothing downstream
            # resolves the name again.
            _staging_name, staging = parent.make_private_subdirectory(".download-")

            try:
                # Telethon picks the extension from the content, so it is handed
                # a STEM: passing ticket.jpg for a PDF would write a PDF called
                # ticket.jpg.
                temp_stem = Path(staging.path) / "part"

                def _stop_at_cap(received, _total):
                    if received > cap:
                        raise _DownloadTooLarge(
                            f"Download aborted: the stream passed the {cap}-byte limit "
                            "(max_bytes). Nothing was kept."
                        )

                downloaded = await cl.download_media(
                    msg, file=str(temp_stem), progress_callback=_stop_at_cap
                )
                if not downloaded:
                    return f"Download failed for message {message_id}."

                produced = Path(downloaded)
                if produced.parent.resolve(strict=False) != Path(staging.path).resolve(
                    strict=False
                ):
                    return (
                        "Download refused: the transfer wrote outside the directory "
                        "this call created for it."
                    )

                # Opened through the staging handle and measured with fstat: the
                # size that decides the refusal is the size of the object being
                # installed, not of whatever answers to its name a moment later.
                with open_verified_file(staging, produced.name) as fetched:
                    if fetched.size > cap:
                        return (
                            f"Download refused: the file turned out to be larger than the "
                            f"{cap}-byte limit (max_bytes). Nothing was kept."
                        )
                    # Which object passed, not just that one did. Telethon wrote
                    # this file through a PATHNAME, so between the check above and
                    # the install below the name can be given to something else -
                    # and the install would publish that instead.
                    staged = fetched.identity
                # The bytes reach storage before the name that promises them does.
                staging.sync_child(produced.name)

                # The suffix comes from the SENDER's mime type, and this file
                # lands in a directory the operator opened for downloads. The
                # bytes were always untrusted; the extension is what decides
                # whether opening the result runs it. save_disappearing_media
                # has had this guard since it was written; this path did not.
                safe = safe_suffix(produced.suffix)
                final = out_path.with_suffix(safe)
                final_name = parent.reserve_free_name(final.stem, final.suffix)
                if final_name is None:
                    return (
                        f"Download refused: {NAME_ATTEMPTS} names near "
                        f"{out_path.name} are already taken. Pass file_path to choose one."
                    )

                # Replace over the reserved placeholder, both ends bound to a held
                # directory: this can neither clobber a file that appeared in the
                # meantime nor publish into a directory that took over the name.
                try:
                    parent.install(staging, produced.name, final_name, expect_source=staged)
                except BaseException:
                    # The reservation is a real, empty file. Leaving it behind
                    # wearing the name the caller was about to be given is the
                    # defect this whole path exists to avoid. `discard` rather
                    # than `unlink`: if the install refused because that name
                    # stopped being this call's placeholder, removing it is the
                    # same mistake again, and the original failure is the one
                    # worth reporting.
                    parent.discard(final_name)
                    raise
                parent.sync()
                return f"Media downloaded to {Path(parent.path) / final_name}."
            finally:
                # Every exit -- success, refusal, exception, or the CancelledError
                # that is a BaseException and never reaches the handler below --
                # takes the whole transfer directory with it, through the handle
                # this call opened rather than through its name. A tree rather
                # than a name because Telethon chooses the extension, so on a
                # failure the only thing known about the partial file is which
                # directory it is in.
                try:
                    staging.remove_tree()
                    # Through its own handle, not by the name it was given: a
                    # name can have changed hands, and this is a removal.
                    staging.remove_self()
                except (OSError, UnsafeTarget) as cleanup_error:
                    log_event(
                        logging.WARNING,
                        "could not remove the download directory",
                        error=cleanup_error,
                    )
                finally:
                    staging.close()
    except _DownloadTooLarge as e:
        # The caller's own limit, not a fault: say which one and how to raise it,
        # rather than burying it in a generic error code.
        return str(e)
    except Exception as e:
        return log_and_format_error(
            "download_media",
            e,
            chat_id=chat_id,
            message_id=message_id,
            file_path=file_path,
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Voice",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def send_voice(
    chat_id: Union[int, str],
    file_path: str,
    topic_id: Optional[int] = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Send a voice message to a chat. File must be an OGG/OPUS voice note.

    Args:
        chat_id: The chat ID or username.
        file_path: Absolute or relative path under allowed roots to the OGG/OPUS file.
        topic_id: Optional forum topic ID (from list_topics). Sends into that topic
            in a forum-enabled community/supergroup. Also works as reply_to for a message.
    """
    try:
        cl = get_client(account)
        async with _open_verified_source(raw_path=file_path, ctx=ctx, tool_name="send_voice") as (
            source,
            path_error,
        ):
            if path_error:
                return path_error

            # No extension check here: `EXTENSION_ALLOWLISTS["send_voice"]` in
            # file_roots refuses anything but .ogg/.opus at the gate, before the
            # handle is opened, so the check this function used to make could
            # never fire. The allow-list IS the promise the docstring makes.
            entity = await resolve_entity(chat_id, cl)
            sent = await cl.send_file(
                entity,
                source.handle,
                reply_to=topic_reply_to(topic_id),
                # One place decides what a voice note is on the wire. This tool
                # is a route into that decision, not a second copy of it.
                **media_send.flags_for("voice_note"),
            )
            return _sent_result(
                sent, chat_id, f"Voice message sent to chat {chat_id} from {source.path}."
            )
    except Exception as e:
        return log_and_format_error(
            "send_voice", e, chat_id=chat_id, file_path=file_path, topic_id=topic_id
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Upload File",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
async def upload_file(file_path: str, ctx: Optional[Context] = None, account: str = None) -> str:
    """
    Upload a local file to Telegram and return upload metadata.

    Args:
        file_path: Absolute or relative path under allowed roots.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        async with _open_verified_source(raw_path=file_path, ctx=ctx, tool_name="upload_file") as (
            source,
            path_error,
        ):
            if path_error:
                return path_error

            uploaded = await cl.upload_file(source.handle)
            payload = {
                "path": str(source.path),
                "name": getattr(uploaded, "name", source.name),
                # The size the open handle reported, which is the size that
                # was authorised -- not a second stat of a name that has been
                # free to become a different file ever since.
                "size": getattr(uploaded, "size", source.size),
                "md5_checksum": getattr(uploaded, "md5_checksum", None),
            }
            return json.dumps(payload, indent=2, default=json_serializer)
    except Exception as e:
        return log_and_format_error("upload_file", e, file_path=file_path)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Media Info",
        openWorldHint=True,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_media_info(chat_id: Union[int, str], message_id: int, account: str = None) -> str:
    """
    Get info about media in a message.

    Args:
        chat_id: The chat ID or username.
        message_id: The message ID.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        msg = await cl.get_messages(entity, ids=message_id)

        if not msg or not msg.media:
            return "No media found in the specified message."

        # This used to return Telethon's pretty-printed debug dump of the media
        # object, which carried the sender's web-page title, document filename and
        # sticker alt with no cleaning and no length bound, as prose rather than
        # inside the envelope that marks a value as untrusted data.
        try:
            return format_tool_result([sanitize_dict(msg.media.to_dict())])
        except Exception as render_error:
            return (
                f"Could not render the {type(msg.media).__name__} in message "
                f"{message_id} as structured data: {render_error}"
            )
    except Exception as e:
        return log_and_format_error("get_media_info", e, chat_id=chat_id, message_id=message_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Sticker",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def send_sticker(
    chat_id: Union[int, str],
    file_path: str,
    topic_id: Optional[int] = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Send a sticker to a chat. File must be a valid .webp sticker file.

    Args:
        chat_id: The chat ID or username.
        file_path: Absolute or relative path under allowed roots to the .webp sticker file.
        topic_id: Optional forum topic ID (from list_topics). Sends into that topic
            in a forum-enabled community/supergroup. Also works as reply_to for a message.
    """
    try:
        cl = get_client(account)
        async with _open_verified_source(
            raw_path=file_path, ctx=ctx, tool_name="send_sticker"
        ) as (source, path_error):
            if path_error:
                return path_error

            entity = await resolve_entity(chat_id, cl)
            sent = await cl.send_file(
                entity,
                source.handle,
                reply_to=topic_reply_to(topic_id),
                **media_send.flags_for("sticker"),
            )
            return _sent_result(
                sent, chat_id, f"Sticker sent to chat {chat_id} from {source.path}."
            )
    except Exception as e:
        return log_and_format_error(
            "send_sticker", e, chat_id=chat_id, file_path=file_path, topic_id=topic_id
        )


# The inline bot Telegram's own clients query for GIFs.


__all__ = [
    "send_file",
    "send_album",
    "download_media",
    "send_voice",
    "upload_file",
    "get_media_info",
    "send_sticker",
]
