import asyncio
import logging
import os
import random

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

dp = Dispatcher()
GROUPS = F.chat.type.in_({"group", "supergroup"})

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
        "🎮 <b>Игры</b>\n\n"
        "❌⭕ /xo — крестики-нолики (2 игрока)\n"
        "🐊 /croc — крокодил\n"
        "🏆 /top — счёт в крокодиле\n\n"
        "Чтобы крокодил видел ваши ответы, у бота должен быть "
        "отключён Privacy Mode (@BotFather → /setprivacy → Disable).",
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

croc: dict[int, dict] = {}       # chat_id -> состояние игры
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
        await message.answer(f"Игра уже идёт, ведущий — {g['leader'][1]} 🐊")
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
        f"🐊 Ведущий: <b>{u.full_name}</b>\n\n"
        "Нажми «Посмотреть слово» и объясняй его без однокоренных слов.\n"
        "Остальные — пишите ответы прямо в чат!",
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
    lines = [f"{i}. {n} — {s}" for i, (n, s) in enumerate(top, 1)]
    await message.answer("🏆 <b>Топ крокодила</b>\n\n" + "\n".join(lines), parse_mode="HTML")


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

    await message.reply(
        f"🎉 <b>{u.full_name}</b> угадал(а)! Слово: <b>{g['word']}</b>\n"
        f"Очков: {pts + 1}",
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

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
