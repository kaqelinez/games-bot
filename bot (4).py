import asyncio
import base64
import html
import logging
import os
import random
import re
import socket
import sqlite3
import struct
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReactionTypeEmoji,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

DB_PATH = os.getenv("DB_PATH", "bot.db")
START_TIME = time.time()

dp = Dispatcher()
GROUPS = F.chat.type.in_({"group", "supergroup"})


# ---------------------------------------------------------------- КНОПКИ
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


HOME = [btn("🏠 Меню", "m:main")]


# ---------------------------------------------------------------- КАРТИНКИ-БАННЕРЫ
CAPTION_MAX = 1000  # у подписи к фото лимит 1024 символа
banner_ids: dict[str, str] = {}  # file_id уже загруженных картинок


def banner_for(key):
    """Картинка раздела: file_id (если уже грузили) или встроенный PNG из BANNER_DATA."""
    if not key:
        return None
    if key in banner_ids:
        return banner_ids[key]
    raw = BANNER_DATA.get(key)
    if not raw:
        return None
    return BufferedInputFile(base64.b64decode(raw), filename=f"{key}.png")


def remember_banner(key, msg):
    try:
        if msg is not None and getattr(msg, "photo", None):
            banner_ids[key] = msg.photo[-1].file_id
    except Exception:
        pass


CB_BANNERS = {
    "m:main": "menu", "m:games": "games", "m:xo": "games", "m:croc": "games",
    "m:server": "server", "m:status": "server", "m:profile": "profile",
    "m:settings": "settings", "m:help": "help", "m:helpadm": "help",
}
CB_BANNER_PREFIX = {
    "o:": "online", "f:": "find", "rl:": "rules", "pv:": "priv", "s:": "settings",
}


def cb_banner_key(data: str):
    if data in CB_BANNERS:
        return CB_BANNERS[data]
    for pre, key in CB_BANNER_PREFIX.items():
        if data.startswith(pre):
            return key
    return None


async def send_card(message: Message, key, text: str, kb, reply: bool = True):
    """Отправляет раздел с картинкой (если она есть и текст влезает в подпись)."""
    photo = banner_for(key) if len(text) <= CAPTION_MAX else None
    if photo is not None:
        try:
            send = message.reply_photo if reply else message.answer_photo
            sent = await send(photo, caption=text, parse_mode="HTML", reply_markup=kb)
            remember_banner(key, sent)
            return sent
        except Exception:
            logging.exception("не удалось отправить картинку %s", key)
    send = message.reply if reply else message.answer
    return await send(text, parse_mode="HTML", reply_markup=kb)


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Меняет сообщение раздела; сам переключается между текстом и картинкой."""
    msg = cb.message
    key = cb_banner_key(cb.data or "")
    photo = banner_for(key) if len(text) <= CAPTION_MAX else None
    has_photo = bool(getattr(msg, "photo", None))
    try:
        if has_photo and photo is not None:
            res = await msg.edit_media(
                InputMediaPhoto(media=photo, caption=text, parse_mode="HTML"),
                reply_markup=kb,
            )
            remember_banner(key, res if not isinstance(res, bool) else None)
        elif not has_photo and photo is None:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            # текст <-> картинка: редактировать нельзя, поэтому шлём новое сообщение
            try:
                await msg.delete()
            except Exception:
                pass
            await send_card(msg, key, text, kb, reply=False)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


def bar(n, total, width: int = 10) -> str:
    if not total:
        return "▱" * width
    filled = max(0, min(width, round(width * n / total)))
    return "▰" * filled + "▱" * (width - filled)


# ---------------------------------------------------------------- БАЗА ДАННЫХ
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute(
    """CREATE TABLE IF NOT EXISTS users(
        chat_id INTEGER, user_id INTEGER, name TEXT,
        prefix TEXT DEFAULT '',
        PRIMARY KEY(chat_id, user_id))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS chats(
        chat_id INTEGER PRIMARY KEY,
        monitoring INTEGER DEFAULT 1,
        mimic INTEGER DEFAULT 0)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS groups(
        chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,
        blocked INTEGER DEFAULT 0, present INTEGER DEFAULT 1)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS disabled(
        chat_id INTEGER, command TEXT,
        PRIMARY KEY(chat_id, command))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS bot_admins(
        chat_id INTEGER, user_id INTEGER, name TEXT DEFAULT '',
        PRIMARY KEY(chat_id, user_id))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS blocked_users(
        user_id INTEGER PRIMARY KEY, name TEXT DEFAULT '')"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS seen(
        nick_lc TEXT PRIMARY KEY, nick TEXT, server TEXT, ts INTEGER)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS msglog(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
        chat_id INTEGER, chat_title TEXT, chat_type TEXT,
        user_id INTEGER, name TEXT, username TEXT, text TEXT)"""
)
db.commit()

SETTING_DEFAULTS = {"monitoring": True, "mimic": False}


def get_setting(chat_id: int, key: str) -> bool:
    row = db.execute(
        f"SELECT {key} FROM chats WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return SETTING_DEFAULTS[key]
    return bool(row[0])


def set_setting(chat_id: int, key: str, value: bool):
    db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))
    db.execute(f"UPDATE chats SET {key}=? WHERE chat_id=?", (int(value), chat_id))
    db.commit()


def get_prefix(chat_id: int, user_id: int) -> str:
    row = db.execute(
        "SELECT prefix FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    return row["prefix"] if row and row["prefix"] else ""


def set_prefix(chat_id: int, user, prefix: str):
    db.execute(
        "INSERT OR IGNORE INTO users(chat_id, user_id, name) VALUES(?,?,?)",
        (chat_id, user.id, user.full_name),
    )
    db.execute(
        "UPDATE users SET prefix=?, name=? WHERE chat_id=? AND user_id=?",
        (prefix, user.full_name, chat_id, user.id),
    )
    db.commit()


def label(chat_id: int, user) -> str:
    """Имя игрока с префиксом (обычный текст, без экранирования)."""
    p = get_prefix(chat_id, user.id)
    return f"{p} {user.full_name}" if p else user.full_name


def fmt_time(sec: int) -> str:
    m, s = divmod(max(sec, 0), 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if not h and not m:
        parts.append(f"{s} сек")
    return " ".join(parts)


def is_group(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup")


def monitoring_allowed(message: Message) -> bool:
    if is_group(message):
        return get_setting(message.chat.id, "monitoring")
    return True


MONITORING_OFF_TEXT = (
    "🔕 Мониторинг в этом чате выключен. Админ может включить: /monitoringon"
)


# ---------------------------------------------------------------- ВЛАДЕЛЕЦ: ЛОГИ, ПИНГ, МОНИТОРИНГ
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_ID", "").replace(" ", "").split(",") if x.isdigit()
}
MON_HOST = os.getenv("MON_HOST", "anmine.ru")
MON_PORT = int(os.getenv("MON_PORT", "1932") or 1932)  # Bedrock, UDP
MON_INTERVAL = int(os.getenv("MON_INTERVAL", "120"))
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL")

LOG_BUFFER = deque(maxlen=300)


class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))


_buf = BufferHandler()
_buf.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_buf)


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.reply(f"Твой ID: {message.from_user.id}")


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    t = time.monotonic()
    sent = await message.reply("🏓 Проверяю…")
    ms = int((time.monotonic() - t) * 1000)
    up = fmt_time(int(time.time() - START_TIME))
    anti = "включён ✅" if KEEPALIVE_URL else "выключен ❌"
    text = (
        "🏓 <b>Понг!</b>\n\n"
        f"⚡ Ответ за <b>{ms} мс</b>\n"
        f"⏱ Работаю без перезапуска: <b>{up}</b>\n"
        f"💤 Анти-сон: <b>{anti}</b>"
    )
    if banner_for("ping") is not None:
        try:
            await sent.delete()
        except Exception:
            pass
        await send_card(message, "ping", text, None)
    else:
        await sent.edit_text(text, parse_mode="HTML")


@dp.message(F.chat.type == "private", Command("logs"))
async def cmd_logs(message: Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS:
        await message.answer(
            "⛔ Только для владельца бота.\n"
            "Владелец задаётся переменной OWNER_ID на хостинге "
            "(свой ID можно узнать командой /myid)."
        )
        return
    n = 30
    if command.args and command.args.strip().isdigit():
        n = min(max(int(command.args.strip()), 1), 100)
    lines = list(LOG_BUFFER)[-n:]
    if not lines:
        await message.answer("Логи пока пусты.")
        return
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[-3800:]
    await message.answer(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")


RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")


def _bedrock_ping(host: str, port: int):
    """Пинг Minecraft Bedrock сервера (RakNet, UDP)."""
    pkt = (
        b"\x01"
        + struct.pack(">q", int(time.time() * 1000))
        + RAKNET_MAGIC
        + struct.pack(">q", 2)
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(4)
    try:
        t = time.monotonic()
        sock.sendto(pkt, (host, port))
        data, _ = sock.recvfrom(4096)
        ms = int((time.monotonic() - t) * 1000)
    except Exception:
        return None
    finally:
        sock.close()
    if len(data) < 35 or data[0] != 0x1C:
        return None
    n = struct.unpack(">H", data[33:35])[0]
    parts = data[35:35 + n].decode("utf-8", "ignore").split(";")
    info = {"ms": ms, "motd": parts[1] if len(parts) > 1 else "",
            "online": None, "max": None}
    try:
        info["online"] = int(parts[4])
        info["max"] = int(parts[5])
    except (IndexError, ValueError):
        pass
    return info


async def check_host() -> dict:
    info = await asyncio.to_thread(_bedrock_ping, MON_HOST, MON_PORT)
    return {"up": info is not None, "info": info}


def fmt_check(res: dict) -> str:
    """Короткий текст для уведомлений владельцу."""
    if not res["up"]:
        return f"🖥 {MON_HOST}:{MON_PORT}: 🔴 Недоступен (нет ответа)"
    i = res["info"]
    line = f"🖥 {MON_HOST}:{MON_PORT}: 🟢 Онлайн, {i['ms']} мс"
    if i["online"] is not None:
        line += f"\n👥 Игроков: {i['online']}/{i['max']}"
    return line


async def view_status():
    res = await check_host()
    if res["up"]:
        i = res["info"]
        text = f"🖥 <b>{html.escape(MON_HOST)}</b>\n\n🟢 <b>Онлайн</b> · {i['ms']} мс"
        if i["online"] is not None:
            text += f"\n👥 {bar(i['online'], i['max'])} {i['online']}/{i['max']}"
        if i["motd"]:
            text += f"\n📝 {html.escape(i['motd'])}"
    else:
        text = (
            f"🖥 <b>{html.escape(MON_HOST)}</b>\n\n🔴 <b>Недоступен</b>\n"
            "Сервер не ответил на пинг."
        )
    kb = markup([[btn("🔄 Обновить", "m:status")] + HOME])
    return text, kb


# ---- онлайн по серверам (данные берём с сайта anclan.ru)
ONLINE_URL = os.getenv("ONLINE_URL", "https://anclan.ru")
SKIP_LINES = {"В избранное", "Уведомлять о входе", "Скопировать ник"}


def parse_online(page: str):
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    page = re.sub(r"<[^>]+>", "\n", page)
    page = html.unescape(page)
    page = re.sub(r"(\d+)\s*\n\s*/\s*\n\s*(\d+)", r"\1/\2", page)
    page = re.sub(r"AnMine\s*#\s*(\d+)", r"AnMine #\1", page)
    lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
    servers, cur = [], None
    for i, ln in enumerate(lines):
        if re.fullmatch(r"AnMine #\d+", ln):
            cur = {"name": ln, "online": None, "max": None, "players": []}
            servers.append(cur)
            continue
        if cur is None:
            continue
        c = re.fullmatch(r"(\d+)\s*/\s*(\d+)", ln)
        if c and cur["online"] is None:
            cur["online"], cur["max"] = int(c.group(1)), int(c.group(2))
            continue
        if ln == "В избранное" and cur["online"] is not None and i > 0:
            nick = lines[i - 1]
            if nick not in SKIP_LINES and not re.fullmatch(r"\d\d:\d\d:\d\d", nick):
                cur["players"].append(nick)
    return servers


async def fetch_online():
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        async with sess.get(ONLINE_URL) as r:
            page = await r.text()
    servers = parse_online(page)
    record_seen(servers)
    return servers


def record_seen(servers):
    """Запоминает, когда и где мы в последний раз видели каждого игрока."""
    now = int(time.time())
    rows = [
        (nick.lower(), nick, srv["name"], now)
        for srv in servers
        for nick in srv["players"]
    ]
    if rows:
        db.executemany(
            "INSERT OR REPLACE INTO seen(nick_lc, nick, server, ts) VALUES(?,?,?,?)",
            rows,
        )
        db.commit()


def server_num(name: str) -> str:
    m = re.search(r"#(\d+)", name)
    return m.group(1) if m else name


async def view_online(n=None):
    try:
        servers = await fetch_online()
    except Exception:
        logging.exception("online fetch failed")
        servers = None

    if not servers:
        text = "❌ <b>Не удалось получить онлайн</b>\nanclan.ru не ответил или сайт изменился."
        return text, markup([[btn("🔄 Повторить", "o:all")] + HOME])

    if n is not None:
        srv = next((x for x in servers if server_num(x["name"]) == str(n)), None)
        if not srv:
            return (
                f"Сервера #{n} нет на сайте.",
                markup([[btn("◀️ К серверам", "o:all")] + HOME]),
            )
        head = (
            f"🎮 <b>{html.escape(srv['name'])}</b>\n"
            f"{bar(srv['online'], srv['max'])} {srv['online']}/{srv['max']}\n\n"
        )
        body = ", ".join(html.escape(x) for x in srv["players"]) or "Никого нет"
        if len(body) > 3400:
            body = body[:3400].rsplit(", ", 1)[0] + "\n… список обрезан"
        kb = markup([
            [btn("◀️ К серверам", "o:all"), btn("🔄 Обновить", f"o:{n}")],
            HOME,
        ])
        return head + body, kb

    total = sum(x["online"] or 0 for x in servers)
    blocks = [
        f"🎮 <b>{html.escape(x['name'])}</b>\n{bar(x['online'], x['max'])} {x['online']}/{x['max']}"
        for x in servers
    ]
    text = (
        "🌐 <b>Онлайн AnMine</b>\n\n" + "\n\n".join(blocks)
        + f"\n\n👥 Всего: <b>{total}</b>\nНажми на сервер, чтобы увидеть ники 👇"
    )
    buttons = [btn(f"👥 #{server_num(x['name'])}", f"o:{server_num(x['name'])}") for x in servers]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([btn("🔄 Обновить", "o:all")] + HOME)
    return text, markup(rows)


@dp.message(Command("server"))
async def cmd_server(message: Message):
    if not monitoring_allowed(message):
        await message.reply(MONITORING_OFF_TEXT)
        return
    text, kb = await view_status()
    await send_card(message, "server", text, kb)


@dp.message(Command("online"))
async def cmd_online(message: Message, command: CommandObject):
    if not monitoring_allowed(message):
        await message.reply(MONITORING_OFF_TEXT)
        return
    arg = (command.args or "").strip()
    text, kb = await view_online(int(arg) if arg.isdigit() else None)
    await send_card(message, "online", text, kb)


@dp.callback_query(F.data.startswith("o:"))
async def cb_online(cb: CallbackQuery):
    if cb.message.chat.type in ("group", "supergroup") and not get_setting(
        cb.message.chat.id, "monitoring"
    ):
        await cb.answer("🔕 Мониторинг в этом чате выключен", show_alert=True)
        return
    arg = cb.data[2:]
    text, kb = await view_online(int(arg) if arg.isdigit() else None)
    await safe_edit(cb, text, kb)
    await cb.answer()


# ---- /find: где сейчас игрок и когда его видели в последний раз
MSK = timezone(timedelta(hours=3))


def fmt_ago_full(ts: int) -> str:
    sec = max(int(time.time()) - ts, 0)
    when = datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")
    if sec < 60:
        return f"только что ({when} МСК)"
    d, rest = divmod(sec, 86400)
    ago = fmt_time(rest).replace(" сек", "")
    if d:
        ago = f"{d} дн {ago}".strip()
    return f"{ago} назад ({when} МСК)"


async def view_find(query: str):
    query = query.strip().lstrip("@")
    q = query.lower()
    kb_again = markup([[btn("🔄 Обновить", f"f:{query[:40]}")] + HOME])
    try:
        servers = await fetch_online()
    except Exception:
        logging.exception("find fetch failed")
        servers = None
    if not servers:
        return (
            "❌ <b>Не удалось получить онлайн</b>\nanclan.ru не ответил или сайт изменился.",
            kb_again,
        )

    # сейчас в сети: точное совпадение, иначе по части ника
    exact, partial = [], []
    for srv in servers:
        for nick in srv["players"]:
            if nick.lower() == q:
                exact.append((nick, srv["name"]))
            elif q in nick.lower():
                partial.append((nick, srv["name"]))
    found = exact or partial

    if found:
        lines = [
            f"🟢 <b>{html.escape(n)}</b> — сейчас на <b>{html.escape(s_)}</b>"
            for n, s_ in found[:15]
        ]
        if len(found) > 15:
            lines.append(f"… и ещё {len(found) - 15}")
        head = "🔎 <b>Поиск:</b> " + html.escape(query) + "\n\n"
        return head + "\n".join(lines), kb_again

    # не в сети — смотрим историю
    rows = db.execute(
        "SELECT nick, server, ts FROM seen WHERE nick_lc=?", (q,)
    ).fetchall()
    if not rows:
        rows = db.execute(
            "SELECT nick, server, ts FROM seen WHERE nick_lc LIKE ? "
            "ORDER BY ts DESC LIMIT 5",
            (f"%{q.replace('%', '').replace('_', '')}%",),
        ).fetchall()
    head = "🔎 <b>Поиск:</b> " + html.escape(query) + "\n\n"
    if rows:
        lines = [
            f"🔴 <b>{html.escape(r['nick'])}</b> не в сети\n"
            f"🕒 Последний раз: {fmt_ago_full(r['ts'])}\n"
            f"🎮 Был на: {html.escape(r['server'])}"
            for r in rows
        ]
        return head + "\n\n".join(lines), kb_again
    return (
        head + "🤷 Игрока сейчас нет в сети, и бот его ещё не видел.\n"
        "<i>Бот запоминает игроков только при запросах /online и /find, "
        "поэтому история может быть неполной.</i>",
        kb_again,
    )


@dp.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    if not monitoring_allowed(message):
        await message.reply(MONITORING_OFF_TEXT)
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "🔎 Напиши ник: <code>/find ник</code>\n"
            "Покажу, на каком сервере игрок сейчас, а если его нет — "
            "когда его видели в последний раз.",
            parse_mode="HTML",
        )
        return
    text, kb = await view_find(arg[:40])
    await send_card(message, "find", text, kb)


@dp.callback_query(F.data.startswith("f:"))
async def cb_find(cb: CallbackQuery):
    if cb.message.chat.type in ("group", "supergroup") and not get_setting(
        cb.message.chat.id, "monitoring"
    ):
        await cb.answer("🔕 Мониторинг в этом чате выключен", show_alert=True)
        return
    text, kb = await view_find(cb.data[2:])
    await safe_edit(cb, text, kb)
    await cb.answer()


monitor_state = {"up": None}


async def monitor_loop(bot: Bot):
    while True:
        try:
            res = await check_host()
            if (
                monitor_state["up"] is not None
                and res["up"] != monitor_state["up"]
            ):
                for oid in OWNER_IDS:
                    try:
                        await bot.send_message(oid, "⚠️ Статус изменился\n" + fmt_check(res))
                    except Exception:
                        pass
            monitor_state["up"] = res["up"]
        except Exception:
            logging.exception("monitor error")
        await asyncio.sleep(MON_INTERVAL)


async def keepalive_loop():
    """Раз в 10 минут обращается к собственному адресу, чтобы хостинг не усыплял бота."""
    if not KEEPALIVE_URL:
        logging.info("keepalive выключен (нет RENDER_EXTERNAL_URL / KEEPALIVE_URL)")
        return
    timeout = aiohttp.ClientTimeout(total=15)
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(KEEPALIVE_URL) as r:
                    logging.info("keepalive: %s", r.status)
        except Exception:
            logging.warning("keepalive не удался")


# ---------------------------------------------------------------- УПРАВЛЕНИЕ БОТОМ
ENV_BLOCKED = {
    int(x) for x in os.getenv("BLOCKED_CHATS", "").replace(" ", "").split(",")
    if re.fullmatch(r"-?\d+", x)
}

KNOWN_COMMANDS = {
    "start", "xo", "croc", "top", "server", "online", "find", "profile", "ping",
    "mute", "unmute", "ban", "unban", "kick", "giveprefix", "removeprefix",
    "monitoringon", "monitoringoff", "mimicon", "mimicoff",
    "addadmin", "deladmin", "admins", "rules", "priv",
}
ALIASES = {
    "menu": "start", "games": "start", "help": "start",
    "prefix": "profile", "monitoringonoff": "monitoringoff",
    "privileges": "priv", "privs": "priv", "donate": "priv", "donates": "priv",
}
CB_TO_CMD = {"m:xo": "xo", "m:croc": "croc", "m:top": "top", "m:status": "server"}

group_cache: dict[int, str] = {}
leave_tried: set[int] = set()


def norm_cmd(s: str) -> str:
    s = s.strip().lower().lstrip("/").split("@")[0]
    return ALIASES.get(s, s)


def remember_group(chat, force: bool = False):
    title = chat.title or str(chat.id)
    if not force and group_cache.get(chat.id) == title:
        return
    group_cache[chat.id] = title
    db.execute(
        "INSERT OR IGNORE INTO groups(chat_id, title, type) VALUES(?,?,?)",
        (chat.id, title, chat.type),
    )
    db.execute(
        "UPDATE groups SET title=?, type=?, present=1 WHERE chat_id=?",
        (title, chat.type, chat.id),
    )
    db.commit()


def mark_group_left(chat_id: int):
    db.execute("UPDATE groups SET present=0 WHERE chat_id=?", (chat_id,))
    db.commit()
    group_cache.pop(chat_id, None)


def groups_list():
    return db.execute(
        "SELECT * FROM groups WHERE present=1 OR blocked=1 "
        "ORDER BY title COLLATE NOCASE, chat_id"
    ).fetchall()


def group_title(chat_id: int) -> str:
    row = db.execute("SELECT title FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
    return row["title"] if row and row["title"] else str(chat_id)


def is_blocked(chat_id: int) -> bool:
    if chat_id in ENV_BLOCKED:
        return True
    row = db.execute("SELECT blocked FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
    return bool(row and row["blocked"])


def set_blocked(chat_id: int, value: bool):
    db.execute(
        "INSERT OR IGNORE INTO groups(chat_id, title, type) VALUES(?,?,?)",
        (chat_id, str(chat_id), "group"),
    )
    db.execute("UPDATE groups SET blocked=? WHERE chat_id=?", (int(value), chat_id))
    db.commit()
    if not value:
        leave_tried.discard(chat_id)


def is_disabled(chat_id: int, cmd: str) -> bool:
    return db.execute(
        "SELECT 1 FROM disabled WHERE chat_id=? AND command=?", (chat_id, cmd)
    ).fetchone() is not None


def disabled_list(chat_id: int):
    return [
        r["command"]
        for r in db.execute(
            "SELECT command FROM disabled WHERE chat_id=? ORDER BY command", (chat_id,)
        )
    ]


def is_bot_admin(chat_id: int, user_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM bot_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone() is not None


ENV_BLOCKED_USERS = {
    int(x) for x in os.getenv("BLOCKED_USERS", "").replace(" ", "").split(",")
    if x.isdigit()
}


def is_user_blocked(user_id: int) -> bool:
    if user_id in ENV_BLOCKED_USERS:
        return True
    return db.execute(
        "SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)
    ).fetchone() is not None


def resolve_chat(arg: str):
    """Номер из списка /groups (1, 2, 3…) или ID чата. None, если не подошло."""
    arg = arg.strip()
    if not re.fullmatch(r"-?\d+", arg):
        return None
    n = int(arg)
    if 0 < n <= 9999:
        rows = groups_list()
        return rows[n - 1]["chat_id"] if n <= len(rows) else None
    return n


async def notify_owners(bot: Bot, text: str):
    for oid in OWNER_IDS:
        try:
            await bot.send_message(oid, text)
        except Exception:
            pass


async def fire(bot: Bot, chat_id: int, message_id: int):
    """Ставит 🔥 на сообщение (за победу в мини-игре)."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji="🔥")],
        )
    except Exception as e:
        logging.warning("реакция 🔥 не поставилась: %s", e)


async def owner_only(message: Message) -> bool:
    if message.from_user.id in OWNER_IDS:
        return True
    await message.reply("⛔ Только для владельца бота (переменная OWNER_ID).")
    return False


class GateMiddleware(BaseMiddleware):
    """Игнорирует заблокированные группы и отключённые команды."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None and user.id not in OWNER_IDS and is_user_blocked(user.id):
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:
                    pass
            return None
        msg = event if isinstance(event, Message) else getattr(event, "message", None)
        chat = getattr(msg, "chat", None)
        if chat is not None and chat.type in ("group", "supergroup"):
            bot = data.get("bot")
            remember_group(chat)
            if is_blocked(chat.id):
                if bot and chat.id not in leave_tried:
                    leave_tried.add(chat.id)
                    try:
                        await bot.leave_chat(chat.id)
                    except Exception:
                        pass
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return None

            cmd = None
            if isinstance(event, Message):
                text = event.text or ""
                if text.startswith("/") and text.split():
                    first = text.split()[0][1:]
                    name, _, target = first.partition("@")
                    ok = True
                    if target and bot:
                        me = await bot.me()
                        ok = target.lower() == (me.username or "").lower()
                    if ok:
                        cmd = norm_cmd(name)
            elif isinstance(event, CallbackQuery) and event.data:
                cmd = CB_TO_CMD.get(event.data) or (
                    "online" if event.data.startswith("o:")
                    else "find" if event.data.startswith("f:")
                    else "rules" if event.data.startswith("rl:")
                    else "priv" if event.data.startswith("pv:")
                    else "xo" if event.data.startswith(("xo:", "g:xo"))
                    else "croc" if event.data.startswith(("cr:", "g:croc")) else None
                )
            if cmd and is_disabled(chat.id, cmd):
                if isinstance(event, Message):
                    await event.reply("🚫 Эта команда отключена в этом чате.")
                else:
                    await event.answer("🚫 Отключено в этом чате", show_alert=True)
                return None
        return await handler(event, data)


dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())


# ---- лог того, что пишут боту (смотреть может только владелец: /messages)
MSGLOG_KEEP = 3000


def log_message(m: Message, u, text: str):
    chat = m.chat
    db.execute(
        "INSERT INTO msglog(ts, chat_id, chat_title, chat_type, user_id, name, username, text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), chat.id, chat.title or "", chat.type,
         u.id, u.full_name, u.username or "", text[:500]),
    )
    if random.random() < 0.02:  # изредка чистим старое
        db.execute(
            "DELETE FROM msglog WHERE id <= (SELECT MAX(id) FROM msglog) - ?",
            (MSGLOG_KEEP,),
        )
    db.commit()


class MsgLogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, Message):
                u = event.from_user
                text = event.text or event.caption
                private = event.chat.type == "private"
                if u and not u.is_bot and text and not (private and u.id in OWNER_IDS):
                    log_message(event, u, text)
        except Exception:
            logging.exception("msglog")
        return await handler(event, data)


dp.message.outer_middleware(MsgLogMiddleware())


@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = event.new_chat_member.status
    who = event.from_user.full_name if event.from_user else "?"
    if status in ("member", "administrator"):
        remember_group(chat, force=True)
        if is_blocked(chat.id):
            try:
                await bot.send_message(chat.id, "🚫 Бот запрещён в этой группе.")
            except Exception:
                pass
            try:
                await bot.leave_chat(chat.id)
            except Exception:
                pass
            return
        await notify_owners(bot, f"➕ Бота добавили в группу «{chat.title}» ({chat.id}). Добавил: {who}")
    elif status in ("left", "kicked"):
        mark_group_left(chat.id)
        await notify_owners(bot, f"➖ Бота убрали из группы «{chat.title}» ({chat.id}).")


OWNER_HELP = (
    "👑 <b>Команды владельца</b> (в личке с ботом)\n\n"
    "📋 /groups — список групп\n"
    "🚫 /blockchat номер — запретить бота в группе\n"
    "✅ /unblockchat номер — разрешить снова\n"
    "✉️ /say номер текст — написать от имени бота\n"
    "🔕 /disable команда номер — отключить команду в группе\n"
    "🔔 /enable команда номер — включить обратно\n"
    "📃 /disabled номер — что отключено\n"
    "🙅 /blockuser — запретить человеку пользоваться ботом (ответом или по ID)\n"
    "✅ /unblockuser — разблокировать человека\n"
    "📃 /blockedusers — список заблокированных людей\n"
    "♻️ /restart — перезапустить бота\n"
    "🧾 /logs — логи бота\n"
    "💬 /messages — что пишут боту (/messages 50, pm — только личка, g1 — группа №1)\n"
    "🆔 /myid — твой ID\n\n"
    "Номер — из списка /groups, либо можно указать ID группы."
)


@dp.message(F.chat.type == "private", Command("owner"))
async def cmd_owner(message: Message):
    if not await owner_only(message):
        return
    await message.answer(OWNER_HELP, parse_mode="HTML")


@dp.message(F.chat.type == "private", Command("restart"))
async def cmd_restart(message: Message):
    if not await owner_only(message):
        return
    await message.answer(
        "♻️ <b>Перезапустить бота?</b>\n\n"
        "Он будет недоступен 1–2 минуты. Данные в памяти (идущие игры) "
        "сбросятся, а на бесплатном Render могут сброситься и настройки.",
        parse_mode="HTML",
        reply_markup=markup([[btn("✅ Да, перезапустить", "rs:yes"), btn("❌ Отмена", "rs:no")]]),
    )


@dp.callback_query(F.data.in_({"rs:yes", "rs:no"}))
async def cb_restart(cb: CallbackQuery):
    if cb.from_user.id not in OWNER_IDS:
        await cb.answer("⛔ Только для владельца бота", show_alert=True)
        return
    if cb.data == "rs:no":
        await cb.message.edit_text("Отменено ✅")
        await cb.answer()
        return
    await cb.message.edit_text("♻️ Перезапускаюсь… вернусь через 1–2 минуты.")
    await cb.answer()
    logging.warning("перезапуск по команде владельца %s", cb.from_user.id)
    await asyncio.sleep(2)  # даём Telegram отметить обновление как обработанное
    os._exit(1)  # Render сам запустит сервис заново


@dp.message(F.chat.type == "private", Command("groups"))
async def cmd_groups(message: Message):
    if not await owner_only(message):
        return
    rows = groups_list()
    if not rows:
        await message.answer(
            "Пока не знаю ни одной группы. Бот запоминает группу, когда в ней "
            "появляется активность (сообщение или команда)."
        )
        return
    entries = []
    for i, r in enumerate(rows, 1):
        icon = "🚫" if r["blocked"] else "🟢"
        left = " (бот вышел)" if not r["present"] else ""
        entries.append(
            f"{i}. {icon} <b>{html.escape(r['title'] or '—')}</b>{left}\n<code>{r['chat_id']}</code>"
        )
    header = f"📋 <b>Группы бота</b> ({len(rows)})\n\n"
    footer = (
        "\n\n🚫 /blockchat номер — запретить\n"
        "✅ /unblockchat номер — разрешить\n"
        "✉️ /say номер текст — написать от имени бота"
    )
    chunks, cur = [], header
    for e in entries:
        if len(cur) + len(e) > 3500:
            chunks.append(cur)
            cur = ""
        cur += e + "\n\n"
    chunks.append(cur.rstrip() + footer)
    for c in chunks:
        await message.answer(c, parse_mode="HTML")


@dp.message(F.chat.type == "private", Command("blockchat"))
async def cmd_blockchat(message: Message, command: CommandObject):
    if not await owner_only(message):
        return
    chat_id = resolve_chat(command.args) if command.args else None
    if chat_id is None:
        await message.reply("Формат: /blockchat номер_или_ID (список: /groups)")
        return
    set_blocked(chat_id, True)
    try:
        await message.bot.leave_chat(chat_id)
        left = "Бот вышел из группы."
    except Exception as e:
        left = f"Выйти из группы не получилось: {e}"
    await message.reply(
        f"🚫 Бот запрещён в «{group_title(chat_id)}» ({chat_id}).\n{left}\n"
        "Если его добавят снова, он сразу выйдет.\n\n"
        "Чтобы запрет пережил перезапуск, добавь ID в переменную BLOCKED_CHATS на Render."
    )


@dp.message(F.chat.type == "private", Command("unblockchat"))
async def cmd_unblockchat(message: Message, command: CommandObject):
    if not await owner_only(message):
        return
    chat_id = resolve_chat(command.args) if command.args else None
    if chat_id is None:
        await message.reply("Формат: /unblockchat номер_или_ID (список: /groups)")
        return
    set_blocked(chat_id, False)
    extra = ""
    if chat_id in ENV_BLOCKED:
        extra = "\n⚠️ ID всё ещё указан в BLOCKED_CHATS на Render — удали его оттуда."
    await message.reply(
        f"✅ Запрет снят для «{group_title(chat_id)}» ({chat_id}).\n"
        "Сам бот не вернётся: добавь его в группу заново." + extra
    )


@dp.message(F.chat.type == "private", Command("say"))
async def cmd_say(message: Message, command: CommandObject):
    if not await owner_only(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /say номер_или_ID текст")
        return
    chat_id = resolve_chat(parts[0])
    if chat_id is None:
        await message.reply("Не нашёл такую группу. Список: /groups")
        return
    try:
        await message.bot.send_message(chat_id, parts[1])
    except Exception as e:
        await message.reply(f"❌ Не отправилось: {e}")
        return
    await message.reply(f"✅ Отправлено в «{group_title(chat_id)}».")


@dp.message(Command("blockuser"))
async def cmd_blockuser(message: Message, command: CommandObject):
    if not await owner_only(message):
        return
    t = reply_target(message)
    uid, name = None, ""
    if t:
        uid, name = t.id, t.full_name
    elif command.args and command.args.strip().isdigit():
        uid = int(command.args.strip())
    if uid is None:
        await message.reply(
            "Ответь командой на сообщение человека (в группе) или укажи ID: /blockuser 123456"
        )
        return
    if uid in OWNER_IDS:
        await message.reply("Себя заблокировать нельзя 🙂")
        return
    db.execute(
        "INSERT OR REPLACE INTO blocked_users(user_id, name) VALUES(?,?)", (uid, name)
    )
    db.commit()
    await message.reply(
        f"🚫 {name or uid} ({uid}) заблокирован: бот игнорирует его во всех чатах.\n\n"
        "Чтобы блокировка пережила перезапуск, добавь ID в переменную BLOCKED_USERS на Render."
    )


@dp.message(Command("unblockuser"))
async def cmd_unblockuser(message: Message, command: CommandObject):
    if not await owner_only(message):
        return
    t = reply_target(message)
    uid = t.id if t else None
    if uid is None and command.args and command.args.strip().isdigit():
        uid = int(command.args.strip())
    if uid is None:
        await message.reply("Ответь командой на сообщение человека или укажи ID: /unblockuser 123456")
        return
    db.execute("DELETE FROM blocked_users WHERE user_id=?", (uid,))
    db.commit()
    extra = ""
    if uid in ENV_BLOCKED_USERS:
        extra = "\n⚠️ ID всё ещё указан в BLOCKED_USERS на Render — удали его оттуда."
    await message.reply(f"✅ {uid} разблокирован.{extra}")


@dp.message(Command("blockedusers"))
async def cmd_blockedusers(message: Message):
    if not await owner_only(message):
        return
    people = {
        r["user_id"]: r["name"]
        for r in db.execute("SELECT user_id, name FROM blocked_users")
    }
    for uid in ENV_BLOCKED_USERS:
        people.setdefault(uid, "")
    if not people:
        await message.reply("Заблокированных людей нет ✅")
        return
    lines = [
        f"🚫 {html.escape(name) if name else 'ID'} — <code>{uid}</code>"
        for uid, name in people.items()
    ]
    await message.reply(
        "🙅 <b>Заблокированные люди</b>\n\n" + "\n".join(lines)[:3500],
        parse_mode="HTML",
    )


async def resolve_cmd_chat(message: Message, parts: list, need_cmd: bool):
    """Определяет чат для /disable, /enable, /disabled. Возвращает chat_id или None."""
    if is_group(message):
        if not await require_admin(message):
            return None
        return message.chat.id
    if not await owner_only(message):
        return None
    idx = 1 if need_cmd else 0
    if len(parts) <= idx:
        await message.reply("Укажи группу: номер из /groups или ID.")
        return None
    chat_id = resolve_chat(parts[idx])
    if chat_id is None:
        await message.reply("Не нашёл такую группу. Список: /groups")
    return chat_id


@dp.message(Command("disable", "enable"))
async def cmd_disable_enable(message: Message, command: CommandObject):
    disable = (command.command or "").lower() == "disable"
    parts = (command.args or "").split()
    if not parts:
        await message.reply(
            f"Формат: /{'disable' if disable else 'enable'} команда"
            + ("" if is_group(message) else " номер_группы")
            + "\nНапример: /disable xo"
        )
        return
    cmd = norm_cmd(parts[0])
    if cmd not in KNOWN_COMMANDS:
        await message.reply(
            "Не знаю такую команду. Можно: " + ", ".join(sorted(KNOWN_COMMANDS))
        )
        return
    chat_id = await resolve_cmd_chat(message, parts, need_cmd=True)
    if chat_id is None:
        return
    if disable:
        db.execute(
            "INSERT OR IGNORE INTO disabled(chat_id, command) VALUES(?,?)", (chat_id, cmd)
        )
        text = f"🔕 Команда /{cmd} отключена в «{group_title(chat_id)}»."
    else:
        db.execute("DELETE FROM disabled WHERE chat_id=? AND command=?", (chat_id, cmd))
        text = f"🔔 Команда /{cmd} снова включена в «{group_title(chat_id)}»."
    db.commit()
    await message.reply(text)


@dp.message(Command("disabled"))
async def cmd_disabled(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    chat_id = await resolve_cmd_chat(message, parts, need_cmd=False)
    if chat_id is None:
        return
    items = disabled_list(chat_id)
    if not items:
        await message.reply("Отключённых команд нет ✅")
        return
    await message.reply(
        f"🔕 Отключено в «{group_title(chat_id)}»:\n" + "\n".join(f"/{c}" for c in items)
    )


# ---- админы бота (могут использовать мут/бан/кик без прав админа в Telegram)
@dp.message(GROUPS, Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    t = reply_target(message)
    uid, name = None, ""
    if t:
        uid, name = t.id, t.full_name
    elif command.args and command.args.strip().isdigit():
        uid = int(command.args.strip())
    if uid is None:
        await message.reply("Ответь командой на сообщение участника или укажи ID: /addadmin 123456")
        return
    db.execute(
        "INSERT OR REPLACE INTO bot_admins(chat_id, user_id, name) VALUES(?,?,?)",
        (message.chat.id, uid, name),
    )
    db.commit()
    await message.reply(
        f"🛡 {name or uid} теперь админ бота: может использовать /mute, /ban, /kick."
    )


@dp.message(GROUPS, Command("deladmin"))
async def cmd_deladmin(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    t = reply_target(message)
    uid = t.id if t else None
    if uid is None and command.args and command.args.strip().isdigit():
        uid = int(command.args.strip())
    if uid is None:
        await message.reply("Ответь командой на сообщение участника или укажи ID: /deladmin 123456")
        return
    db.execute(
        "DELETE FROM bot_admins WHERE chat_id=? AND user_id=?", (message.chat.id, uid)
    )
    db.commit()
    await message.reply("🛡 Админ бота снят.")


@dp.message(GROUPS, Command("admins"))
async def cmd_admins(message: Message):
    rows = db.execute(
        "SELECT user_id, name FROM bot_admins WHERE chat_id=?", (message.chat.id,)
    ).fetchall()
    if not rows:
        await message.reply("Админов бота здесь нет. Назначить: /addadmin (ответом на сообщение).")
        return
    lines = [f"• {r['name'] or 'ID'} ({r['user_id']})" for r in rows]
    await message.reply("🛡 <b>Админы бота</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------- ЧТО ПИШУТ БОТУ (владелец)
def fmt_msg(r) -> str:
    when = datetime.fromtimestamp(r["ts"], MSK).strftime("%d.%m %H:%M")
    who = html.escape(r["name"] or "?")
    if r["username"]:
        who += f" (@{html.escape(r['username'])})"
    where = "ЛС" if r["chat_type"] == "private" else html.escape(r["chat_title"] or str(r["chat_id"]))
    return (
        f"🕒 {when} · <b>{who}</b> · {where}\n"
        f"🆔 <code>{r['user_id']}</code>\n{html.escape(r['text'] or '')}"
    )


@dp.message(F.chat.type == "private", Command("messages", "inbox"))
async def cmd_messages(message: Message, command: CommandObject):
    if not await owner_only(message):
        return
    n, scope, gnum = 20, "all", None
    for tok in (command.args or "").lower().split():
        if tok in ("pm", "private", "лс"):
            scope = "pm"
        elif tok == "groups":
            scope = "groups"
        elif re.fullmatch(r"g\d+", tok):
            scope, gnum = "group", int(tok[1:])
        elif tok.isdigit():
            n = min(max(int(tok), 1), 100)
    where, params = [], []
    if scope == "pm":
        where.append("chat_type='private'")
    elif scope == "groups":
        where.append("chat_type!='private'")
    elif scope == "group":
        chat_id = resolve_chat(str(gnum))
        if chat_id is None:
            await message.reply("Не нашёл такую группу. Список: /groups")
            return
        where.append("chat_id=?")
        params.append(chat_id)
    sql = "SELECT * FROM msglog"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", params + [n]).fetchall()
    if not rows:
        await message.reply(
            "💬 Пока никто ничего не писал боту.\n\n"
            "<i>Лог ведётся с момента обновления бота. В группах бот видит обычные "
            "сообщения только если у него отключён Privacy Mode "
            "(@BotFather → /setprivacy → Disable).</i>",
            parse_mode="HTML",
        )
        return
    out, size = [], 0
    for r in rows:  # от новых к старым, пока влезает в сообщение
        block = fmt_msg(r)
        if size + len(block) > 3500 and out:
            break
        out.append(block)
        size += len(block) + 2
    out.reverse()
    names = {"all": "везде", "pm": "личка", "groups": "группы", "group": "группа"}
    head = f"💬 <b>Что пишут боту</b> ({names[scope]}): {len(out)} из {len(rows)}\n\n"
    await message.reply(head + "\n\n".join(out), parse_mode="HTML")


# ---------------------------------------------------------------- ПРАВИЛА И ПРИВИЛЕГИИ (anmine.su)
SITE_URL = os.getenv("ANMINE_SITE", "https://anmine.su").rstrip("/")
RULES_URL = SITE_URL + "/rules"
PRIV_URL = SITE_URL + "/donates"
SITE_TTL = 1800  # кэш страниц, секунд
site_cache: dict[str, tuple[float, str]] = {}


async def fetch_site(url: str) -> str:
    """Скачивает страницу (кэш 30 минут; если сайт не отвечает — отдаёт старую копию)."""
    now = time.time()
    cached = site_cache.get(url)
    if cached and now - cached[0] < SITE_TTL:
        return cached[1]
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
        ) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                page = await r.text()
    except Exception:
        if cached:
            return cached[1]
        raise
    site_cache[url] = (now, page)
    return page


def html_to_lines(page: str, headings: bool = False) -> list:
    """HTML -> список непустых строк. С headings=True заголовки h1-h6 помечаются '§§ '."""
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", page)
    if headings:
        page = re.sub(
            r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1>",
            lambda m: "\n§§ " + re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))) + "\n",
            page,
        )
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|section|article|h[1-6]|ul|ol|table)>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page)
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in page.splitlines()]
    return [ln for ln in lines if ln]


def site_button(text: str, url: str) -> list:
    return [InlineKeyboardButton(text=text, url=url)]


# ---- /rules
RULE_NUM = r"\d{1,2}(?:\.\d{1,2}){0,3}"
RULE_LINE = re.compile(rf"^({RULE_NUM})\.?\)?\s+(.+)$")


def parse_rules(lines: list) -> dict:
    """{'1': [заголовок], '1.1': [текст, доп. строки (наказание)…], …}"""
    merged, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if re.fullmatch(rf"{RULE_NUM}\.?", ln) and i + 1 < len(lines):
            merged.append(f"{ln} {lines[i + 1]}")  # номер и текст в разных блоках
            i += 2
        else:
            merged.append(ln)
            i += 1
    rules: dict[str, list] = {}
    cur = None
    for ln in merged:
        m = RULE_LINE.match(ln)
        if m:
            key = m.group(1)
            if key in rules:  # повтор (меню, оглавление) — пропускаем
                cur = None
            else:
                rules[key] = [m.group(2)[:300]]
                cur = key
        elif cur and len(rules[cur]) < 8:
            rules[cur].append(ln[:300])
    return dict(sorted(rules.items(), key=lambda kv: tuple(int(x) for x in kv[0].split("."))))


def fmt_rule(key: str, parts: list) -> str:
    text = f"<b>{key}.</b> {html.escape(parts[0])}"
    for extra in parts[1:]:
        text += f"\n  ↳ {html.escape(extra)}"
    return text


async def view_rules(arg: str = ""):
    link = site_button("🌐 Открыть на сайте", RULES_URL)
    try:
        rules = parse_rules(html_to_lines(await fetch_site(RULES_URL)))
    except Exception:
        logging.exception("rules fetch failed")
        rules = None
    if not rules:
        return (
            "❌ <b>Не удалось получить правила</b>\nСайт не ответил или изменился. "
            "Смотри правила сервера AnMine на <code>anmine.su/rules</code>",
            markup([link, [btn("🔄 Повторить", f"rl:{arg or 'all'}")] + HOME]),
        )

    q = arg.strip().rstrip(".").replace(",", ".")
    sections = [(k, v) for k, v in rules.items() if "." not in k]

    if not q or q == "all":
        if sections:
            body = "\n".join(f"<b>{k}.</b> {html.escape(v[0])}" for k, v in sections)
            nums = [btn(k, f"rl:{k}") for k, _ in sections]
            rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
        else:
            body = f"Всего пунктов: {len(rules)}"
            rows = []
        text = (
            "📜 <b>Правила сервера AnMine</b>\n"
            "<i>Это правила сервера AnMine с сайта anmine.su/rules</i>\n\n"
            f"{body[:3000]}\n\n"
            "Напиши <code>/rules 1</code> — раздел целиком, "
            "<code>/rules 4.10</code> — один пункт."
        )
        return text, markup(rows + [link, HOME])

    if not re.fullmatch(r"\d+(\.\d+)*", q):
        return (
            "Напиши номер: <code>/rules 1</code> или <code>/rules 4.10</code>.\n"
            "Список разделов: /rules",
            markup([[btn("📋 Разделы", "rl:all")] + HOME]),
        )

    keys = [k for k in rules if k == q or k.startswith(q + ".")]
    if not keys:
        return (
            f"🤷 Пункта <b>{html.escape(q)}</b> нет в правилах сервера AnMine.\n"
            "Список разделов: /rules",
            markup([[btn("📋 Разделы", "rl:all")] + HOME]),
        )

    out, size = [], 0
    for i, k in enumerate(keys):
        block = fmt_rule(k, rules[k])
        if size + len(block) > 3300 and out:
            out.append(
                f"<i>… показано {i} из {len(keys)} пунктов. "
                f"Дальше: /rules {k}, либо на сайте.</i>"
            )
            break
        out.append(block)
        size += len(block) + 2
    kind = "пункт" if q in rules and len(keys) == 1 else "раздел"
    head = (
        f"📜 <b>Правила сервера AnMine</b> · {kind} {html.escape(q)}\n"
        "<i>Это правила сервера AnMine (anmine.su/rules)</i>\n\n"
    )
    kb = markup([[btn("📋 Разделы", "rl:all")] + HOME, link])
    return head + "\n\n".join(out), kb


@dp.message(Command("rules"))
async def cmd_rules(message: Message, command: CommandObject):
    text, kb = await view_rules(command.args or "")
    await send_card(message, "rules", text, kb)


@dp.callback_query(F.data.startswith("rl:"))
async def cb_rules(cb: CallbackQuery):
    text, kb = await view_rules(cb.data[3:])
    await safe_edit(cb, text, kb)
    await cb.answer()


# ---- /priv — возможности привилегий с anmine.su/donates
PRIV_SKIP = {"правила сервера", "поддержка сервера", "донат сервера", "сервер", "меню"}
PRIV_PAST = "возможности прошлых привилегий"


def parse_privs(page: str):
    """Возвращает (блоки по заголовкам, все строки страницы без пометок)."""
    lines = html_to_lines(page, headings=True)
    blocks, cur = [], None
    for ln in lines:
        if ln.startswith("§§"):
            name = ln[2:].strip()
            cur = {"name": name, "lines": []} if name else None
            if cur:
                blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)
    seen, res = set(), []
    for b in blocks:
        n = b["name"].lower()
        if n in PRIV_SKIP or n in seen or len(b["lines"]) < 2:
            continue
        seen.add(n)
        res.append(b)
    plain = [ln[3:].strip() if ln.startswith("§§") else ln for ln in lines]
    return res, plain


CMD_END = re.compile(r"/[A-Za-z_]+(?: [A-Za-z0-9]+)?(?=\s*(?:\(|$))")
CMD_ANY = re.compile(r"/[A-Za-z_]+")
NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
LINE = "━━━━━━━━━━━━━━"


def classify_priv_line(ln: str):
    """('past'|'cmd'|'perk', готовая HTML-строка)."""
    ln = ln[:300]
    if ln.lower().startswith(PRIV_PAST):
        return "past", "➕ <i>Включает все возможности прошлых привилегий</i>"
    m = CMD_END.search(ln) or CMD_ANY.search(ln)
    if m:
        desc = ln[:m.start()].rstrip(" -–—:⇨>").strip()
        rest = ln[m.end():].strip()
        out = "▸ " + (html.escape(desc) + " — " if desc else "")
        out += f"<code>{html.escape(m.group(0))}</code>"
        if rest:
            out += f" <i>{html.escape(rest)}</i>"
        return "cmd", out
    return "perk", "▸ " + html.escape(ln)


def fmt_priv_lines(lines: list) -> list:
    out, last = [], None
    for ln in lines:
        if ln == last:
            continue
        last = ln
        out.append(classify_priv_line(ln)[1])
    return out


def page_chunks(items: list, limit: int = 3300) -> list:
    pages, cur, size = [], [], 0
    for it in items:
        if size + len(it) > limit and cur:
            pages.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it) + 1
    if cur:
        pages.append(cur)
    return pages


def priv_card(b: dict, idx: int, total: int) -> str:
    past, cmds, perks, last = [], [], [], None
    for ln in b["lines"]:
        if ln == last:
            continue
        last = ln
        kind, txt = classify_priv_line(ln)
        {"past": past, "cmd": cmds, "perk": perks}[kind].append(txt)
    parts = [
        f"💎 <b>{html.escape(b['name'])}</b>  ·  {idx}/{total}",
        "<i>Привилегия сервера AnMine</i>",
        LINE,
    ]
    if past:
        parts.append(past[0])
    if cmds:
        parts.append("\n⚡ <b>Команды</b>\n" + "\n".join(cmds))
    if perks:
        parts.append("\n✨ <b>Возможности</b>\n" + "\n".join(perks))
    text = "\n".join(parts)
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n<i>… остальное на сайте</i>"
    return text


async def view_privs(arg: str = ""):
    link = site_button("🌐 Открыть на сайте", PRIV_URL)
    try:
        blocks, plain = parse_privs(await fetch_site(PRIV_URL))
    except Exception:
        logging.exception("priv fetch failed")
        blocks, plain = [], []
    if not blocks and not plain:
        return (
            "❌ <b>Не удалось получить привилегии</b>\nСайт не ответил или изменился. "
            "Смотри на <code>anmine.su/donates</code>",
            markup([link, [btn("🔄 Повторить", f"pv:{arg or 'all'}")] + HOME]),
        )
    arg = arg.strip()
    n = len(blocks)

    if n >= 2:
        if arg and arg != "all":
            idx = None
            if arg.isdigit() and 1 <= int(arg) <= n:
                idx = int(arg)
            else:
                for i, x in enumerate(blocks, 1):
                    if arg.lower() in x["name"].lower():
                        idx = i
                        break
            if idx is not None:
                nav = [
                    btn("◀️", f"pv:{(idx - 2) % n + 1}"),
                    btn(f"{idx}/{n}", "pv:all"),
                    btn("▶️", f"pv:{idx % n + 1}"),
                ]
                return (
                    priv_card(blocks[idx - 1], idx, n),
                    markup([nav, [btn("📋 Все привилегии", "pv:all")] + HOME, link]),
                )
        lines = [
            f"{NUM_EMOJI[i] if i < len(NUM_EMOJI) else str(i + 1) + '.'} <b>{html.escape(b['name'])}</b>"
            for i, b in enumerate(blocks)
        ]
        text = (
            "💎 <b>Привилегии AnMine</b>\n"
            f"{LINE}\n" + "\n".join(lines)[:2500] + f"\n{LINE}\n"
            "Нажми на привилегию, чтобы увидеть команды и возможности 👇"
        )
        icons = ["🔹", "🔸", "💠", "🔶"]
        names = [btn(f"{icons[i % 4]} {b['name'][:18]}", f"pv:{i + 1}") for i, b in enumerate(blocks)]
        rows = [names[i:i + 2] for i in range(0, len(names), 2)]
        return text, markup(rows + [link, HOME])

    # структуру не распознали — весь текст страницы по страницам
    items = fmt_priv_lines(plain)
    pages = page_chunks(items)
    idx = 0
    if arg.startswith("p") and arg[1:].isdigit():
        idx = min(max(int(arg[1:]) - 1, 0), len(pages) - 1)
    nav = []
    if idx > 0:
        nav.append(btn("◀️", f"pv:p{idx}"))
    nav.append(btn(f"{idx + 1}/{len(pages)}", f"pv:p{idx + 1}"))
    if idx < len(pages) - 1:
        nav.append(btn("▶️", f"pv:p{idx + 2}"))
    head = f"💎 <b>Привилегии AnMine</b>\n{LINE}\n"
    return head + "\n".join(pages[idx]), markup([nav, link, HOME])


@dp.message(Command("priv", "privs", "privileges", "donate", "donates"))
async def cmd_priv(message: Message, command: CommandObject):
    text, kb = await view_privs(command.args or "")
    await send_card(message, "priv", text, kb)


@dp.callback_query(F.data.startswith("pv:"))
async def cb_priv(cb: CallbackQuery):
    text, kb = await view_privs(cb.data[3:])
    await safe_edit(cb, text, kb)
    await cb.answer()


# ---------------------------------------------------------------- ЛИЧКА
@dp.message(F.chat.type == "private")
async def private_any(message: Message):
    me = await message.bot.me()
    kb = markup([[InlineKeyboardButton(
        text="➕ Добавить в группу",
        url=f"https://t.me/{me.username}?startgroup=true",
    )]])
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Я работаю только в группах: крестики-нолики, крокодил, "
        "статус сервера и многое другое.\n\n"
        "Добавь меня в чат и напиши /start 🎮\n\n"
        "<i>Сообщения, которые ты пишешь боту, может видеть владелец бота.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ---------------------------------------------------------------- ГЛАВНОЕ МЕНЮ
HELP_TEXT = (
    "❓ <b>Команды бота</b>\n\n"
    "🎮 /xo, /croc — игры · /top — топ крокодила\n"
    "🖥 /server — статус сервера\n"
    "👥 /online — онлайн (<code>/online 1</code> — ники)\n"
    "🔎 /find ник — где игрок и когда был в сети\n"
    "📜 /rules [номер] — правила сервера AnMine\n"
    "💎 /priv — привилегии AnMine\n"
    "👤 /profile — профиль и префикс\n"
    "🏓 /ping — скорость ответа бота"
)

HELP_ADMIN = (
    "🛡 <b>Команды для админов</b>\n\n"
    "/mute [30m|2h|1d], /unmute, /ban, /unban, /kick — ответом на сообщение\n"
    "/giveprefix текст, /removeprefix — префиксы\n"
    "/addadmin, /deladmin, /admins — админы бота\n"
    "/disable, /enable команда, /disabled — запретить/разрешить команды\n"
    "/monitoringon, /monitoringoff — вкл/выкл /server и /online\n"
    "/mimicon, /mimicoff — режим кривляния\n\n"
    "<i>Для крокодила и кривляния отключи Privacy Mode у бота "
    "(@BotFather → /setprivacy → Disable). Для мута и бана бот должен быть "
    "админом группы.</i>"
)

MAIN_ROWS = [
    [btn("🎮 Игры", "m:games"), btn("🖥 Сервер", "m:server")],
    [btn("📜 Правила", "rl:all"), btn("💎 Привилегии", "pv:all")],
    [btn("👤 Профиль", "m:profile"), btn("⚙️ Настройки", "m:settings")],
    [btn("❓ Команды", "m:help")],
]


def main_text(user) -> str:
    return (
        f"👋 <b>Привет, {html.escape(user.first_name or 'друг')}!</b>\n\n"
        "Я — игровой помощник этого чата.\n\n"
        "🎮 игры  ·  🖥 онлайн\n"
        "📜 правила  ·  💎 привилегии\n\n"
        "Выбери раздел 👇"
    )


def profile_text(chat_id: int, user) -> str:
    p = get_prefix(chat_id, user.id)
    return (
        f"👤 <b>{html.escape(label(chat_id, user))}</b>\n\n"
        f"🏷 Префикс: <b>{html.escape(p) if p else 'нет'}</b>\n\n"
        "<i>Префикс выдают админы чата.</i>"
    )


def settings_view(chat_id: int):
    mon = get_setting(chat_id, "monitoring")
    mim = get_setting(chat_id, "mimic")
    text = (
        "⚙️ <b>Настройки чата</b>\n\n"
        f"🖥 Мониторинг сервера: <b>{'включён' if mon else 'выключен'}</b>\n"
        f"🤪 Режим кривляния: <b>{'включён' if mim else 'выключен'}</b>\n\n"
        "<i>Менять могут только админы чата.</i>"
    )
    kb = markup([
        [btn(f"{'✅' if mon else '❌'} Мониторинг", "s:monitoring")],
        [btn(f"{'✅' if mim else '❌'} Кривляние", "s:mimic")],
        HOME,
    ])
    return text, kb


@dp.message(GROUPS, Command("start", "menu", "games", "help"))
async def cmd_menu(message: Message):
    await send_card(message, "menu", main_text(message.from_user), markup(MAIN_ROWS), reply=False)


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(cb: CallbackQuery):
    action = cb.data[2:]
    chat_id = cb.message.chat.id
    u = cb.from_user

    if action == "main":
        await safe_edit(cb, main_text(u), markup(MAIN_ROWS))

    elif action in ("games", "xo", "croc"):
        text, kb = await games_view(cb.bot, chat_id)
        await safe_edit(cb, text, kb)

    elif action == "top":
        await send_top(cb.message)

    elif action == "server":
        await safe_edit(
            cb,
            "🖥 <b>Сервер</b>\n\nСтатус и онлайн AnMine:",
            markup([
                [btn("📡 Статус", "m:status"), btn("👥 Онлайн", "o:all")],
                [btn("📜 Правила", "rl:all"), btn("💎 Привилегии", "pv:all")],
                HOME,
            ]),
        )

    elif action == "status":
        if cb.message.chat.type in ("group", "supergroup") and not get_setting(
            chat_id, "monitoring"
        ):
            await cb.answer("🔕 Мониторинг в этом чате выключен", show_alert=True)
            return
        text, kb = await view_status()
        await safe_edit(cb, text, kb)

    elif action == "profile":
        await safe_edit(cb, profile_text(chat_id, u), markup([HOME]))

    elif action == "settings":
        text, kb = settings_view(chat_id)
        await safe_edit(cb, text, kb)

    elif action == "help":
        await safe_edit(cb, HELP_TEXT, markup([[btn("🛡 Для админов", "m:helpadm")], HOME]))

    elif action == "helpadm":
        await safe_edit(cb, HELP_ADMIN, markup([[btn("◀️ Команды", "m:help")] + HOME]))

    await cb.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_settings(cb: CallbackQuery):
    key = cb.data[2:]
    if key not in SETTING_DEFAULTS:
        await cb.answer()
        return
    chat_id = cb.message.chat.id
    if not await is_chat_admin(cb.bot, chat_id, cb.from_user.id):
        await cb.answer("⛔ Только для админов чата", show_alert=True)
        return
    set_setting(chat_id, key, not get_setting(chat_id, key))
    text, kb = settings_view(chat_id)
    await safe_edit(cb, text, kb)
    await cb.answer("Готово ✅")


# ---------------------------------------------------------------- ПРОВЕРКИ ПРАВ
async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Админ чата в Telegram или владелец бота."""
    if user_id in OWNER_IDS:
        return True
    m = await bot.get_chat_member(chat_id, user_id)
    return m.status in ("creator", "administrator")


async def user_level(message: Message):
    uid = message.from_user.id
    if uid in OWNER_IDS:
        return "owner"
    m = await message.bot.get_chat_member(message.chat.id, uid)
    if m.status in ("creator", "administrator"):
        return "admin"
    if is_bot_admin(message.chat.id, uid):
        return "botadmin"
    return None


async def require_admin(message: Message) -> bool:
    if await is_chat_admin(message.bot, message.chat.id, message.from_user.id):
        return True
    await message.reply("⛔ Эта команда только для админов чата.")
    return False


async def require_mod(message: Message):
    """Мут/бан/кик: админы чата, админы бота и владелец. Возвращает уровень или None."""
    lvl = await user_level(message)
    if not lvl:
        await message.reply("⛔ Только для админов чата и админов бота.")
    return lvl


async def target_allowed(message: Message, t, lvl: str) -> bool:
    if t.id in OWNER_IDS and lvl != "owner":
        await message.reply("⛔ Нельзя применять это к владельцу бота.")
        return False
    if lvl == "botadmin":
        m = await message.bot.get_chat_member(message.chat.id, t.id)
        if m.status in ("creator", "administrator") or is_bot_admin(message.chat.id, t.id):
            await message.reply("⛔ Админ бота не может применять это к админам.")
            return False
    return True


def reply_target(message: Message):
    rep = message.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        return rep.from_user
    return None


# ---------------------------------------------------------------- ПЕРЕКЛЮЧАТЕЛИ КОМАНДАМИ
async def toggle(message: Message, key: str, value: bool, on_text: str, off_text: str):
    if not await require_admin(message):
        return
    set_setting(message.chat.id, key, value)
    await message.reply(on_text if value else off_text)


@dp.message(GROUPS, Command("monitoringon"))
async def cmd_monitoring_on(message: Message):
    await toggle(
        message, "monitoring", True,
        "🟢 Мониторинг включён: /server и /online снова работают в этом чате.",
        "",
    )


@dp.message(GROUPS, Command("monitoringoff", "monitoringonoff"))
async def cmd_monitoring_off(message: Message):
    await toggle(
        message, "monitoring", False,
        "",
        "🔕 Мониторинг выключен: /server и /online в этом чате не работают.",
    )


@dp.message(GROUPS, Command("mimicon"))
async def cmd_mimic_on(message: Message):
    await toggle(
        message, "mimic", True,
        "🤪 Режим кривляния включён! Теперь могу передразнивать и отвечать.",
        "",
    )


@dp.message(GROUPS, Command("mimicoff"))
async def cmd_mimic_off(message: Message):
    await toggle(
        message, "mimic", False,
        "",
        "😐 Режим кривляния выключен.",
    )


# ---------------------------------------------------------------- ПРЕФИКСЫ (только админы)
@dp.message(GROUPS, Command("giveprefix"))
async def cmd_giveprefix(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    t = reply_target(message)
    text = (command.args or "").strip()
    if not t or not text:
        await message.reply("Ответь на сообщение участника: /giveprefix 🔥 Легенда")
        return
    if len(text) > 20:
        await message.reply("Префикс слишком длинный (максимум 20 символов).")
        return
    set_prefix(message.chat.id, t, text)
    await message.reply(f"🏷 {t.full_name} получил префикс «{text}».")


@dp.message(GROUPS, Command("removeprefix"))
async def cmd_removeprefix(message: Message):
    if not await require_admin(message):
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /removeprefix")
        return
    set_prefix(message.chat.id, t, "")
    await message.reply(f"🏷 Префикс у {t.full_name} снят.")


@dp.message(GROUPS, Command("profile", "prefix"))
async def cmd_profile(message: Message):
    t = reply_target(message) or message.from_user
    await send_card(message, "profile", profile_text(message.chat.id, t), markup([HOME]))


# ---------------------------------------------------------------- МОДЕРАЦИЯ
def parse_duration(args):
    if not args:
        return timedelta(minutes=10)
    m = re.fullmatch(r"(\d+)\s*([mhdмчд]?)", args.strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "m"
    if n <= 0:
        return None
    if unit in ("h", "ч"):
        d = timedelta(hours=n)
    elif unit in ("d", "д"):
        d = timedelta(days=n)
    else:
        d = timedelta(minutes=n)
    return min(d, timedelta(days=365))


async def mod_error(message: Message, e: Exception):
    text = getattr(e, "message", str(e))
    await message.reply(
        f"❌ Не получилось: {text}\n"
        "Проверь, что бот — админ группы с правом ограничивать/банить участников, "
        "и что цель — не админ."
    )


@dp.message(GROUPS, Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    lvl = await require_mod(message)
    if not lvl:
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /mute 30m")
        return
    if not await target_allowed(message, t, lvl):
        return
    delta = parse_duration(command.args)
    if delta is None:
        await message.reply("Формат: /mute 30m, /mute 2h, /mute 1d (по умолчанию 10 минут)")
        return
    until = datetime.now(timezone.utc) + delta
    try:
        await message.bot.restrict_chat_member(
            message.chat.id,
            t.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    mins = int(delta.total_seconds() // 60)
    await message.reply(f"🔇 {t.full_name} в муте на {fmt_time(mins * 60)}.")


@dp.message(GROUPS, Command("unmute"))
async def cmd_unmute(message: Message):
    lvl = await require_mod(message)
    if not lvl:
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /unmute")
        return
    try:
        await message.bot.restrict_chat_member(
            message.chat.id,
            t.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    await message.reply(f"🔊 {t.full_name} размучен.")


@dp.message(GROUPS, Command("ban"))
async def cmd_ban(message: Message):
    lvl = await require_mod(message)
    if not lvl:
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /ban")
        return
    if not await target_allowed(message, t, lvl):
        return
    try:
        await message.bot.ban_chat_member(message.chat.id, t.id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    await message.reply(f"🔨 {t.full_name} забанен.")


@dp.message(GROUPS, Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    lvl = await require_mod(message)
    if not lvl:
        return
    t = reply_target(message)
    uid = t.id if t else None
    if uid is None and command.args and command.args.strip().isdigit():
        uid = int(command.args.strip())
    if uid is None:
        await message.reply("Ответь командой на сообщение или укажи ID: /unban 123456")
        return
    try:
        await message.bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    await message.reply("✅ Пользователь разбанен, может вернуться по ссылке.")


@dp.message(GROUPS, Command("kick"))
async def cmd_kick(message: Message):
    lvl = await require_mod(message)
    if not lvl:
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /kick")
        return
    if not await target_allowed(message, t, lvl):
        return
    try:
        await message.bot.ban_chat_member(message.chat.id, t.id)
        await message.bot.unban_chat_member(message.chat.id, t.id, only_if_banned=True)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    await message.reply(f"👢 {t.full_name} исключён.")


# ---------------------------------------------------------------- КРОКОДИЛ
WORDS = [
    "слон", "самолёт", "гитара", "компьютер", "бабочка", "холодильник",
    "велосипед", "динозавр", "пингвин", "вулкан", "зонтик", "робот",
    "футбол", "космонавт", "арбуз", "телефон", "паук", "корабль",
    "дракон", "кактус", "шоколад", "радуга", "пират", "снеговик",
    "фокусник", "жираф", "микроволновка", "маяк", "ниндзя", "верблюд",
    "парашют", "водопад", "утюг", "обезьяна", "мороженое", "лифт",
    "будильник", "крокодил", "пылесос", "рыцарь", "акула", "очки",
    "батут", "попугай", "танк", "ёлка", "пицца", "сантехник",
    "зомби", "бумеранг", "кенгуру", "лыжи", "скейтборд", "привидение",
]

croc: dict[int, dict] = {}         # chat_id -> состояние игры
croc_scores: dict[int, dict] = {}  # chat_id -> {user_id: (name, очки)}


def norm(s: str) -> str:
    return s.strip().lower().replace("ё", "е")


async def send_top(target: Message):
    scores = croc_scores.get(target.chat.id)
    if not scores:
        await target.answer("Пока нет очков. Сыграйте в /croc 🐊")
        return
    medals = ["🥇", "🥈", "🥉"]
    top = sorted(scores.values(), key=lambda x: x[1], reverse=True)[:10]
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} {html.escape(n)} — <b>{s}</b>"
        for i, (n, s) in enumerate(top)
    ]
    await send_card(
        target, "top", "🏆 <b>Топ крокодила</b>\n\n" + "\n".join(lines),
        markup([HOME]), reply=False,
    )


@dp.message(GROUPS, Command("top"))
async def cmd_top(message: Message):
    await send_top(message)
# ---------------------------------------------------------------- ИГРЫ (inline-кнопки)
XO_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
xo_games: dict[tuple, dict] = {}  # (chat_id, message_id) -> партия (в памяти)
CROC_TIMEOUT = 15 * 60  # секунд, потом игру можно начать заново


async def games_view(bot=None, chat_id=None):
    text = (
        "🎮 <b>Игры</b>\n\n"
        "❌⭕ <b>Крестики-нолики</b> — дуэль двух игроков\n"
        "🐊 <b>Крокодил</b> — один показывает слово, остальные угадывают\n\n"
        "Выбери игру 👇"
    )
    kb = markup([
        [btn("❌⭕ Крестики-нолики", "g:xo"), btn("🐊 Крокодил", "g:croc")],
        [btn("🏆 Топ крокодила", "m:top")],
        HOME,
    ])
    return text, kb


async def say(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        pass


# ---- крестики-нолики
def xo_result(b):
    for line in XO_LINES:
        a, c, d = line
        if b[a] and b[a] == b[c] == b[d]:
            return b[a], line
    return ("draw", None) if all(b) else (None, None)


def xo_name(g, sym) -> str:
    p = g["p"].get(sym)
    return html.escape(p[1]) if p else "—"


def xo_text(g) -> str:
    head = (
        "❌⭕ <b>Крестики-нолики</b>\n\n"
        f"❌ {xo_name(g, 'X')}\n⭕ {xo_name(g, 'O')}\n\n"
    )
    if len(g["p"]) < 2:
        return head + "Ждём соперника 👇"
    if g["win"] == "draw":
        return head + "🤝 <b>Ничья!</b>"
    if g["win"]:
        return head + f"🏆 Победил(а) <b>{xo_name(g, g['win'])}</b> 🔥"
    mark = "❌" if g["turn"] == "X" else "⭕"
    return head + f"Ход: {mark} <b>{xo_name(g, g['turn'])}</b>"


def xo_kb(g):
    if len(g["p"]) < 2:
        return markup([[btn("✅ Играть против " + g["p"]["X"][1][:20], "xo:join")]])
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            v = g["b"][i]
            label_ = "❌" if v == "X" else "⭕" if v == "O" else "▫️"
            if g["line"] and i in g["line"]:
                label_ = "🔥"
            row.append(btn(label_, f"xo:{i}"))
        rows.append(row)
    if g["win"]:
        rows.append([btn("🔄 Реванш", "xo:again")])
    return markup(rows)


async def start_xo(target: Message, user, reply: bool):
    g = {
        "b": [""] * 9, "p": {"X": [user.id, label(target.chat.id, user)]},
        "turn": "X", "start": "X", "win": None, "line": None,
    }
    send = target.reply if reply else target.answer
    sent = await send(xo_text(g), parse_mode="HTML", reply_markup=xo_kb(g))
    xo_games[(target.chat.id, sent.message_id)] = g
    while len(xo_games) > 200:  # не копим старые партии
        xo_games.pop(next(iter(xo_games)))


async def xo_render(cb: CallbackQuery, g: dict):
    try:
        await cb.message.edit_text(xo_text(g), parse_mode="HTML", reply_markup=xo_kb(g))
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@dp.message(GROUPS, Command("xo"))
async def cmd_xo(message: Message):
    await start_xo(message, message.from_user, reply=True)


@dp.callback_query(F.data == "g:xo")
async def cb_start_xo(cb: CallbackQuery):
    await start_xo(cb.message, cb.from_user, reply=False)
    await cb.answer("Игра создана 👇")


@dp.callback_query(F.data.startswith("xo:"))
async def cb_xo(cb: CallbackQuery):
    chat_id, mid = cb.message.chat.id, cb.message.message_id
    g = xo_games.get((chat_id, mid))
    if g is None:
        await cb.answer("Партия закончилась или бот перезапускался. Новая: /xo", show_alert=True)
        return
    act = cb.data[3:]
    u = cb.from_user
    ids = [p[0] for p in g["p"].values()]

    if act == "join":
        if u.id in ids:
            await cb.answer("Ты уже в игре — ждём соперника 🙂", show_alert=True)
            return
        if len(g["p"]) >= 2:
            await cb.answer("Место уже занято", show_alert=True)
            return
        g["p"]["O"] = [u.id, label(chat_id, u)]
        await xo_render(cb, g)
        await cb.answer("Игра началась!")
        return

    if len(g["p"]) < 2:
        await cb.answer("Ждём соперника…", show_alert=True)
        return

    if act == "again":
        if u.id not in ids:
            await cb.answer("Реванш — только для игроков", show_alert=True)
            return
        if not g["win"]:
            await cb.answer()
            return
        g["start"] = "O" if g["start"] == "X" else "X"
        g.update(b=[""] * 9, turn=g["start"], win=None, line=None)
        await xo_render(cb, g)
        await cb.answer("Реванш!")
        return

    if not act.isdigit() or not 0 <= int(act) < 9:
        await cb.answer()
        return
    if g["win"]:
        await cb.answer("Игра окончена. Нажми «Реванш»", show_alert=True)
        return
    sym = g["turn"]
    if g["p"][sym][0] != u.id:
        if u.id in ids:
            await cb.answer("Сейчас не твой ход ⏳")
        else:
            await cb.answer("Ты не в этой партии. Начни свою: /xo", show_alert=True)
        return
    i = int(act)
    if g["b"][i]:
        await cb.answer("Клетка занята", show_alert=True)
        return
    g["b"][i] = sym
    res, line = xo_result(g["b"])
    g["win"], g["line"] = res, line
    if not res:
        g["turn"] = "O" if sym == "X" else "X"
    await xo_render(cb, g)
    if res and res != "draw":
        await fire(cb.bot, chat_id, mid)
    await cb.answer()


# ---- крокодил
def croc_hint(word: str) -> str:
    return " ".join("▫️" for _ in word)


def croc_text(c: dict, note: str = "") -> str:
    head = "🐊 <b>Крокодил</b>\n\n"
    if note:
        head += note + "\n\n"
    if c["active"]:
        return (
            head + f"🎤 Ведущий: <b>{html.escape(c['leader'][1])}</b>\n"
            f"🔤 Слово из <b>{len(c['word'])}</b> букв:\n{croc_hint(c['word'])}\n\n"
            "Остальные пишут догадки прямо в чат 💬"
        )
    return head + "Кто покажет слово? Нажми кнопку 👇"


def croc_kb(c: dict):
    if c["active"]:
        return markup([
            [btn("👀 Показать слово", "cr:peek")],
            [btn("🔄 Другое слово", "cr:new"), btn("🏳️ Сдаться", "cr:give")],
        ])
    return markup([[btn("🙋 Я буду ведущим", "cr:lead")], [btn("🏆 Топ", "m:top")]])


async def start_croc(target: Message, reply: bool):
    chat_id = target.chat.id
    c = croc.get(chat_id)
    if c and c["active"] and time.time() - c["ts"] < CROC_TIMEOUT:
        await target.answer(
            f"🐊 Игра уже идёт, ведущий — <b>{html.escape(c['leader'][1])}</b>. "
            "Пишите догадки в чат!",
            parse_mode="HTML",
        )
        return
    c = {"active": False, "leader": None, "word": "", "msg": 0, "ts": time.time()}
    send = target.reply if reply else target.answer
    sent = await send(croc_text(c), parse_mode="HTML", reply_markup=croc_kb(c))
    c["msg"] = sent.message_id
    croc[chat_id] = c


async def croc_edit(bot: Bot, chat_id: int, c: dict, note: str = ""):
    try:
        await bot.edit_message_text(
            croc_text(c, note), chat_id=chat_id, message_id=c["msg"],
            parse_mode="HTML", reply_markup=croc_kb(c),
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.warning("croc edit: %s", e)


@dp.message(GROUPS, Command("croc"))
async def cmd_croc(message: Message):
    await start_croc(message, reply=True)


@dp.callback_query(F.data == "g:croc")
async def cb_start_croc(cb: CallbackQuery):
    await start_croc(cb.message, reply=False)
    await cb.answer("Игра создана 👇")


@dp.callback_query(F.data.startswith("cr:"))
async def cb_croc(cb: CallbackQuery):
    chat_id, mid = cb.message.chat.id, cb.message.message_id
    c = croc.get(chat_id)
    if not c or c["msg"] != mid:
        await cb.answer("Эта игра уже закончилась. Новая: /croc", show_alert=True)
        return
    act = cb.data[3:]
    u = cb.from_user

    if act == "lead":
        if c["active"]:
            await cb.answer("Ведущий уже есть", show_alert=True)
            return
        c.update(active=True, leader=[u.id, label(chat_id, u)],
                 word=random.choice(WORDS), ts=time.time())
        await croc_edit(cb.bot, chat_id, c)
        await cb.answer(f"Твоё слово: {c['word']}\n\nПокажи его без слов!", show_alert=True)
        return

    if not c["active"]:
        await cb.answer()
        return
    if c["leader"][0] != u.id:
        await cb.answer("Слово видит только ведущий 🤫", show_alert=True)
        return

    if act == "peek":
        await cb.answer(f"Твоё слово: {c['word']}", show_alert=True)
    elif act == "new":
        old = c["word"]
        while c["word"] == old:
            c["word"] = random.choice(WORDS)
        await croc_edit(cb.bot, chat_id, c)
        await cb.answer(f"Новое слово: {c['word']}", show_alert=True)
    elif act == "give":
        word = c["word"]
        c["active"] = False
        await croc_edit(cb.bot, chat_id, c, f"🏳️ Ведущий сдался. Слово было: <b>{html.escape(word)}</b>")
        await cb.answer()
    else:
        await cb.answer()


async def croc_guess(message: Message) -> bool:
    """Проверяет обычное сообщение как догадку. True — сообщение обработано."""
    c = croc.get(message.chat.id)
    if not c or not c["active"] or not message.text:
        return False
    if time.time() - c["ts"] > CROC_TIMEOUT:
        c["active"] = False
        await croc_edit(message.bot, message.chat.id, c, "⌛ Время вышло.")
        return False
    if norm(c["word"]) not in re.findall(r"\w+", norm(message.text)):
        return False
    u = message.from_user
    if u.id == c["leader"][0]:
        await message.reply("🙅 Ведущему нельзя говорить слово!")
        return True
    word = c["word"]
    c["active"] = False
    name = label(message.chat.id, u)
    sc = croc_scores.setdefault(message.chat.id, {})
    pts = sc.get(u.id, (name, 0))[1] + 1
    sc[u.id] = (name, pts)
    await croc_edit(
        message.bot, message.chat.id, c,
        f"🎉 <b>{html.escape(name)}</b> угадал(а) слово «{html.escape(word)}»!",
    )
    await message.reply(f"🎉 Верно! Слово «{word}». Очков у тебя: {pts} 🔥")
    await fire(message.bot, message.chat.id, message.message_id)
    return True


# ---------------------------------------------------------------- РЕЖИМ КРИВЛЯНИЯ
MIMIC_CHANCE = float(os.getenv("MIMIC_CHANCE", "0.3"))  # шанс ответить на сообщение
MIMIC_COOLDOWN = 4  # секунд между случайными ответами в одном чате
mimic_last: dict[int, float] = {}

VOWELS = "аеёиоуыэюяaeiou"
GREET = ["Привет!", "Здарова 👋", "Ну привет)", "О, кто пришёл!"]
HOWARE = ["Нормально, а у тебя?", "Живу, не жалуюсь", "Лучше всех!", "Бывало и лучше 😅"]
YESNO = [
    "Да", "Нет", "Наверное", "Конечно!", "Даже не думай",
    "Хм... 50 на 50", "Однозначно нет", "Скорее да, чем нет",
]
THANKS = ["Обращайся!", "Пожалуйста 😌"]


def kover(text: str) -> str:
    """Коверкает чужой текст случайным способом."""
    text = text.strip()
    words = text.split()
    if not words:
        return text
    mode = random.choice(["spongebob", "baby", "stretch", "echo", "shout", "reverse"])
    if mode == "spongebob":
        out, up = [], False
        for ch in text:
            if ch.isalpha():
                out.append(ch.upper() if up else ch.lower())
                up = not up
            else:
                out.append(ch)
        return "".join(out)
    if mode == "baby":
        table = str.maketrans("рРшШжЖлЛ", "лЛсСзЗвВ")
        return text.translate(table) + " 🥺"
    if mode == "stretch":
        w = words[-1]
        words[-1] = "".join(
            ch * random.randint(2, 4) if ch.lower() in VOWELS else ch for ch in w
        )
        return " ".join(words) + "~"
    if mode == "echo":
        last = words[-1]
        return f"{last}... {last}... {last}..."
    if mode == "shout":
        return text.upper() + "!!!"
    return " ".join(reversed(words))


def try_answer(text: str):
    """Простые ответы по правилам. Если не подошло — None."""
    t = text.lower().strip()
    if re.search(r"\b(привет|здаров|здравствуй|хай)\b", t):
        return random.choice(GREET)
    if "как дела" in t or "как ты" in t:
        return random.choice(HOWARE)
    if re.search(r"\b(спасибо|спс|благодарю)\b", t):
        return random.choice(THANKS)
    if t.endswith("?"):
        return random.choice(YESNO)
    return None


async def maybe_mimic(message: Message):
    chat_id = message.chat.id
    if not get_setting(chat_id, "mimic"):
        return
    text = (message.text or "").strip()
    if not text or len(text) > 200 or re.fullmatch(r"https?://\S+", text):
        return

    me = await message.bot.me()
    mention = f"@{me.username}".lower() if me.username else None
    replied_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == me.id
    )
    direct = replied_to_bot or bool(mention and mention in text.lower())

    now = time.time()
    if not direct:
        if random.random() > MIMIC_CHANCE:
            return
        if now - mimic_last.get(chat_id, 0) < MIMIC_COOLDOWN:
            return
    mimic_last[chat_id] = now

    if mention:
        text = re.sub(re.escape(mention), "", text, flags=re.I).strip()
    if not text:
        await message.reply("Чего?")
        return

    reply = try_answer(text) or kover(text)
    await message.reply(reply[:300])


# ---------------------------------------------------------------- ТЕКСТ В ГРУППЕ
# Ловит обычные сообщения (не команды): ответы в крокодиле и кривляние
@dp.message(GROUPS, F.text, ~F.text.startswith("/"))
async def group_text(message: Message):
    chat_id = message.chat.id
    u = message.from_user
    if u is None or u.is_bot:
        return

    if await croc_guess(message):
        return
    await maybe_mimic(message)


# ---------------------------------------------------------------- ЗАПУСК
async def health(_request):
    return web.Response(text="ok")


async def setup_commands(bot: Bot):
    """Список команд, который Telegram показывает при вводе «/»."""
    common = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="xo", description="Крестики-нолики"),
        BotCommand(command="croc", description="Крокодил"),
        BotCommand(command="top", description="Топ крокодила"),
        BotCommand(command="server", description="Статус сервера"),
        BotCommand(command="online", description="Онлайн по серверам"),
        BotCommand(command="find", description="Найти игрока по нику"),
        BotCommand(command="rules", description="Правила сервера AnMine"),
        BotCommand(command="priv", description="Привилегии AnMine"),
        BotCommand(command="profile", description="Профиль и префикс"),
        BotCommand(command="ping", description="Скорость ответа бота"),
    ]
    admin = common + [
        BotCommand(command="mute", description="Мут (ответом на сообщение)"),
        BotCommand(command="unmute", description="Снять мут"),
        BotCommand(command="ban", description="Бан"),
        BotCommand(command="unban", description="Разбан"),
        BotCommand(command="kick", description="Исключить"),
        BotCommand(command="giveprefix", description="Выдать префикс"),
        BotCommand(command="removeprefix", description="Снять префикс"),
        BotCommand(command="monitoringon", description="Включить мониторинг"),
        BotCommand(command="monitoringoff", description="Выключить мониторинг"),
        BotCommand(command="mimicon", description="Включить кривляние"),
        BotCommand(command="mimicoff", description="Выключить кривляние"),
        BotCommand(command="addadmin", description="Назначить админа бота"),
        BotCommand(command="deladmin", description="Снять админа бота"),
        BotCommand(command="admins", description="Список админов бота"),
        BotCommand(command="disable", description="Отключить команду в чате"),
        BotCommand(command="enable", description="Включить команду в чате"),
        BotCommand(command="disabled", description="Отключённые команды"),
    ]
    owner = [
        BotCommand(command="owner", description="Команды владельца"),
        BotCommand(command="groups", description="Список групп"),
        BotCommand(command="blockchat", description="Запретить бота в группе"),
        BotCommand(command="unblockchat", description="Разрешить бота в группе"),
        BotCommand(command="say", description="Написать от имени бота"),
        BotCommand(command="disable", description="Отключить команду в группе"),
        BotCommand(command="enable", description="Включить команду в группе"),
        BotCommand(command="disabled", description="Отключённые команды"),
        BotCommand(command="blockuser", description="Заблокировать человека"),
        BotCommand(command="unblockuser", description="Разблокировать человека"),
        BotCommand(command="blockedusers", description="Список заблокированных людей"),
        BotCommand(command="restart", description="Перезапустить бота"),
        BotCommand(command="messages", description="Что пишут боту"),
        BotCommand(command="logs", description="Логи бота"),
        BotCommand(command="myid", description="Мой ID"),
    ]
    try:
        await bot.set_my_commands(common, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(admin, scope=BotCommandScopeAllChatAdministrators())
        await bot.set_my_commands([BotCommand(command="start", description="О боте")])
        for oid in OWNER_IDS:
            try:
                await bot.set_my_commands(owner, scope=BotCommandScopeChat(chat_id=oid))
            except Exception:
                pass
    except Exception:
        logging.exception("не удалось установить список команд")


async def main():
    bot = Bot(TOKEN)

    # мини-сервер, чтобы бесплатный хостинг видел открытый порт
    app = web.Application()
    app.router.add_get("/", health)
    app["bot"] = bot
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    await setup_commands(bot)
    if os.getenv("NOTIFY_START", "1") != "0":
        await notify_owners(bot, "🟢 Бот запущен")
    asyncio.create_task(keepalive_loop())
    await dp.start_polling(bot)


# ---------------------------------------------------------------- ВСТРОЕННЫЕ КАРТИНКИ (PNG в base64)
BANNER_DATA = {
    "find": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////+//79/f3l6vC6u8DwmEr8kjz7kjzxkkTfhTyFZ1V6"
        "TjF0Si9rTj5uSDFtRzBqRzFqRjBpRS9oRTBnRC9mRDBkQzBjQi9jQCxgQS9gQC9fQC9ePy9dPy9cPy9dPi5bPi9aPS9YPTBYPC5W"
        "Oy5UOi5UOSxTOi5SOS5ROC1QOC5POC5PNy1ONy1NNy5ONixMNi1MNSxLNS1KNS5JNCxINC1IMyxFNTFGMy5HMyxDMy9FMixDMi5D"
        "MSxCMSxAMC0/Lys+Liw9Liw8Liw7Li07LS06LS07LSs3LS87LCk5LC05Kik4LC04Kik3LC03Ky03KSg2Ky42Kyw2Kyo1Kis0Kiw0"
        "KSoyKS0yKCswKCwvJysuJissJiwtJSorJSwqJSwrJCkpJCwpIykoJCsoIyonIysnIikmIysmIislIismIikkIislISkjISsiISsj"
        "IColHSEhICsgICsgHyofHysfHyoeHyseHiodHysdHiscHisdHiocHSobHSoaHSobHCgaHCkZHCsZHCoZHCkZGygYHCoYGyoXGyoW"
        "GyoZGiYWGioWGikWGSgVGioVGSgUGioUGSoUGSkTGSoTGSkSGSoVGCYSGCoRGCoTFycQFyoQFykbEhgPFyoPFykOFyoJFCko2EiS"
        "AABP/ElEQVR42u29iVsbV5bwbUAPtgSSkNCCEIuEJCQhVrGZVYCdmMV2kDchYWKC99hxcCedTDp2dybTY/cXj7vT6XmSt1/n7f5X"
        "v7svVae0AAnY0YmDpFItt6p+Ots999ap01Saw//+35/r8rbK/4Pk/8LyDyb/h8tf/y7kr2aR34kN+B4sDgA2xqrlp+oA/sb5+2sZ"
        "+IwMGhE8AgLrAL6rANaG318rSU1a8GAA/vDPuryl8n8riKBGcKfL/6jyCgn9+0pdzEFkr//HuM9KbbBo+skH8Ccmx7mHE3QZoNOo"
        "kr+K+GnAmRcaNjsaAk88gP8Scnx7OOHX4XD8/U8tAhLI93wgAk86gD9/dvsukduf/XzAPXx++z6R25///Pbqv389Kt0kUnr0r58O"
        "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"
        "QHR1/n/1HFeVDeeI4AAAAABJRU5ErkJggg=="
    ),
    "games": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////z9vi/xtOnk+eoi/qni/uni/qmi/qhi+eWg9lcVI5U"
        "S4hPSYFNRn9LRXxLRHxKRHpJQ3pIQnhHQndHQXZGQXZFQHREQHNEP3NDP3JCPnBCPW9BPW1APG5APGw/PGw/O2w+O2s+O2o+Omo9"
        "Omk8Omg8OWg8OGc7OGY6N2U5N2Q4NmM4NmI4NmE4NWI3NWE3NWA3NGA2NWA2NF81NF81NF01M140M100M1wzMlwyMlsyMloyMVox"
        "MVkxMFgwMFcvL1YuL1UuLlQtLVMsLVErLFAqLFAqK1AqK08pK04pKk4pKk0oKk0oKkwnKUwoKUsnKUsmKUomKEomKEklKEkmJ0sm"
        "J0glKEglJ0glJkYkKEckJ0gkJ0cjJ0ckJ0YjJkYjJkUiJUQhJUMhJEQgJEIgI0EfI0AfIUMfIj8eIz8eIj8eIj4eIT0dIj4dIT0c"
        "ITwcIDwbIDsbIDoaHzoaHzkaHzgZHzgZHjkZHjcYHzcYHjcYHjYYHTYXHjYXHTYXHTUXHTQXHDQWHTQWHDQWHDMVHDMWHDIVGzIV"
        "GzEUGzIUGzEUGzATGzEUGjETGjATGi8SGS8TGS4SGi4SGS4SGS0SGC0RGS0RGC0QGCwQGCsQFysQFyoPFyoRFykOFyoREySbOQ1S"
        "AABS/0lEQVR42u29i0MbR7LwG7ROsGSBeb8kgUBCbxBPAeYhhITBso3BNsE2XATmYa8dO7aTDblJPic5zu6e5FwnzpfdOMnmxP/q"
        "7ap+zkwLJB4O2KrNhmjU09Mz81N1VXV19zvvoZSfann18+8leTPlP0J+I/IrlZdM/o3yrz2Elvo3O4dVAbXJuvfXtndKAL7N/P1b"
        "g9+PUjQMWgg8IIIlAN8e/P5jUX9G+n7MI7sh+OtBCSwB+NYAKPH7VUNfXvyMmlCrBQ9CoArgbyU5ufL77+z/9I9WfvnlpRQg6Scq"
        "P/74w57y44+ssGoLEvnll7wtko35PX+7FQB/OoLH8oeQk/xyT8BdoIL7D/79z2+/8+YaX7ww+agI/WYA7blBjAxS7qAqpRp2RNcm"
        "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"
    ),
    "help": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////+///+/v719/n66Wv+4kf+4Ef94Ef84Ef43lDNy6yd"
        "m3p3cUBzbDZyajVyajNuZzVsZjRqZDlqZDNnYTVnYTJkXzRhXTRfWjNaWUFdWTNcWDNbVzNjXTFdWDFZVTNYVTJVUjJXUzBSUDRQ"
        "TjFPTTFQTi9OTDNNTDFMSzFLSjFOTC9MSS9JSDJIRzBJSC5GRTBERDBGRS9CQjFBQTA/QC9HRS5FRC5EQy5BQS09PjM+Py88PS8+"
        "Pi4/Pi09PS07PC86Oy86Oy05Oi44Oi44OCs3OTA3OS43OC43OCw2OC41NzE1Ny40Ny42Nyw1Nis0Ni40NSkzNi4yNTAyNS4xNC8x"
        "NC4wNDEyNCwwMy4vMi0xMyotMS0uMCosLywrLy0qLiwpLS0qLSooLC0nKywnKyomKywlKiwlKSwkKSwjKSwkKCwjKCwiKCwiJywh"
        "JywkKSokKCghJiwhJiggJSsfJSweJCsfJCkeIysdIysdIiscIywcIiwcIiscISsdIigbIisbISsaISsaICsZHysYHysaICkYHisX"
        "HisYHigXHSkWHSoWHCkVHCoVHCgUHCoUGyoTGyoUGykUGigTGioTGikSGioSGSoRGSoRGCoQGCoRFykPFyoPFykOFykREiMI4Tb8"
        "AABCKElEQVR42u2di18Td7r/XfJyC0kQMAQCJiEXEgMJiQjhLkQg9qCulnpBIjdR6u5vz69bGns9p0u77W5rb9vW3ardXf1Xz/d+"
        "mXkmmQlBwebTCswl35nJvPN5nu9tcuw1oubjgWc//7uho6r/QPoX0y9ET58+JUtP/mnUo0c/UX0Pim189OiR8YVPnvzrX6jQp0/p"
        "EfjxwJOxOvNjDQB/FfwR/ND/T55Y0WeBn84gQOC/MIGUwVoIbAD4qgKo8ocJeQrZH8fv+6qyQPCfGoG/VCCwOoD/+KWhI6p/WYuw"
        "9/jxY/y3Ch4Vo+tbo77+2rTqW7orf+UjBUFU+OPHTwXlsCxOXQHwR5tX+/QJ09P6vosHVe6vU0+Z8Bv6+DHi5LGCn2Z7jLgqkgjy"
        "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"
        "AABJRU5ErkJggg=="
    ),
    "menu": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX///////7+///9/f3z9PWS5bVK34FK3oBK3YBJ3H+dybdj"
        "joAwcFUnak0pZ04mZ0wmZUslZUsuYVAlZEolY0olYUokYkkkYEkkXkgjX0gjXUcjW0ciW0YiWkYiWUYiWEUhV0UhVkUfVkMkVEYg"
        "VEMgU0MgUkMgUEMfUUIfUEEfT0EfTkEeTUEdTT8jTEQeTEEeS0AeSkAdSj8hSUEdST8dSD8dRz8gQkAcSD0cRj0cRT0cRD0bQzwb"
        "QjwbQTwaQTobQDwaQDoaPzobPTsaPjoaPDoZPTkfOj0ZOzkZOjkZOTkYOjcYOTgYODkYODgYODYaNDkYNzkYNzgYNzYYNjgYNjcY"
        "NjUYNDcXNzYXNTcXNTUXNDYXNDQTNjQXMzYXMzQUMzMXMTYWMTQWMDQVMDMXLjQVLjMVLDMULDEVKzMUKzEUKjIUKjAUKTIUKTEU"
        "KDERLDATKDATJzETJy8QJy4TJjETJjATJi4TJTASJS4SJC8SJC4SIy8SIy0SIi8SIi4SIi0SIS4RIS0RIC4RIC0RHy0RHywRHi0R"
        "HiwMISoRHSwQHS0QHSwQHSsQHCwQHCsQGywQGysQGyoQGisOGioQGSoPGSsPGSoPGCsPGCoPGCkKGCcPFyoPFykNFykMEydRDl7p"
        "AABNfElEQVR42u2diV8bV5aojeGX6Ug4YFYt7AhJSAIEEfsmA2KxGFsTx4F2IhuxehtHnuilO1t3x92dCa/TS173m7yMM+l/9d17"
        "7l51SguLY4hOZtyUqnTrVtWnc+5Z7q0r/wLy5htd//z7D1W5qPI/xeS/DflOyT+E/J8y5O/yaNWA2XLRTjj1/EoVwJ8pf5Knv/+f"
        "MqUkgichsArgZQfQQf1x+P5u4PdXREwG/35iAqsA/jwBLMqfof3+6izaUcWV4CkA9P/z2++rcgbynfE/8lMu5Xxasfx3Efmu6JhP"
        "Mfbn4oKwaEfROig0Be/7BQXwf37k8j+v29nwY3+UUvrTs+Tvu6L8lUufieBfrQSWi+AlAvDHXx8+Ajn89Y/nr9J+/HB3D2T3wx+/"
        "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"
        "ALxhDAGHhlgpPlsJgfLH1hmiDghdmYUB2NbW1tJZjwohrLW1tcheIgJAqgR9bOUiRiCbIAcLtg0NKRu8uKgXxQCC2yBlAbi7uzbJ"
        "ZM2+a2ty7X7huLCbuW8/XNzGdwt5tnKBm+CXkbszogVD1naPjwv3t3YtOx226RN8t3C8plZG2D8+fNfs4ZbaTw6opwnBw/uVXKil"
        "w3BJu8f5YX2xB3oP7uONOG1n5E/keFeujFC/dlzYyu2eG4AeCWBbqw9bDtVH8aPitLcIgD2nAPDDrZyz7IuKIPuu3cP9XTJU2sIO"
        "3xUfZLYKPFdFBlXW3buWoiM69tKaN8+8i/Ukk9kvqGxYxnYl+v7jQn4rk9mq6EJt10Nki8aeVQ5uX+u0tRGn7V3VVk62Vdg3OifJ"
        "4/KByoKoVxJqYWg6G5gAGFKFWARAFoHh8RdugKm0tCLSIj4ttlcYYY9MynEAe7XpcWyCJlu0V4UC6crRLBpNgzFlAigTTui++06H"
        "GwwwwXZvmVK8LYejMhn9DPZOqv2ZYheKN+9w9Y6Nlugz1pxT/88TQJ/nWsM1izRc8/gZY0X3ngeAd+9X5SeWXI4gZgEwt6vD98E9"
        "mQXeTKt3cqnZcNpszIAoQ+iUJQjtUghEnnqH9a+6Gommc97bIgHkDIqaBM0NUbFoFYgRL7CBaDSEo4lUAXztAcw9yJ0HgC2+esf1"
        "r/yNlD/HvS1VACuS97m8/n0oBiCD7957dgCXygfQo2lAj/P6V/WelqJ7ywZwtBIA2ZDk8vG3K8Npr30fHEww/XRXFqNaALQk4uLx"
        "kfIALJps87cWTdS1tJcFYJwDOFMOgPW3Zufn5+dWHrx/ueTe7niArs8YDAXGd++95n0ATfcAkXu7U3T5KSKBqQ+2IQkiXgtsqwUU"
        "xfgiCt0JaTifz8sB5FGW4gC2FNvb2QgttAkAIR8CwWhVkBDmNYFjU5xAFohhkRgWikmbACbpVLr43OUDMNDQ0kikpSHw0wFI+tBM"
        "pFQfigA40NgG0hh4UBTAMShGLQZg+/kACPPjtPnBkUhMFaXymSFFAVyKJxKJsfnLB2CwBW55e0vwpwMwyI1WiT4UATDcxkIebeFi"
        "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"
    ),
    "online": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////+/v7q7fFN2vEi1fAi0+8i0+4i0+0i0ewhy+ZYobMb"
        "bYMkZXoWZnwWY3kVZHkiYXUWYngWYXcWYHYVYHYpVmoWXXMWWm8VXnMVXHEVW3AVWW4VV2wVVWsVVGkVU2gVUWYUWW4UVmsUVWoU"
        "UmgUUWYUUGUSUWYaTmIUT2QUTWIUS2ATTGETSl8TSl4TSV4TSV0TSF0TSFwZRloTR1wTRlsTRVoTRFkcPlETQ1gTQlcTQFUSSV0S"
        "SFwMS2ASRVkSQlYLRFkSQFQSP1MSPlIRP1MSPVESO08SOk8SOk4SOk0SOU0RPE8ROU0NO08VNkoSOEwSN0sSNksSNkoSNkkSNUkR"
        "N0oRNUkTM0cRNEgRM0cRMkYRMUUTL0MRMEQRL0MRLkIPM0YQL0IRLUERLEAQLT8QKz8QKz4QKj4QKj0QKT0QKTwQKDwQJzsQJjoN"
        "KTwQJTkQJTgPJTgPJTcQJDcPJDYQIzYPIzUQIjUPIjUPIjQQITUPITQQIDQPIDQPIDMPIDIPHzMPHzIPHzEPHjIPHjEPHjACIjgP"
        "HTEPHTAPHS8PHDAPHC8PHC4PGy8PGy4PGy0PGi4PGi0PGiwFGzAPGS0PGSwPGSsPGCwPGCsPGCoHGC0PFysPFyoPFykOECM1PFRe"
        "AABOd0lEQVR42u29iV9TV/7/r3lQlQRQtuwLkAWzE0xA1rAJRRGwirKJAoobinastct3+qmfWrR16kxnpv30+/t+Ov/q77zPfu49"
        "NwS3As1rppLl5OYm95n3drZDR7COVQX+8/P/VrS/9Tv89zv8X9X/lfTbb79y/fJPST//3Vpyu1/E63/77TfpyMaToSfye+lzPlQB"
        "8GAxCJfdGsDfJP5k/H7+uRR/f0dPywj+oiXQdCa6U6kAeKAN4O+/awyghfmTiPp7WZJe8KtkBS1tIJwHJvD3CoB/HttHrvfvFuZP"
        "h59q+v5mkGoIdQhaGUF6JjsRyAH0/+f1bxXtXwFgiqtlkkK+XzTUvRaw/WAhHY/CJ/+iBoXyCbFzUU9I1Z4HUPxWKpDtwN9vGv5+"
        "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"
        "AAAASUVORK5CYII="
    ),
    "ping": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX+/v6N4Mc01Zo005o005l0uKgsrIIlb2AealkgZlkdZFcd"
        "YlYdYVYdYFUdX1QdXlQcX1QhW1QcXVMcXFMcW1IcWlEcWFAbW1EbWVAbWE8bV08bVU4bVE4aVk4aVE0aU0wZUksaUEsZUEoeTEsZ"
        "TkkZS0gYTEgYSkgYSkcYSUgYSUYYSEgYSEcYSEYYR0YXSEUfRUkYRkYYRUUXRkQXRUQXREQXQ0QXQ0MdQkYXQkMXQUMYPkIWQkIW"
        "QEEWP0EWPkAWPUAWPD8VOz4TOz0VOj4VOT4VOD0UOTwgN0EVNz0VNj0VNjwVNjsVNTsUNjsUNTwUNTsUNDwUNDsUNDkWMjsUMzsU"
        "MzoUMzkUMjoUMjkWMDoUMTkUMDgSMzgTMDgNMDUTLzgVLjgTLjcTLTcTLDYSLTUTKzYSKzUSKjUUKTUSKTUSKTMSKDQSJzMSJzIS"
        "JjMRKDIRJjIMKzMSJTMRJTMRJTIRJTESJDIRJDIRJDERJDARIzERIzARIjERIjARIi8RITAQIy8QIS8QIC8QIC4QHy4QHy0QHi4Q"
        "Hi0LHywQHS0PHSwQHC0PHC0PHCwQGywPGywPGysQGiwPGiwPGisDHCgPGSsPGSoPGCsPGCoPGCkKGCgPFyoPFykOFyoNEyd6bczE"
        "AABFn0lEQVR42u2diV9b17Xvja9BICQBGtCERiQhURAGCdsgARIWKM6guBnt2AIPOOZ5aBz66neb5jY3Ux2nvhluk6bpp27+1bfn"
        "4Zx1hITBAaxfG4OOjo6mL2vttfZae584RXUy9Mv//quro6qnLfVPVT//JPWDqu+tpZ6mPPxn7cKtX4PVKz/RBfDY8/fUgr8f2uXv"
        "e/1MCwKf7onALoDHHkDNTIH4fd+WYCvYNoJdAF9QADvD7xuDdkewXQJ3B/Dbn7s6ovpnC/0Ejvkg5P5qIQhH0Br+1Op1/BN+6S8A"
        "gE9/YXp6DN8b7Ghh/H4y42dFnyTNksHOEdRe7wsD4E9Pt197h+i17ac/HTf+/nT7DtH2x0/3YP4sTd83dzeY7n5vMoRmBNsl8Ken"
        "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"
    ),
    "priv": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////+///9/f3m6/DIxtT/rvLfrt78e8H1crf0craxja3q"
        "brCMTHh0QmlmQmRqO2FpOmBoOmBnOV9mOF5lOF5iOF1jN1xgN1thNltfNlpeNlpcNVlcNFdaNFdaM1ZXM1ZXMlRSMlNVMVNSMVJU"
        "L1JPMFFPL09OL1BOLk5NMFBNL09ML09ML05MLk5MLE1LL05LLk5LLk1KLk5KLUxJLU1JLUxILUxILEtELktGLEtFLEtFK0lCK0lC"
        "KkhAKkdBKUY/KUY+KEU7KkY8KEQ7KEQ5KEQ8J0M6J0Q5J0M4J0Q4J0M4J0I1J0E5JkI3JkI5JEE2JkI2JkE2JkA2JD81JkE0JUE1"
        "JT8zJUA0JD4xJD8xIz0uIz0uIjssIjsrIjssITosHzkqIjoqITopIToqITkoITkpIDgoIDknIDgoHzcmIDglHzckHzckHzYlHjUj"
        "HjYiHjUhHjUjGzMgHjQgHTQfHTQfHTMeHTMfHDIdHTMdHDMdHDIdHDEcHDIbHDEdGzAbGzEbGi8aGzAZGzAaGy8ZGi4YGzAYGjAY"
        "Gi8YGi4XGy8XGi8WGi4YGS0WGS4WGS0WGCwVGS0VGCwUGS0UGCwTGSwTGCwSGCwSGCsRGCsRFysQFyoPFyoPFykOFykTEyfoBuSR"
        "AABQQklEQVR42u2diUNTVxb/FWLBkIWE7AnZEzbDFiACYQmLOojSCoohKExrVUQRU0edznSsXX7t/GxnprVj+2s79l/93f3d+959"
        "WSAUpPm2Anlb3sv75Nxzzj33vhOnsZr8b378ta63Vf/73//wT0G/8PpZq5+IfqhIdGvJgYQ3+kV1Fuz0pDpRB/B48CcBsDR/P0nx"
        "+14tGYI/VUXgryUJrAN4XA3gL2Xsnwa/7/VVgRUsawPrAB5nAKttfzXWj7H2naAqECxHYHkAX/+ilp6JPQ46qGs7ep+ZvtmjdP0H"
        "69+KvmUCL8jq/6hY1JK4l7MrCeAbpuMH4EFd25H7zPTs3vcCfiruvv+JEPb9t3gNTyBlsCYElgDw5zcfzK+8C7Qy/8GbY2YDf35z"
        "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"
        "FWVP1EyN/2oAAAAASUVORK5CYII="
    ),
    "profile": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX///////7+///+/v73+Pp93NUt1sEt1MAt1L8t1L6jsbov"
        "xbQ6dnobaGkaZ2cdY2YaYmUaYGMZYmQyWGEaXmIaW2AZXWEZW18ZWl4ZWV0ZVlwYWV0YV1sYVVoYVFkYUVcXU1gXUVcXUFYXT1Uf"
        "TFUXTlUXTVQXTFMXSVEWTVMWS1IWSlIWSVEWSFAWSE8WR08WRk8VR08cPkoWRU4WQ00VRE0VQ0wVQ0sVQksVQUsVQEoVP0kVPUgU"
        "QkoUPkgUPUcUPEYUO0YUOkUUOUQTOkQcNkQUOEQUN0MTOEMTN0ITNkMTNkITNkETNUITNUETNUAVMj8TNEETNEATM0ATMz8TMkAT"
        "Mj8TMj4TMT4RNUARMT0SMD0ULjwSLz0SLjwSLTsSLDoRLToUKzoSKzkRKjgSKTgRKTgRKTcRKDcRKDYSJzYRJzcRJzYRJzURJjYR"
        "JjUOKTYRJTUQJTUQJTQRJDQQJDMQIzMQIzIQIjMQIjIRITIQITIQITEQIDIQIDEQIDAQHzEQHzAQHy8QHjAQHi8QHi4PIC8FIjAQ"
        "HS8PHS4QHC4PHC4PHC0QGy4PGy4PGy0PGywQGi0PGi0PGiwPGisCHCwPGSwPGSsPGSoPGCsPGCoAGSgPFyoPFykLFykNEybO8Pic"
        "AABLWklEQVR42u2diVtbR5qvc9s8TiwEBiOEJLQLSWgDhNglFglsw+AFx46DWK1AnGTG6ag7sXtyk053pj1N0pOezp3E7e4kfuZf"
        "vfXVXufU0YIBA9YvecBaTp2jo5dvqfqq6o23sC5d9PzvD7+0dFb14sUL8lOrnxT9g+n58x9lfW8t+W3Pnz9nDajtWp2eXJ/Vlb/R"
        "AvDc8GdJgJa/xvEzEPijBYG1ALQmsAXg+QCwBn/18fu+IWkRVBh8UcsGtgA8/1awLn/C+1rh951B9RFs2AjWBfD7n1o6k/on/t9K"
        "/5Cko+67JqSDUW7/EBdfE0CrMPZs6Rd9LHQuVPcbahq/b1VZI8gbExdxGAQlAP/2T1XPf3n/7jrW3fd/ef7Ps6pfPv9Xqs9/+ef5"
        "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"
    ),
    "rules": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX///////7+///9/f3t46Dy0kPKzMz6zBX4zBXzySO6rG+H"
        "fEh1ZylzZCFqXiRuYCFrXiFqXSFwYR5sXh9pXCFnWyJpWx9nWiBiWzNlWSJjWCJhVyNlWSBiViBdUyReUyFfVB9aUCNYTyNXTiNc"
        "USBYTyFVTSNUTCNTSyNVTCBQSSVRSSFNRyRORyFIRCpLRCRIQyRKRCNKRCJLRCBHQiRGQSREQCZHQSJFQCJDPyVDPiRAPCVDPiJA"
        "OyI9OyY7OSU9OiM6OCY6OCU5OCY7OCM5NyY4NyY6NyM3Nik4NiY3NiY3NSY2NSg2NiY2NSY1NSY1NCY0MyY4NSM2NCQ0MiIyMiYx"
        "MSUwMCYwMCQvLyYtLicsLScrLSctLSQqLCcqKycpLCcpKycpKicoKycoKicnKicqKyQnKScmKScnKCMlKCckJygkJyYjJigiJigj"
        "JiUhJSgiJSQgJCgfIygeIigdIigfIyYjIxodISgcISkcISgcICgeICQbISgbICgaICgbICcaHygaHyYZHykZHikYHikXHikXHSkW"
        "HSkYHScWHCkVHCkVGykUGykUGikVGygWGyYTGykTGikSGikTGigSGSkSGScRGSkRGCkQGCkPGCkPFyoPFykOFykTFyYNDyRw+7Bd"
        "AABCpklEQVR42u3diVvbVrow8PDxPA5YQCBsBmyw8YJ3O5jNMRjSksDkMhns2BhISG/blASy3DTLkJuZSZt2ejtN1+m0t+3cpv1X"
        "v7PoSOccvbJlsFkSv9MJWJa1WD/es+hIOnWaRMvpkd//9WsjXqv4RYj/NY3vLIb5EsQ1VbWRpxoA3wh/B+dXBcEGwEZY9fddlWGN"
        "YANgIyz5+24fUWOBHMDvf2lENfGzFpWn/gJOrPvW/YaCI/LbAe2BCoUV4KhuNzmA/2yYqipeabHfqfX+4zAkqR9QlLP3BRDlFP5A"
        "FyhGdX9pDYD7jJ9eXZ+/fAnF5fnrr37Sp+6ubZBY29Wn/vzq/uZ1Epv3X/18pP7Mc98XZaIswf89kEAO4Nc/N8J6/PhqfixMYmz+"
        "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"
    ),
    "server": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////6+/usw99jqv6Kpclgpftgpfpgpfk1u49io/VbhLo/"
        "YI4zU4IwUYAwT3wuT3wuTXotTXotTHguSnUsSnUrSXQrSHIqSHMqR3EpRnApRW4oRG0oQ2snQmonQWcmQGcmP2UlP2QlPWIkPmMk"
        "PWMkPWIkPGEkPGAjPGElO14jO2AjO18jO14jOl4iO18iOV0iOVshOV0hOFshOFofOFwjNlYhN1ohNlggNlggNVcgNFUfNVYfNFUf"
        "M1MeM1MeMlIeMVEcNFgdMVEdMFAdME8dME4cME8dL00cL04cL00cLk0cLkwcLksbLkwcLUsbLUsbLUobLEkbK0caLEkaK0gaK0ca"
        "KkcZKkcZKUUZKUQZKEQYKUQYKEMYKEIVKEoZJ0MYJ0MYJ0IYJ0EYJkEXJkEXJkAXJUAXJT8XJT4WJT4XJD4WJD4WJD0WJDwWIzwV"
        "Iz0VIjsVIjoVIjkTIjwVITkUITkUIDkUIDgUIDcUHzcTIDcTHzcTHzYTHzUZHTATHjUTHjQTHTMSHjQSHTQSHTMSHTISHDISHDEQ"
        "HTMTGzARGzERGzARGy8RGi8RGi4RGS0QGy8QGi4QGi0QGS4QGS0QGSwMGjMQGCwPGCwPGCsPGCoPFysPFyoPFykLFy4REiG5fzVt"
        "AABTtUlEQVR42u2diV9bR7LvgyzHgwQChFi0I5CEjBDCYHYEmE3sIMCAl3C9YBMbYuzIfk5i52UmyTifcSbvJs8eJsm1J//q66pe"
        "Th+dFkgGFOOn32SMlqOz6HxV3V1VXf3Rx6hzZ5x/vH5T1Aeht2+zvPH777+h/s30L6bXQv/XKPEe35p/mu7r99+znkZOJ/tREcAP"
        "C74c+PtNjR8n7hdNGRBmIMj29vu7nEsRwA/U9h1wwxl/GeZPho8g938yJDDUIZhhBN/FGhcB/P+p7UX+dM2vET8jfBqEkh1UGcHf"
        "f393AiUAX/1PUadXb96w/9M/mSKY/Aqi3LAm9RUI29qfDhVuhh9gH6Z7wp3++ttvqjPSTuZN9vP+UwCEpgL15l338FboeM/spPZ7"
        "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"
        "QmCC"
    ),
    "settings": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX////+///9/v7m6u6lsMCbo7CUo7mUo7iUo7eTo7iPnK9b"
        "ZndJU2dHUmVGUWRFUGNET2FDTmFCTWBCTF9BS15AS14/Sl0/SVw+SFs9SFs9R1o8Rlk7Rlk7RVg6RFc5Q1Y4Q1U5QlQ4QlQ3QVQ2"
        "QFM1P1E0PlEzPlEzPVAzPU8zPE8yPE8yPE4yO04xO04xOk0wOk0vOUwvOUsuOUwvOEsuOEsuN0otN0otNkksNUgsNEYrNUgrNEcq"
        "NEcqM0UpMkUoMkUoMUMnMUQnMEMmMEMmL0ImL0EmLkAlL0IlLkIlLkElLkAkLkElLUAkLUEkLUAkLT8kLD4jLUAjLEAjLD8jLD4i"
        "LD8jKz0iKz8iKz4iKz0hKz4iKjwhKj4hKj0hKjwhKTsgKTwgKDofKDsfJzoeJzoeJjgdJjkdJTccJTgcJDcbJTgbJDcbJDYbIzUa"
        "IzYaIzUaIjUZIzYZIjUZIjQYIjUZITQYITQYIDMXIDMXHzIXHzEXHjAWHzIWHjEWHjAVHjEVHTAVHS8VHC4UHTAUHC8UHC4THC8U"
        "Gy0TGy4TGy0TGiwSGy4SGi0SGiwSGSsRGi0RGS4RGSwRGSsQGSwSGCkQGC0QGCsQGCoQFyoPGCwPFywPFyoPFykNFysSEyOPTOKc"
        "AABQ/ElEQVR42u2diUMb173vCRLYmREgISEJCUmgLSBAIC6LMCAhFpuL7WD7YqiwIyOELEOJXddtvPSSS3pJaPJIntu4y3MT5199"
        "Z58zM2dAwmAjR782CcycObN9+P1+37NN3QVkF80dv/ztp5pVq/3888/43wb2/7D9S20/quz/IvtBbXijqpymkn/hqo3PzS5PaHU1"
        "AD8M/o4E8CcRf3r8fhDbcQgeQ+BPRxJYA/DDAPCn4x2gsfs7Aj4xgyfxgTUAP2QAy4i/R+H3Qxl2FILlEFgD8AO2f/+7Mvenwu+H"
        "sk3FoADBn05A4OkByIf8t6rgLar4VSZ/x9hR/Gmd3/8RWJkInpRABqD3l+//+Rb204udh8h2Xvx0whp2sjlk2Z2f/lmz8p5ZaXFp"
        "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"
    ),
    "top": (
        "iVBORw0KGgoAAAANSUhEUgAAAoAAAADcCAMAAADXwOC0AAAB4FBMVEX///////7//v7+/v78/Pzx8vXpzn78vyT7vyTyvzjct1eO"
        "gFl2YCVtXC5tWiZsWSdrWSdrWCZpVydoVidoViVmVSZlVCdkUyZgUitjUydiUidhUSdiUiZfUCdgUCZeTydcTilbTShbTCVYSydX"
        "SidQSjlVSShSRyhVSSZTRyZPRShORChNQyhQRCVOQyZMQihKQShMQiVKQCVJQClIPydHPyhGPidFPSdEPSlDPCdEPCVBOypCOyZB"
        "OihAOihBOSY/OSc9OCg8Nyk9NyY6Nig5NSg5NCg4NCg8NiY5NCY4MyY3Myg2Myg2MiY1NDQ1Mig0MSgzMSk1MSYzMCgyMCgxMC00"
        "MCYxLykxLygwLysyLyUwLikuLSkwLSQtLCksKygrKicpKSkoKCYnKCgmKCkmJyklJykmJyclJikkJiklJicjJikjJSkkJSYiJSki"
        "JCkjJCYhJCkhIyUgIykgISQfIykfIikfICMeIikeISgeHyEdISkdICkcICkdICccHyQbICkbHykaHykbHycaHigZHikZHigYHikY"
        "HSkXHSkYHScYHCcWHCkXGyUVGykVGycVGicUGykUGikTGikUGicTGSgSGSkSGCcRGCkRGCgQGCkQFykPFykPFygOFykKEydZc3l/"
        "AABGfElEQVR42u2diV9b15n3Pa8tsCMMZpEUQCAL7QKJTWa1MWAcAzZ4xWaRBIjEbjymsbFrN2TaT9OkmaRvkk7TSSZp+ib/6nv2"
        "9blaQCTGo99Mg3Xv1bnbV892zj33xGmq+sDP3/2rpuOq/wfpRyry7x+k/ofpW67/lvovW8pa/gXegtKoukfwYJyO/EQNwDecvx+L"
        "82fD9zcpi0EDQZPAHysnsAbgmwogbP+c8DPI06RB6GwFSxBYGsBvf6zpDdIPgNUT6AEu96siAjyyRqFhDX/4ofzDPAiA3zPVbvIv"
        "qQqvejH+LPy+KkMlETw0gP/4Z3n6SeifNR1KlVzJyq76Dz84RH0ynOMOFrGF//zfolK3RDKCQsgGlnsNKgbw+58ermY3kLKrD3/6"
        "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"
    ),
}

if __name__ == "__main__":
    asyncio.run(main())
