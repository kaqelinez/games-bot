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
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatPermissions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

DB_PATH = os.getenv("DB_PATH", "bot.db")

dp = Dispatcher()
GROUPS = F.chat.type.in_({"group", "supergroup"})

# ---------------------------------------------------------------- ВЛАДЕЛЕЦ: ЛОГИ И МОНИТОРИНГ
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_ID", "").replace(" ", "").split(",") if x.isdigit()
}
MON_HOST = os.getenv("MON_HOST", "anmine.ru")
MON_PORT = int(os.getenv("MON_PORT", "1932") or 1932)  # Bedrock, UDP
MON_INTERVAL = int(os.getenv("MON_INTERVAL", "120"))

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
    if not res["up"]:
        return f"🖥 {MON_HOST}:{MON_PORT}: 🔴 Недоступен (нет ответа)"
    i = res["info"]
    line = f"🖥 {MON_HOST}:{MON_PORT}: 🟢 Онлайн, {i['ms']} мс"
    if i["online"] is not None:
        line += f"\n👥 Игроков: {i['online']}/{i['max']}"
    if i["motd"]:
        line += f"\n📝 {i['motd']}"
    return line


@dp.message(Command("server"))
async def cmd_server(message: Message):
    res = await check_host()
    await message.reply(fmt_check(res))


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
    return parse_online(page)


def split_text(text: str, limit: int = 3900):
    chunks, cur = [], ""
    for word in text.split(" "):
        if len(cur) + len(word) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += (" " if cur else "") + word
    if cur:
        chunks.append(cur)
    return chunks


@dp.message(Command("online"))
async def cmd_online(message: Message, command: CommandObject):
    try:
        servers = await fetch_online()
    except Exception:
        logging.exception("online fetch failed")
        await message.reply("❌ Не удалось открыть anclan.ru, попробуй позже.")
        return
    if not servers:
        logging.warning("online: серверы на странице не найдены")
        await message.reply(
            "❌ Не смог разобрать страницу anclan.ru (возможно, сайт изменился)."
        )
        return

    arg = (command.args or "").strip()
    if arg.isdigit():
        srv = next((x for x in servers if x["name"].endswith(f"#{arg}")), None)
        if not srv:
            await message.reply(f"Сервера #{arg} нет на сайте.")
            return
        head = f"🎮 {srv['name']} — {srv['online']}/{srv['max']}"
        body = ", ".join(srv["players"]) or "Никого нет"
        for chunk in split_text(head + "\n\n" + body):
            await message.reply(chunk)
        return

    total = sum(x["online"] or 0 for x in servers)
    lines = [f"🎮 {x['name']}: {x['online']}/{x['max']}" for x in servers]
    await message.reply(
        "🌐 Онлайн AnMine\n\n" + "\n".join(lines)
        + f"\n\n👥 Всего: {total}\nНики игроков: /online 1, /online 2 …"
    )


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


# ---------------------------------------------------------------- ЛИЧКА
PRIVATE_TEXT = (
    "👋 Я работаю только в группах.\n"
    "Добавь меня в группу и напиши /games"
)


@dp.message(F.chat.type == "private")
async def private_any(message: Message):
    await message.answer(PRIVATE_TEXT)


# ---------------------------------------------------------------- МЕНЮ
@dp.message(GROUPS, Command("start", "games", "help"))
async def games_menu(message: Message):
    await message.answer(
        "🎮 <b>Игры</b>\n"
        "❌⭕ /xo — крестики-нолики\n"
        "🐊 /croc — крокодил, 🏆 /top — счёт крокодила\n\n"
        "💰 <b>Экономика</b>\n"
        "/balance — баланс\n"
        "/daily — ежедневный бонус\n"
        "/pay сумма — перевод (ответом на сообщение)\n"
        "/rich — самые богатые\n\n"
        "🏷 <b>Префиксы</b>\n"
        "/prefixes — список, /buyprefix номер — купить, /noprefix — снять\n\n"
        "🖥 /server — статус anmine.ru\n"
        "👥 /online — онлайн по серверам, /online 1 — ники\n\n"
        "🛡 <b>Для админов</b>\n"
        "/mute [30m|2h|1d] — мут (ответом)\n"
        "/unmute, /ban, /unban, /kick — ответом на сообщение\n"
        "/giveprefix текст — выдать свой префикс (ответом)\n\n"
        "Для крокодила у бота должен быть отключён Privacy Mode, "
        "а для мута/бана бот должен быть админом группы.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------- БАЗА ДАННЫХ
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute(
    """CREATE TABLE IF NOT EXISTS users(
        chat_id INTEGER, user_id INTEGER, name TEXT,
        balance INTEGER DEFAULT 0, farm_level INTEGER DEFAULT 1,
        last_farm INTEGER DEFAULT 0, last_daily INTEGER DEFAULT 0,
        prefix TEXT DEFAULT '',
        PRIMARY KEY(chat_id, user_id))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS owned(
        chat_id INTEGER, user_id INTEGER, idx INTEGER,
        PRIMARY KEY(chat_id, user_id, idx))"""
)
db.commit()

DAILY_CD = 86400
DAILY_BONUS = 300

PREFIXES = [
    ("🌱 Новичок", 0),
    ("🌾 Фермер", 1000),
    ("⭐ Звезда", 5000),
    ("💎 Мажор", 20000),
    ("👑 Король", 100000),
]


def get_user(chat_id: int, user):
    db.execute(
        "INSERT OR IGNORE INTO users(chat_id, user_id, name) VALUES(?,?,?)",
        (chat_id, user.id, user.full_name),
    )
    db.execute(
        "UPDATE users SET name=? WHERE chat_id=? AND user_id=?",
        (user.full_name, chat_id, user.id),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user.id),
    ).fetchone()


def set_field(chat_id: int, user_id: int, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    db.execute(
        f"UPDATE users SET {cols} WHERE chat_id=? AND user_id=?",
        (*fields.values(), chat_id, user_id),
    )
    db.commit()


def disp(row) -> str:
    name = html.escape(row["name"] or "Игрок")
    return f"{html.escape(row['prefix'])} {name}" if row["prefix"] else name


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


# ---------------------------------------------------------------- ЭКОНОМИКА
@dp.message(GROUPS, Command("balance", "bal"))
async def cmd_balance(message: Message):
    target = message.reply_to_message.from_user if (
        message.reply_to_message and message.reply_to_message.from_user
        and not message.reply_to_message.from_user.is_bot
    ) else message.from_user
    r = get_user(message.chat.id, target)
    await message.reply(
        f"👤 {disp(r)}\n"
        f"💰 Баланс: <b>{r['balance']}</b> 🪙",
        parse_mode="HTML",
    )


@dp.message(GROUPS, Command("daily"))
async def cmd_daily(message: Message):
    cid, u = message.chat.id, message.from_user
    r = get_user(cid, u)
    now = int(time.time())
    left = r["last_daily"] + DAILY_CD - now
    if left > 0:
        await message.reply(f"⏳ Бонус уже получен. Следующий через {fmt_time(left)}.")
        return
    set_field(cid, u.id, balance=r["balance"] + DAILY_BONUS, last_daily=now)
    await message.reply(
        f"🎁 Ежедневный бонус: <b>+{DAILY_BONUS}</b> 🪙", parse_mode="HTML"
    )


@dp.message(GROUPS, Command("pay"))
async def cmd_pay(message: Message, command: CommandObject):
    cid, u = message.chat.id, message.from_user
    rep = message.reply_to_message
    if not rep or not rep.from_user or rep.from_user.is_bot:
        await message.reply("Ответь командой на сообщение игрока: /pay 100")
        return
    if rep.from_user.id == u.id:
        await message.reply("Нельзя переводить самому себе 🙂")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Укажи сумму числом: /pay 100")
        return
    amount = int(command.args.strip())
    if amount <= 0:
        await message.reply("Сумма должна быть больше нуля.")
        return
    me = get_user(cid, u)
    if me["balance"] < amount:
        await message.reply(f"❌ Не хватает монет. Баланс: {me['balance']} 🪙")
        return
    other = get_user(cid, rep.from_user)
    set_field(cid, u.id, balance=me["balance"] - amount)
    set_field(cid, rep.from_user.id, balance=other["balance"] + amount)
    await message.reply(
        f"✅ {disp(me)} перевёл {disp(other)} <b>{amount}</b> 🪙",
        parse_mode="HTML",
    )


@dp.message(GROUPS, Command("rich"))
async def cmd_rich(message: Message):
    rows = db.execute(
        "SELECT * FROM users WHERE chat_id=? AND balance>0 "
        "ORDER BY balance DESC LIMIT 10",
        (message.chat.id,),
    ).fetchall()
    if not rows:
        await message.answer("Пока ни у кого нет монет. Начни с /daily 🎁")
        return
    lines = [f"{i}. {disp(r)} — {r['balance']} 🪙" for i, r in enumerate(rows, 1)]
    await message.answer(
        "💰 <b>Самые богатые</b>\n\n" + "\n".join(lines), parse_mode="HTML"
    )


# ---------------------------------------------------------------- ПРЕФИКСЫ
@dp.message(GROUPS, Command("prefixes"))
async def cmd_prefixes(message: Message):
    cid, u = message.chat.id, message.from_user
    get_user(cid, u)
    owned = {
        row["idx"]
        for row in db.execute(
            "SELECT idx FROM owned WHERE chat_id=? AND user_id=?", (cid, u.id)
        )
    }
    lines = []
    for i, (name, price) in enumerate(PREFIXES, 1):
        mark = " ✅" if (i - 1) in owned else ""
        lines.append(f"{i}. {name} — {price} 🪙{mark}")
    await message.reply(
        "🏷 <b>Префиксы</b>\n\n" + "\n".join(lines) +
        "\n\nКупить: /buyprefix номер\nСнять: /noprefix",
        parse_mode="HTML",
    )


@dp.message(GROUPS, Command("buyprefix"))
async def cmd_buyprefix(message: Message, command: CommandObject):
    cid, u = message.chat.id, message.from_user
    r = get_user(cid, u)
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Напиши номер: /buyprefix 2 (список: /prefixes)")
        return
    n = int(command.args.strip())
    if not 1 <= n <= len(PREFIXES):
        await message.reply("Такого номера нет. Смотри /prefixes")
        return
    name, price = PREFIXES[n - 1]
    has = db.execute(
        "SELECT 1 FROM owned WHERE chat_id=? AND user_id=? AND idx=?",
        (cid, u.id, n - 1),
    ).fetchone()
    if has:
        set_field(cid, u.id, prefix=name)
        await message.reply(f"✅ Префикс «{name}» надет.")
        return
    if r["balance"] < price:
        await message.reply(
            f"❌ Нужно {price} 🪙, у тебя {r['balance']}."
        )
        return
    db.execute(
        "INSERT OR IGNORE INTO owned(chat_id, user_id, idx) VALUES(?,?,?)",
        (cid, u.id, n - 1),
    )
    db.commit()
    set_field(cid, u.id, balance=r["balance"] - price, prefix=name)
    await message.reply(f"🎉 Куплен и надет префикс «{name}»!")


@dp.message(GROUPS, Command("noprefix"))
async def cmd_noprefix(message: Message):
    get_user(message.chat.id, message.from_user)
    set_field(message.chat.id, message.from_user.id, prefix="")
    await message.reply("Префикс снят.")


# ---------------------------------------------------------------- МОДЕРАЦИЯ
async def require_admin(message: Message) -> bool:
    m = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if m.status not in ("creator", "administrator"):
        await message.reply("⛔ Эта команда только для админов чата.")
        return False
    return True


def reply_target(message: Message):
    rep = message.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        return rep.from_user
    return None


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
    if not await require_admin(message):
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /mute 30m")
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
    await message.reply(
        f"🔇 {html.escape(t.full_name)} в муте на {fmt_time(mins * 60)}.",
        parse_mode="HTML",
    )


@dp.message(GROUPS, Command("unmute"))
async def cmd_unmute(message: Message):
    if not await require_admin(message):
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
    await message.reply(
        f"🔊 {html.escape(t.full_name)} размучен.", parse_mode="HTML"
    )


@dp.message(GROUPS, Command("ban"))
async def cmd_ban(message: Message):
    if not await require_admin(message):
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /ban")
        return
    try:
        await message.bot.ban_chat_member(message.chat.id, t.id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    await message.reply(
        f"🔨 {html.escape(t.full_name)} забанен.", parse_mode="HTML"
    )


@dp.message(GROUPS, Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await require_admin(message):
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
    if not await require_admin(message):
        return
    t = reply_target(message)
    if not t:
        await message.reply("Ответь командой на сообщение участника: /kick")
        return
    try:
        await message.bot.ban_chat_member(message.chat.id, t.id)
        await message.bot.unban_chat_member(message.chat.id, t.id, only_if_banned=True)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await mod_error(message, e)
        return
    await message.reply(
        f"👢 {html.escape(t.full_name)} исключён.", parse_mode="HTML"
    )


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
    get_user(message.chat.id, t)
    set_field(message.chat.id, t.id, prefix=text)
    await message.reply(
        f"🏷 {html.escape(t.full_name)} получил префикс «{html.escape(text)}».",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------- КРЕСТИКИ-НОЛИКИ
xo_games: dict[str, dict] = {}
LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]
SYM = {"X": "❌", "O": "⭕", " ": "⬜"}


def xo_winner(board):
    for a, b, c in LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return None


def xo_keyboard(g):
    kb = InlineKeyboardBuilder()
    if g["o"] is None:
        kb.button(text="⭕ Присоединиться", callback_data="xo:join")
        return kb.as_markup()
    for i, cell in enumerate(g["board"]):
        kb.button(text=SYM[cell], callback_data=f"xo:{i}")
    kb.adjust(3)
    return kb.as_markup()


def xo_text(g, result=None):
    head = f"❌ {g['x'][1]}"
    if g["o"] is None:
        return f"{head}  vs  ⭕ ???\n\nЖдём второго игрока…"
    head += f"  vs  ⭕ {g['o'][1]}"
    if result == "draw":
        return f"{head}\n\n🤝 Ничья!"
    if result in ("X", "O"):
        name = g["x"][1] if result == "X" else g["o"][1]
        return f"{head}\n\n🏆 Победил(а) {name} {SYM[result]}"
    turn_name = g["x"][1] if g["turn"] == "X" else g["o"][1]
    return f"{head}\n\nХодит: {turn_name} {SYM[g['turn']]}"


@dp.message(GROUPS, Command("xo"))
async def xo_start(message: Message):
    u = message.from_user
    g = {
        "board": [" "] * 9,
        "x": (u.id, u.full_name),
        "o": None,
        "turn": "X",
        "over": False,
    }
    msg = await message.answer(xo_text(g), reply_markup=xo_keyboard(g))
    xo_games[f"{msg.chat.id}:{msg.message_id}"] = g


@dp.callback_query(F.data.startswith("xo:"))
async def xo_click(cb: CallbackQuery):
    key = f"{cb.message.chat.id}:{cb.message.message_id}"
    g = xo_games.get(key)
    if not g:
        await cb.answer("Эта игра уже не активна. Начни новую: /xo", show_alert=True)
        return
    u = cb.from_user
    action = cb.data.split(":")[1]

    if action == "join":
        if g["o"] is not None:
            await cb.answer("Уже есть второй игрок")
            return
        if u.id == g["x"][0]:
            await cb.answer("Нужен другой игрок 🙂", show_alert=True)
            return
        g["o"] = (u.id, u.full_name)
        await cb.message.edit_text(xo_text(g), reply_markup=xo_keyboard(g))
        await cb.answer()
        return

    if g["over"] or g["o"] is None:
        await cb.answer()
        return

    cur = g["x"] if g["turn"] == "X" else g["o"]
    if u.id != cur[0]:
        if u.id in (g["x"][0], g["o"][0]):
            await cb.answer("Сейчас не твой ход")
        else:
            await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return

    i = int(action)
    if g["board"][i] != " ":
        await cb.answer("Клетка занята")
        return

    g["board"][i] = g["turn"]
    win = xo_winner(g["board"])
    if win:
        g["over"] = True
        await cb.message.edit_text(xo_text(g, win), reply_markup=xo_keyboard(g))
    elif " " not in g["board"]:
        g["over"] = True
        await cb.message.edit_text(xo_text(g, "draw"), reply_markup=xo_keyboard(g))
    else:
        g["turn"] = "O" if g["turn"] == "X" else "X"
        await cb.message.edit_text(xo_text(g), reply_markup=xo_keyboard(g))
    await cb.answer()


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

CROC_REWARD = 100

croc: dict[int, dict] = {}         # chat_id -> состояние игры
croc_scores: dict[int, dict] = {}  # chat_id -> {user_id: (name, очки)}


def norm(s: str) -> str:
    return s.strip().lower().replace("ё", "е")


def lead_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎤 Хочу быть ведущим", callback_data="cr:lead")
    return kb.as_markup()


def game_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👀 Посмотреть слово", callback_data="cr:see")
    kb.button(text="🔄 Другое слово", callback_data="cr:new")
    kb.button(text="🏳️ Сдаться", callback_data="cr:giveup")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(GROUPS, Command("croc"))
async def croc_start(message: Message):
    g = croc.get(message.chat.id)
    if g and g["active"]:
        await message.answer(
            f"Игра уже идёт, ведущий — {html.escape(g['leader'][1])} 🐊",
            parse_mode="HTML",
        )
        return
    await message.answer(
        "🐊 <b>Крокодил</b>\nКто объясняет слово?",
        parse_mode="HTML",
        reply_markup=lead_kb(),
    )


@dp.callback_query(F.data == "cr:lead")
async def croc_lead(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    g = croc.get(chat_id)
    if g and g["active"]:
        await cb.answer("Ведущий уже выбран", show_alert=True)
        return
    u = cb.from_user
    croc[chat_id] = {
        "active": True,
        "leader": (u.id, u.full_name),
        "word": random.choice(WORDS),
    }
    await cb.message.edit_text(
        f"🐊 Ведущий: <b>{html.escape(u.full_name)}</b>\n\n"
        "Нажми «Посмотреть слово» и объясняй его без однокоренных слов.\n"
        f"Остальные — пишите ответы прямо в чат! Награда: {CROC_REWARD} 🪙",
        parse_mode="HTML",
        reply_markup=game_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data.in_({"cr:see", "cr:new", "cr:giveup"}))
async def croc_leader_actions(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    g = croc.get(chat_id)
    if not g or not g["active"]:
        await cb.answer("Игра не активна. Начни: /croc", show_alert=True)
        return
    if cb.from_user.id != g["leader"][0]:
        await cb.answer("Это кнопки только для ведущего 🙂", show_alert=True)
        return

    if cb.data == "cr:see":
        await cb.answer(f"Твоё слово: {g['word']}", show_alert=True)
    elif cb.data == "cr:new":
        old = g["word"]
        while g["word"] == old:
            g["word"] = random.choice(WORDS)
        await cb.answer(f"Новое слово: {g['word']}", show_alert=True)
    else:
        word = g["word"]
        g["active"] = False
        await cb.message.edit_text(
            f"🏳️ Ведущий сдался. Слово было: <b>{word}</b>",
            parse_mode="HTML",
        )
        await cb.message.answer("Играем ещё?", reply_markup=lead_kb())
        await cb.answer()


@dp.message(GROUPS, Command("top"))
async def croc_top(message: Message):
    scores = croc_scores.get(message.chat.id)
    if not scores:
        await message.answer("Пока нет очков. Сыграйте в /croc 🐊")
        return
    top = sorted(scores.values(), key=lambda x: x[1], reverse=True)[:10]
    lines = [
        f"{i}. {html.escape(n)} — {s}" for i, (n, s) in enumerate(top, 1)
    ]
    await message.answer(
        "🏆 <b>Топ крокодила</b>\n\n" + "\n".join(lines), parse_mode="HTML"
    )


# Ловим ответы игроков (обычные текстовые сообщения, не команды)
@dp.message(GROUPS, F.text, ~F.text.startswith("/"))
async def croc_guess(message: Message):
    g = croc.get(message.chat.id)
    if not g or not g["active"]:
        return
    u = message.from_user
    if u.id == g["leader"][0]:
        return
    if norm(message.text) != norm(g["word"]):
        return

    g["active"] = False
    chat_scores = croc_scores.setdefault(message.chat.id, {})
    name, pts = chat_scores.get(u.id, (u.full_name, 0))
    chat_scores[u.id] = (u.full_name, pts + 1)

    # награда монетами
    r = get_user(message.chat.id, u)
    set_field(message.chat.id, u.id, balance=r["balance"] + CROC_REWARD)

    await message.reply(
        f"🎉 <b>{html.escape(u.full_name)}</b> угадал(а)! Слово: <b>{g['word']}</b>\n"
        f"Очков: {pts + 1}, награда: +{CROC_REWARD} 🪙",
        parse_mode="HTML",
        reply_markup=lead_kb(),
    )


# ---------------------------------------------------------------- ЗАПУСК
async def health(_request):
    return web.Response(text="ok")


async def main():
    bot = Bot(TOKEN)

    # мини-сервер, чтобы бесплатный хостинг видел открытый порт
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    asyncio.create_task(monitor_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
