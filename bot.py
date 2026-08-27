import asyncio
import io
import json
import logging
import os
import re
from collections import defaultdict, deque

import aiohttp
from PIL import Image, ImageDraw, ImageFont

TWITCH_HOST = "irc.chat.twitch.tv"
TWITCH_PORT = 6667
CHANNEL = os.environ["TWITCH_CHANNEL"].lower().lstrip("#")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_WEBHOOK_PLAIN_URL = os.environ.get("DISCORD_WEBHOOK_PLAIN_URL", "")
TWITCH_USERNAME = os.environ["TWITCH_USERNAME"].lower()
TWITCH_TOKEN = os.environ["TWITCH_TOKEN"]
RECONNECT_DELAY = 10
MAX_MESSAGES = 10

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
TWITCH_PURPLE = 0x9146FF

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

message_buffer: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_MESSAGES))
user_colors: dict[str, int] = {}


def parse_tags(raw: str) -> dict:
    tags = {}
    for part in raw.lstrip("@").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k] = v
    return tags


def hex_to_int(color: str) -> int:
    try:
        return int(color.lstrip("#"), 16)
    except Exception:
        return TWITCH_PURPLE


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


def measure_entry(draw, fonts, username, message, action=None):
    font_bold, font_regular, font_italic = fonts
    uname_w = int(draw.textlength(username + ": ", font=font_bold))
    first_avail = IMG_WIDTH - PADDING * 2 - uname_w
    rest_avail = IMG_WIDTH - PADDING * 2
    msg_lines = wrap_text(draw, message, font_regular, first_avail)
    n = len(msg_lines)
    if action:
        last_w = int(draw.textlength(msg_lines[-1], font=font_regular))
        action_w = int(draw.textlength(action, font=font_italic))
        avail = first_avail if n == 1 else rest_avail
        if last_w + action_w > avail:
            n += 1
    return n * LINE_HEIGHT


def draw_entry(draw, fonts, y, username, uname_color, message, msg_color, action=None):
    font_bold, font_regular, font_italic = fonts
    uname_str = username + ": "
    uname_w = int(draw.textlength(uname_str, font=font_bold))
    first_avail = IMG_WIDTH - PADDING * 2 - uname_w
    rest_avail = IMG_WIDTH - PADDING * 2
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
        avail = first_avail if len(msg_lines) == 1 else rest_avail
        if last_w + action_w <= avail:
            y -= LINE_HEIGHT
            ax = (PADDING + uname_w if len(msg_lines) == 1 else PADDING) + last_w
            draw.text((ax, y), action, font=font_italic, fill=MUTED)
            y += LINE_HEIGHT
        else:
            draw.text((PADDING, y), action, font=font_italic, fill=MUTED)
            y += LINE_HEIGHT

    return y


def render_image(username: str, entries: list[tuple], fonts) -> bytes:
    dummy = Image.new("RGB", (IMG_WIDTH, 1))
    dummy_draw = ImageDraw.Draw(dummy)
    total_height = PADDING * 2
    for msg, _, action in entries:
        total_height += measure_entry(dummy_draw, fonts, username, msg, action)

    uname_color = int_to_rgb(user_colors.get(username, TWITCH_PURPLE))
    img = Image.new("RGB", (IMG_WIDTH, total_height), BG)
    draw = ImageDraw.Draw(img)
    y = PADDING
    for msg, msg_color, action in entries:
        y = draw_entry(draw, fonts, y, username, uname_color, msg, msg_color, action)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def build_image(username: str, fonts, all_muted: bool, action_label: str, deleted_text: str = None) -> bytes:
    msgs = list(message_buffer.get(username, []))

    if deleted_text and deleted_text in msgs:
        msgs.remove(deleted_text)

    if not msgs and not deleted_text:
        return render_image(username, [("(no messages recorded)", MUTED, f"—{action_label}")], fonts)

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

    return render_image(username, entries, fonts)


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
    username: str,
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
        lines = [f"{emoji} **{action_label}** — `{username}` (Twitch)"]

        # Include recent messages mirroring the image content
        msgs = list(message_buffer.get(username, []))
        if deleted_text and deleted_text in msgs:
            msgs.remove(deleted_text)
        for msg in msgs:
            # Truncate individual messages to keep the total under Discord's 2000-char limit
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


def handle_clearchat(line: str, fonts) -> tuple[str, bytes, str, None] | None:
    if re.search(r":tmi\.twitch\.tv CLEARCHAT #\S+\s*$", line):
        log.info("Chat fully cleared in #%s", CHANNEL)
        return None

    m = re.match(r"^(@\S+) :tmi\.twitch\.tv CLEARCHAT #\S+ :(\S+)$", line)
    if not m:
        return None

    tags = parse_tags(m.group(1))
    username = m.group(2)
    duration = tags.get("ban-duration")
    label = f"Timed out ({format_duration(int(duration))})" if duration else "Banned"
    return username, build_image(username, fonts, all_muted=True, action_label=label), label, None


def handle_clearmsg(line: str, fonts) -> tuple[str, bytes, str, str] | None:
    m = re.match(r"^(@\S+) :tmi\.twitch\.tv CLEARMSG #\S+ :(.+)$", line)
    if not m:
        return None
    tags = parse_tags(m.group(1))
    login = tags.get("login", "unknown")
    text = m.group(2)
    return login, build_image(login, fonts, all_muted=False, action_label="Deleted", deleted_text=text), "Deleted", text


def handle_privmsg(line: str) -> None:
    m = re.match(r"^(@\S+) :(\S+)!\S+ PRIVMSG #\S+ :(.+)$", line)
    if not m:
        return
    tags = parse_tags(m.group(1))
    username = m.group(2)
    color = tags.get("color", "")
    if color:
        user_colors[username] = hex_to_int(color)
    message_buffer[username].append(m.group(3))

def get_irc_command(line: str) -> str | None:
    parts = line.split(" ")

    #Tagged IRC message:
    #@tags :prefix COMMAND
    if line.startswith("@"):
        return parts[2] if len(parts) > 2 else None

    #Untagged IRC message:
    #:prefix COMMAND ...
    if line.startswith(":"):
        return parts[1] if len(parts) > 1 else None

    #PING, etc.
    return parts[0] if parts else None

async def connect(session: aiohttp.ClientSession, fonts) -> None:
    reader, writer = await asyncio.open_connection(TWITCH_HOST, TWITCH_PORT)
    log.info("Connected to Twitch IRC, joining #%s", CHANNEL)
    for line in [
        b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n",
        f"PASS {TWITCH_TOKEN}\r\n".encode(),
        f"NICK {TWITCH_USERNAME}\r\n".encode(),
        f"JOIN #{CHANNEL}\r\n".encode(),
    ]:
        writer.write(line)
    await writer.drain()

    try:
    async for raw in reader:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

        command = get_irc_command(line)

        if command == "PING":
            writer.write(b"PONG :tmi.twitch.tv\r\n")
            await writer.drain()
            continue

        if command == "NOTICE" and "Login authentication failed" in line:
            log.error("Authentication failed — check TWITCH_USERNAME and TWITCH_TOKEN")
            return

        result = None

        if command == "CLEARCHAT":
            result = handle_clearchat(line, fonts)

        elif command == "CLEARMSG":
            result = handle_clearmsg(line, fonts)

        elif command == "PRIVMSG":
            handle_privmsg(line)

        if result:
            username, image, label, deleted_text = result
            log.info("Sending mod action image for %s", username)
            await send_discord_image(
                session,
                image,
                username,
                label,
                deleted_text=deleted_text,
            )
finally:
    writer.close()
    await writer.wait_closed()


async def main() -> None:
    fonts = load_fonts()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await connect(session, fonts)
            except Exception as e:
                log.error("Connection lost: %s — reconnecting in %ss", e, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    asyncio.run(main())
