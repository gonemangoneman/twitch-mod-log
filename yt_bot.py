import asyncio
import io
import json
import logging
import os
import re
from collections import defaultdict, deque

import aiohttp
from PIL import Image, ImageDraw, ImageFont

YT_CHANNEL_ID = os.environ["YT_CHANNEL_ID"]
YT_API_KEY = os.environ["YT_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_WEBHOOK_PLAIN_URL = os.environ.get("DISCORD_WEBHOOK_PLAIN_URL", "")
MAX_MESSAGES = 10
# Check for a new live stream every 5 minutes. RSS is free (no quota), so this is safe.
CHECK_STREAM_INTERVAL = 300

FONT_BOLD = "/usr/share/fonts/truetype/inter/Inter-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/inter/Inter-Regular.ttf"
FONT_ITALIC = "/usr/share/fonts/truetype/inter/Inter-Italic.ttf"
FONT_SIZE = 15
IMG_WIDTH = 560
PADDING = 18
LINE_HEIGHT = 24
BG = (24, 24, 27)
FG = (239, 239, 241)
MUTED = (114, 114, 120)
YT_RED = 0xFF0000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

message_buffer: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_MESSAGES))
message_cache: dict[str, tuple[str, str]] = {}
display_names: dict[str, str] = {}


def int_to_rgb(color: int) -> tuple[int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        d = seconds // 86400
        return f"{d} day{'s' if d != 1 else ''}"
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{seconds}s"


def load_fonts():
    return (
        ImageFont.truetype(FONT_BOLD, FONT_SIZE),
        ImageFont.truetype(FONT_REGULAR, FONT_SIZE),
        ImageFont.truetype(FONT_ITALIC, FONT_SIZE),
    )


def wrap_text(draw, text, font, max_width):
    if not text:
        return [""]
    words = text.split(" ")
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            original_word = word
            while word and draw.textlength(word + "…", font=font) > max_width:
                word = word[:-1]
            current = word + "…" if len(word) < len(original_word) else word
    if current:
        lines.append(current)
    return lines or [""]


def measure_entry(draw, fonts, name, message, action=None):
    font_bold, font_regular, font_italic = fonts
    uname_w = int(draw.textlength(name + ": ", font=font_bold))
    first_avail = IMG_WIDTH - PADDING * 2 - uname_w
    msg_lines = wrap_text(draw, message, font_regular, first_avail)
    n = len(msg_lines)
    if action:
        last_w = int(draw.textlength(msg_lines[-1], font=font_regular))
        action_w = int(draw.textlength(action, font=font_italic))
        avail = first_avail if n == 1 else IMG_WIDTH - PADDING * 2
        if last_w + action_w > avail:
            n += 1
    return n * LINE_HEIGHT


def draw_entry(draw, fonts, y, name, uname_color, message, msg_color, action=None):
    font_bold, font_regular, font_italic = fonts
    uname_str = name + ": "
    uname_w = int(draw.textlength(uname_str, font=font_bold))
    first_avail = IMG_WIDTH - PADDING * 2 - uname_w
    msg_lines = wrap_text(draw, message, font_regular, first_avail)

    draw.text((PADDING, y), uname_str, font=font_bold, fill=uname_color)
    draw.text((PADDING + uname_w, y), msg_lines[0], font=font_regular, fill=msg_color)
    y += LINE_HEIGHT

    for line in msg_lines[1:]:
        draw.text((PADDING, y), line, font=font_regular, fill=msg_color)
        y += LINE_HEIGHT

    if action:
        last_w = int(draw.textlength(msg_lines[-1], font=font_regular))
        action_w = int(draw.textlength(action, font=font_italic))
        avail = first_avail if len(msg_lines) == 1 else IMG_WIDTH - PADDING * 2
        if last_w + action_w <= avail:
            y -= LINE_HEIGHT
            ax = (PADDING + uname_w if len(msg_lines) == 1 else PADDING) + last_w
            draw.text((ax, y), action, font=font_italic, fill=MUTED)
            y += LINE_HEIGHT
        else:
            draw.text((PADDING, y), action, font=font_italic, fill=MUTED)
            y += LINE_HEIGHT

    return y


def render_image(name: str, entries: list[tuple], fonts, color: int) -> bytes:
    dummy = Image.new("RGB", (IMG_WIDTH, 1))
    dummy_draw = ImageDraw.Draw(dummy)
    total_height = PADDING * 2
    for msg, _, action in entries:
        total_height += measure_entry(dummy_draw, fonts, name, msg, action)

    img = Image.new("RGB", (IMG_WIDTH, total_height), BG)
    draw = ImageDraw.Draw(img)
    y = PADDING
    for msg, msg_color, action in entries:
        y = draw_entry(draw, fonts, y, name, int_to_rgb(color), msg, msg_color, action)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def build_image(author_id: str, name: str, fonts, all_muted: bool, action_label: str, deleted_text: str = None) -> bytes:
    msgs = list(message_buffer.get(author_id, []))

    if deleted_text and deleted_text in msgs:
        msgs.remove(deleted_text)

    if not msgs and not deleted_text:
        return render_image(name, [("(no messages recorded)", MUTED, f"—{action_label}")], fonts, YT_RED)

    entries = []
    if all_muted:
        for i, msg in enumerate(msgs):
            action = f"—{action_label}" if i == len(msgs) - 1 else None
            entries.append((msg, MUTED, action))
    else:
        for msg in msgs:
            entries.append((msg, FG, None))
        if deleted_text:
            entries.append((deleted_text, MUTED, f"—{action_label}"))

    return render_image(name, entries, fonts, YT_RED)


def _action_emoji(label: str) -> str:
    if "Ban" in label:
        return "🔨"
    if "Timed out" in label:
        return "⏱️"
    if "Deleted" in label:
        return "🗑️"
    return "⚠️"


async def send_discord_image(
    session: aiohttp.ClientSession,
    image_bytes: bytes,
    author_id: str,
    name: str,
    action_label: str,
    deleted_text: str | None = None,
) -> None:
    msg_url = None
    try:
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({}), content_type="application/json")
        form.add_field("files[0]", image_bytes, filename="modaction.png", content_type="image/png")
        async with session.post(DISCORD_WEBHOOK_URL + "?wait=true", data=form) as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                log.warning("Discord webhook returned %s: %s", resp.status, body)
            elif resp.status == 200:
                try:
                    data = await resp.json()
                    channel_id = data.get("channel_id", "")
                    msg_id = data.get("id", "")
                    if channel_id and msg_id:
                        msg_url = f"https://discord.com/channels/@me/{channel_id}/{msg_id}"
                except Exception:
                    pass
    except Exception as e:
        log.error("Failed to send Discord image: %s", e)

    # Post plain-text notification to the second webhook
    if DISCORD_WEBHOOK_PLAIN_URL:
        emoji = _action_emoji(action_label)
        lines = [f"{emoji} **{action_label}** — `{name}` (YouTube)"]

        # Include recent messages mirroring the image content
        msgs = list(message_buffer.get(author_id, []))
        if deleted_text and deleted_text in msgs:
            msgs.remove(deleted_text)
        for msg in msgs:
            safe = msg[:200] + "…" if len(msg) > 200 else msg
            lines.append(f"> {safe}")
        if deleted_text:
            safe = deleted_text[:200] + "…" if len(deleted_text) > 200 else deleted_text
            lines.append(f"> ~~{safe}~~")

        if msg_url:
            lines.append(f"🔗 {msg_url}")

        content = "\n".join(lines)[:2000]
        try:
            async with session.post(
                DISCORD_WEBHOOK_PLAIN_URL,
                json={"content": content, "allowed_mentions": {"parse": []}},
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    log.warning("Plain webhook returned %s: %s", resp.status, body)
        except Exception as e:
            log.error("Failed to send plain-text webhook: %s", e)


async def get_rss_video_ids(session: aiohttp.ClientSession) -> list[str]:
    """
    Fetch the channel's RSS feed and return all video IDs listed (up to 15).
    Uses zero API quota — it's a plain HTTP request.
    """
    try:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
        async with session.get(url) as resp:
            if resp.status != 200:
                log.warning("RSS feed returned %s", resp.status)
                return []
            text = await resp.text()
        return re.findall(r"<yt:videoId>([^<]+)</yt:videoId>", text)
    except Exception as e:
        log.error("Error fetching RSS feed: %s", e)
        return []


async def get_live_chat_id(session: aiohttp.ClientSession) -> str | None:
    """
    Find the active live stream from the channel's RSS feed video list.
    Uses a single batched videos.list call (1 API unit) to check all RSS entries.
    Returns the activeLiveChatId of the first video that is currently live.
    """
    video_ids = await get_rss_video_ids(session)
    if not video_ids:
        return None

    # Batch all IDs into one request — videos.list costs 1 unit regardless of count
    try:
        async with session.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "liveStreamingDetails",
                "id": ",".join(video_ids),
                "key": YT_API_KEY,
            },
        ) as resp:
            if resp.status == 403:
                data = await resp.json()
                _handle_quota_error(data)
                return None
            data = await resp.json()
    except Exception as e:
        log.error("Error calling videos.list: %s", e)
        return None

    if "error" in data:
        _handle_quota_error(data)
        return None

    for item in data.get("items", []):
        details = item.get("liveStreamingDetails", {})
        chat_id = details.get("activeLiveChatId")
        actual_start = details.get("actualStartTime")
        actual_end = details.get("actualEndTime")
        video_id = item.get("id", "unknown")

        # Only consider streams that have actually started and have NOT yet ended.
        # A stream that ended recently may still temporarily carry an activeLiveChatId,
        # which caused the bot to latch onto a dead stream instead of the live one.
        if chat_id and actual_start and not actual_end:
            log.info("Found active live chat ID: %s for video %s", chat_id, video_id)
            return chat_id
        elif chat_id and actual_end:
            log.info("Skipping video %s — has activeLiveChatId but stream has ended", video_id)
        elif not chat_id:
            log.debug("Video %s is not live (no activeLiveChatId)", video_id)

    return None


def _handle_quota_error(data: dict) -> None:
    """Log a clear message when the YouTube API quota is exhausted."""
    error = data.get("error", {})
    errors = error.get("errors", [])
    reasons = [e.get("reason", "") for e in errors]
    if "quotaExceeded" in reasons or error.get("status") == "RESOURCE_EXHAUSTED":
        log.error(
            "YouTube API quota exhausted. Quota resets at midnight Pacific time. "
            "The bot will keep retrying every %s seconds but calls will fail until then.",
            CHECK_STREAM_INTERVAL,
        )
    else:
        log.error("YouTube API error: %s", data.get("error"))


async def poll_chat(session: aiohttp.ClientSession, live_chat_id: str, fonts) -> None:
    page_token = None

    while True:
        params = {
            "liveChatId": live_chat_id,
            "part": "snippet,authorDetails",
            "key": YT_API_KEY,
            "maxResults": 200,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            async with session.get(
                "https://www.googleapis.com/youtube/v3/liveChat/messages",
                params=params,
            ) as resp:
                if resp.status in (403, 404):
                    data = await resp.json()
                    if resp.status == 403:
                        _handle_quota_error(data)
                    else:
                        log.info("Stream ended or chat unavailable (status %s)", resp.status)
                    return
                data = await resp.json()
        except Exception as e:
            log.error("Poll error: %s", e)
            await asyncio.sleep(10)
            continue

        if "error" in data:
            _handle_quota_error(data)
            return

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            author = item.get("authorDetails", {})
            msg_type = snippet.get("type")
            author_id = author.get("channelId", "unknown")
            name = author.get("displayName", "unknown")

            if author_id:
                display_names[author_id] = name

            if msg_type == "textMessageEvent":
                text = snippet.get("textMessageDetails", {}).get("messageText", "")
                if text:
                    message_cache[item["id"]] = (author_id, text)
                    message_buffer[author_id].append(text)

            elif msg_type == "messageDeletedEvent":
                deleted_id = snippet.get("messageDeletedDetails", {}).get("deletedMessageId", "")
                cached = message_cache.get(deleted_id)
                if cached:
                    cached_author_id, text = cached
                    cached_name = display_names.get(cached_author_id, cached_author_id)
                    log.info("Message deleted from %s", cached_name)
                    image = build_image(cached_author_id, cached_name, fonts, all_muted=False, action_label="Deleted", deleted_text=text)
                    await send_discord_image(session, image, cached_author_id, cached_name, "Deleted", deleted_text=text)

            elif msg_type == "userBannedEvent":
                banned = snippet.get("userBannedDetails", {})
                banned_user = banned.get("bannedUserDetails", {})
                ban_type = banned.get("banType", "permanent")
                duration = int(banned.get("banDurationSeconds", 0))
                banned_id = banned_user.get("channelId", "unknown")
                banned_name = banned_user.get("displayName", "unknown")
                display_names[banned_id] = banned_name

                label = f"Timed out ({format_duration(duration)})" if ban_type == "temporary" else "Banned"
                log.info("%s: %s", label, banned_name)
                image = build_image(banned_id, banned_name, fonts, all_muted=True, action_label=label)
                await send_discord_image(session, image, banned_id, banned_name, label)

        page_token = data.get("nextPageToken")
        # Respect YouTube's requested polling interval, floor at 30s to stay within quota.
        interval = max(data.get("pollingIntervalMillis", 30000) / 1000, 30)
        await asyncio.sleep(interval)


async def main() -> None:
    fonts = load_fonts()
    async with aiohttp.ClientSession() as session:
        while True:
            log.info("Looking for active stream on channel %s", YT_CHANNEL_ID)
            live_chat_id = await get_live_chat_id(session)
            if not live_chat_id:
                log.info("No active stream found, retrying in %ss", CHECK_STREAM_INTERVAL)
                await asyncio.sleep(CHECK_STREAM_INTERVAL)
                continue
            await poll_chat(session, live_chat_id, fonts)
            log.info("Stream ended, checking again in %ss", CHECK_STREAM_INTERVAL)
            await asyncio.sleep(CHECK_STREAM_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
