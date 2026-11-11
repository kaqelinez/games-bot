import asyncio
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
    InlineKeyboardMarkup,
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


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
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
    await sent.edit_text(
        "🏓 <b>Понг!</b>\n\n"
        f"⚡ Ответ за <b>{ms} мс</b>\n"
        f"⏱ Работаю без перезапуска: <b>{up}</b>\n"
        f"💤 Анти-сон: <b>{anti}</b>",
        parse_mode="HTML",
    )


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
    await message.reply(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("online"))
async def cmd_online(message: Message, command: CommandObject):
    if not monitoring_allowed(message):
        await message.reply(MONITORING_OFF_TEXT)
        return
    arg = (command.args or "").strip()
    text, kb = await view_online(int(arg) if arg.isdigit() else None)
    await message.reply(text, parse_mode="HTML", reply_markup=kb)


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
    await message.reply(text, parse_mode="HTML", reply_markup=kb)


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
    "addadmin", "deladmin", "admins",
}
ALIASES = {
    "menu": "start", "games": "start", "help": "start",
    "prefix": "profile", "monitoringonoff": "monitoringoff",
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
                    else "xo" if event.data.startswith("x:")
                    else "croc" if event.data.startswith("c:") else None
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
        "Добавь меня в чат и напиши /start 🎮",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ---------------------------------------------------------------- ГЛАВНОЕ МЕНЮ
HELP_TEXT = (
    "❓ <b>Все команды</b>\n\n"
    "🎮 <b>Игры</b>\n"
    "/xo — крестики-нолики, /croc — крокодил, /top — счёт крокодила\n\n"
    "🖥 <b>Сервер</b>\n"
    "/server — статус anmine.ru\n"
    "/online — онлайн, /online 1 — ники\n"
    "/find ник — где игрок и когда был в сети\n\n"
    "👤 /profile — профиль и префикс\n"
    "🏓 /ping — скорость ответа бота\n\n"
    "🛡 <b>Для админов</b>\n"
    "/mute [30m|2h|1d], /unmute, /ban, /unban, /kick — ответом на сообщение\n"
    "/giveprefix текст, /removeprefix — префиксы (ответом)\n"
    "/addadmin, /deladmin, /admins — админы бота (могут мут/бан/кик)\n"
    "/disable команда, /enable команда, /disabled — запретить/разрешить команды в чате\n"
    "/monitoringon, /monitoringoff — вкл/выкл /server и /online\n"
    "/mimicon, /mimicoff — режим кривляния\n\n"
    "Для крокодила и кривляния отключи Privacy Mode у бота "
    "(@BotFather → /setprivacy → Disable). Для мута и бана бот должен быть "
    "админом группы."
)

MAIN_ROWS = [
    [btn("🎮 Игры", "m:games"), btn("🖥 Сервер", "m:server")],
    [btn("👤 Профиль", "m:profile"), btn("⚙️ Настройки", "m:settings")],
    [btn("❓ Команды", "m:help")],
]


def main_text(user) -> str:
    return (
        f"👋 <b>Привет, {html.escape(user.first_name or 'друг')}!</b>\n\n"
        "Я — игровой бот этого чата.\n"
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
    await message.answer(
        main_text(message.from_user),
        parse_mode="HTML",
        reply_markup=markup(MAIN_ROWS),
    )


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(cb: CallbackQuery):
    action = cb.data[2:]
    chat_id = cb.message.chat.id
    u = cb.from_user

    if action == "main":
        await safe_edit(cb, main_text(u), markup(MAIN_ROWS))

    elif action == "games":
        text, kb = games_view()
        await safe_edit(cb, text, kb)

    elif action == "xo":
        await start_xo(cb.message)

    elif action == "croc":
        await start_croc(cb.message)

    elif action == "top":
        await send_top(cb.message)

    elif action == "server":
        await safe_edit(
            cb,
            "🖥 <b>Сервер</b>\n\nСтатус и онлайн AnMine:",
            markup([
                [btn("📡 Статус", "m:status"), btn("👥 Онлайн", "o:all")],
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
        await safe_edit(cb, HELP_TEXT, markup([HOME]))

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
    await message.reply(
        profile_text(message.chat.id, t),
        parse_mode="HTML",
        reply_markup=markup([HOME]),
    )


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
    await target.answer(
        "🏆 <b>Топ крокодила</b>\n\n" + "\n".join(lines), parse_mode="HTML"
    )


@dp.message(GROUPS, Command("top"))
async def cmd_top(message: Message):
    await send_top(message)
# ---------------------------------------------------------------- ИГРЫ
GAMES_TEXT = "🎮 <b>Игры</b>\n\nВыбери игру 👇"
MAX_GAMES = 200
ROUND_TTL = 15 * 60  # раунд крокодила без движения считается забытым


def games_view():
    return GAMES_TEXT, markup([
        [btn("❌⭕ Крестики-нолики", "m:xo"), btn("🐊 Крокодил", "m:croc")],
        [btn("🏆 Топ крокодила", "m:top")],
        HOME,
    ])


@dp.message(GROUPS, Command("games"))
async def cmd_games_menu(message: Message):
    text, kb = games_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ---------------- крестики-нолики
XO_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
xo_games: dict[tuple, dict] = {}  # (chat_id, message_id) -> партия


def new_xo(players=None) -> dict:
    return {"b": [""] * 9, "p": players or {}, "turn": "X", "win": None, "line": ()}


def xo_result(b):
    for line in XO_LINES:
        a, c, d = line
        if b[a] and b[a] == b[c] == b[d]:
            return b[a], line
    return ("draw", ()) if all(b) else (None, ())


def xo_text(g: dict) -> str:
    x, o = g["p"].get("X"), g["p"].get("O")
    px = html.escape(x[1]) if x else "—"
    po = html.escape(o[1]) if o else "—"
    if g["win"] == "draw":
        tail = "🤝 <b>Ничья!</b>"
    elif g["win"]:
        tail = f"🔥 <b>Победил(а) {html.escape(g['p'][g['win']][1])}!</b>"
    elif not x:
        tail = "Нажми на любую клетку — ты играешь ❌."
    elif not o:
        tail = "Ждём соперника: пусть нажмёт на любую клетку (он будет ⭕)."
    else:
        sym = g["turn"]
        tail = f"Ход: {'❌' if sym == 'X' else '⭕'} {html.escape(g['p'][sym][1])}"
    return f"❌⭕ <b>Крестики-нолики</b>\n\n❌ {px}\n⭕ {po}\n\n{tail}"


def xo_kb(g: dict) -> InlineKeyboardMarkup:
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            v = g["b"][i]
            if g["win"] in ("X", "O") and i in g["line"]:
                t = "🟥" if v == "X" else "🟦"
            else:
                t = "❌" if v == "X" else "⭕" if v == "O" else "▫️"
            row.append(btn(t, f"x:{i}"))
        rows.append(row)
    if g["win"]:
        rows.append([btn("🔄 Реванш", "x:new"), btn("🏠 Меню", "m:main")])
    else:
        rows.append([btn("🏳️ Закончить", "x:stop")])
    return markup(rows)


async def start_xo(target: Message):
    g = new_xo()
    m = await target.answer(xo_text(g), parse_mode="HTML", reply_markup=xo_kb(g))
    xo_games[(target.chat.id, m.message_id)] = g
    while len(xo_games) > MAX_GAMES:
        xo_games.pop(next(iter(xo_games)))


@dp.message(GROUPS, Command("xo"))
async def cmd_xo(message: Message):
    await start_xo(message)


@dp.callback_query(F.data.startswith("x:"))
async def cb_xo(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    g = xo_games.get((chat_id, cb.message.message_id))
    if g is None:
        await cb.answer("Эта игра устарела. Начни новую: /xo", show_alert=True)
        return
    u = cb.from_user
    name = label(chat_id, u)
    ids = {p[0] for p in g["p"].values()}
    act = cb.data[2:]

    if act == "new":
        if not g["win"] or u.id not in ids:
            await cb.answer("Реванш — только для игроков после партии")
            return
        g.update(new_xo(g["p"]))
    elif act == "stop":
        if u.id not in ids:
            await cb.answer("Ты не участвуешь в этой партии")
            return
        xo_games.pop((chat_id, cb.message.message_id), None)
        await safe_edit(cb, "🏁 Партия остановлена.",
                        markup([[btn("❌⭕ Новая игра", "m:xo")], HOME]))
        await cb.answer()
        return
    else:
        try:
            i = int(act)
        except ValueError:
            await cb.answer()
            return
        if g["win"]:
            await cb.answer("Партия окончена — жми «Реванш»")
            return
        if not 0 <= i < 9 or g["b"][i]:
            await cb.answer("Клетка занята")
            return
        sym = g["turn"]
        cur = g["p"].get(sym)
        if cur is None:
            if u.id in ids:
                await cb.answer("Ждём соперника 🙂")
                return
            g["p"][sym] = [u.id, name]
        elif cur[0] != u.id:
            await cb.answer("Сейчас не твой ход" if u.id in ids else "Ты не участвуешь в этой партии")
            return
        g["b"][i] = sym
        res, line = xo_result(g["b"])
        if res:
            g["win"], g["line"] = res, line
        else:
            g["turn"] = "O" if sym == "X" else "X"

    await safe_edit(cb, xo_text(g), xo_kb(g))
    await cb.answer()


# ---------------- крокодил
def croc_idle_kb() -> InlineKeyboardMarkup:
    return markup([
        [btn("🎤 Стать ведущим", "c:lead")],
        [btn("🏆 Топ крокодила", "m:top"), btn("🏠 Меню", "m:main")],
    ])


CROC_RULES = (
    "🐊 <b>Крокодил</b>\n\n"
    "Ведущий получает слово и объясняет его в чате — без называния самого слова. "
    "Остальные пишут варианты обычными сообщениями. Кто угадает — получает очко."
)


def croc_active_text(g: dict) -> str:
    return (
        "🐊 <b>Крокодил</b>\n\n"
        f"🎤 Ведущий: <b>{html.escape(g['leader'][1])}</b>\n\n"
        "Угадывайте слово — пишите варианты в чат 👇\n"
        "<i>Ведущий жмёт «Слово», чтобы посмотреть его.</i>"
    )


def croc_active_kb() -> InlineKeyboardMarkup:
    return markup([
        [btn("👁 Слово", "c:show")],
        [btn("🔄 Другое слово", "c:new"), btn("🏳️ Сдаться", "c:giveup")],
    ])


def croc_is_live(chat_id: int) -> bool:
    g = croc.get(chat_id)
    if g and time.time() - g["t"] > ROUND_TTL:
        croc.pop(chat_id, None)
        return False
    return g is not None


async def start_croc(target: Message):
    if croc_is_live(target.chat.id):
        g = croc[target.chat.id]
        await target.answer(f"🐊 Раунд уже идёт. Ведущий: {html.escape(g['leader'][1])}", parse_mode="HTML")
        return
    await target.answer(CROC_RULES, parse_mode="HTML", reply_markup=croc_idle_kb())


@dp.message(GROUPS, Command("croc"))
async def cmd_croc(message: Message):
    await start_croc(message)


@dp.callback_query(F.data.startswith("c:"))
async def cb_croc(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    u = cb.from_user
    act = cb.data[2:]

    if act == "lead":
        if croc_is_live(chat_id):
            await cb.answer("Раунд уже идёт", show_alert=True)
            return
        g = {"leader": [u.id, label(chat_id, u)], "word": random.choice(WORDS),
             "msg": cb.message.message_id, "t": time.time()}
        croc[chat_id] = g
        await safe_edit(cb, croc_active_text(g), croc_active_kb())
        await cb.answer(f"Твоё слово: {g['word']}", show_alert=True)
        return

    g = croc.get(chat_id)
    if g is None or g["msg"] != cb.message.message_id:
        await cb.answer("Этот раунд уже закончился", show_alert=True)
        return
    if g["leader"][0] != u.id:
        await cb.answer("Это кнопки ведущего 🙂", show_alert=True)
        return

    g["t"] = time.time()
    if act == "show":
        await cb.answer(f"Твоё слово: {g['word']}", show_alert=True)
    elif act == "new":
        old = g["word"]
        while g["word"] == old:
            g["word"] = random.choice(WORDS)
        await cb.answer(f"Новое слово: {g['word']}", show_alert=True)
    elif act == "giveup":
        croc.pop(chat_id, None)
        await safe_edit(
            cb,
            f"🏳️ Ведущий сдался. Слово было: <b>{html.escape(g['word'])}</b>",
            croc_idle_kb(),
        )
        await cb.answer()
    else:
        await cb.answer()


async def croc_try(message: Message) -> bool:
    """Проверяет сообщение в чате как вариант слова в крокодиле. True — сообщение обработано."""
    chat_id = message.chat.id
    if not croc_is_live(chat_id):
        return False
    g = croc[chat_id]
    u = message.from_user
    text = norm(message.text or "")
    word = norm(g["word"])

    if u.id == g["leader"][0]:
        if word in text:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer("🙈 Ведущему нельзя называть слово!")
            return True
        return False

    if text != word:
        return False

    croc.pop(chat_id, None)
    name = label(chat_id, u)
    sc = croc_scores.setdefault(chat_id, {})
    _old, pts = sc.get(u.id, (name, 0))
    sc[u.id] = (name, pts + 1)
    await fire(message.bot, chat_id, message.message_id)
    await message.reply(
        f"🎉 <b>{html.escape(name)}</b> угадал(а) слово «{html.escape(g['word'])}»! "
        f"Очков: <b>{pts + 1}</b>",
        parse_mode="HTML",
    )
    try:
        await message.bot.edit_message_text(
            chat_id=chat_id, message_id=g["msg"],
            text=f"🐊 Раунд окончен. Слово: <b>{html.escape(g['word'])}</b>\nУгадал(а): {html.escape(name)}",
            parse_mode="HTML", reply_markup=croc_idle_kb(),
        )
    except Exception:
        pass
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

    if await croc_try(message):
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
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    await setup_commands(bot)
    if os.getenv("NOTIFY_START", "1") != "0":
        await notify_owners(bot, "🟢 Бот запущен")
    asyncio.create_task(keepalive_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
