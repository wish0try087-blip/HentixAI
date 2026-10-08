# -*- coding: utf-8 -*-
"""
HentixAI — Telegram-бот на aiogram 3.x.

!!! ВАЖНЫЕ НАСТРОЙКИ В @BotFather (обязательно!) !!!
1. /setprivacy -> Choose "DISABLE" (Group Privacy = ВЫКЛ) —
   иначе бот не будет видеть сообщения в группах.
2. Bot Settings -> Allowed to be added to groups = Allow Groups = ВКЛ.
3. Bot Settings -> Inline Mode = ВКЛ —
   иначе не будет работать режим "@HentixAI_bot вопрос".
4. Проверка подписки на канал НЕ требуется — бот работает для всех
   (проверки подписки полностью удалены из кода).

Функционал (обновлённая версия):
- ЗАДАЧА 1: проверка подписки УБРАНА ПОЛНОСТЬЮ — нет check_subscription(),
  нет get_chat_member() для канала, нет is_subscribed, нет кнопок
  "Подписаться"/"Я подписался", нет callback "check_sub". Бот работает для
  всех пользователей без каких-либо проверок подписки.
- ЗАДАЧА 2: INLINE MODE с кнопкой. Пользователь пишет "@HentixAI_bot вопрос"
  -> InlineQueryResultArticle(id="1", title="Ваш запрос",
  description=query[:50], input_message_content=query, reply_markup — кнопка
  "⚡ Получить ответ" с callback_data="inline:<query>"). При нажатии на кнопку
  (callback_inline_answer): callback.answer() -> баланс <= 0 -> edit_text
  "💸 Монеты закончились! Ждите начисления в 00:00 МСК."; баланс > 0 ->
  НОВОЕ сообщение "⌨️ Хентикс думает..." (reply) -> process_query ->
  evaluate_response -> списание 1 монеты -> bot.edit_message_text
  (chat_id=..., message_id=...) этого статусного сообщения. chat_id:
  callback.message.chat.id, если message есть, иначе callback.inline_message_id.
- Группы: упоминания по триггерам @hentixai_bot, @hentixai, @hentix,
  "хентикс", "hentix" (регистронезависимо) — та же логика "думает -> ответ".
- Экономика: старт 100 монет, 1 монета = 1 запрос, автоначисление 20-30
  монет всем в 00:00 МСК с уведомлением "🎁 Ежедневное начисление! ..."
  (try/except на случай блокировки бота пользователем).
- Команды: /start, /menu (в ЛС кнопки, в группах статистика), /balance,
  /daily, /help, /debug, callback show_balance, /clear_memory, /members.
- Память chat_history (100 сообщений, 50 в контекст), поиск Tavily.
- Приветствие при добавлении бота в группу (my_chat_member).
- Windows-фикс asyncio, логирование, try/except вокруг всех API,
  html.escape имён, инициализация БД через aiosqlite.
"""

# ---- ПРАВИЛО 8: Windows-фикс asyncio (должен быть ДО создания event loop) ----
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import contextlib
import html as html_lib
import logging
import random
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

import config

# ---------------- Логирование ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("hentixai")

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Дедуп сообщений, отправленных через inline (Telegram может прислать
# одно и то же message-update дважды).
_MSG_DEDUP: dict[tuple[int, int], float] = {}


def escape_html(text: str) -> str:
    """Экранирование HTML-спецсимволов (<, >, &) в именах пользователей."""
    return html_lib.escape(text or "", quote=False)


# ================= БАЗА ДАННЫХ =================

async def init_db() -> None:
    """Создание таблиц users и chat_history при запуске."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 100,
                last_daily_bonus TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT
            )
            """
        )
        # Мягкие миграции для старых БД
        for ddl in (
            "ALTER TABLE users ADD COLUMN created_at TEXT",
        ):
            with contextlib.suppress(Exception):
                await db.execute(ddl)
        await db.commit()
    logger.info("База данных инициализирована.")


async def get_user(user_id: int):
    """Вернуть строку пользователя или None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def ensure_user(user_id: int, username: str | None) -> int:
    """Создать пользователя с балансом START_BALANCE (100), если его нет. Вернуть баланс."""
    row = await get_user(user_id)
    if row is None:
        now = datetime.now().isoformat(timespec="seconds")
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (user_id, username, balance, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, username or "", config.START_BALANCE, now),
            )
            await db.commit()
        logger.info("Создан пользователь %s с балансом %s", user_id, config.START_BALANCE)
        return config.START_BALANCE
    return row["balance"]


async def add_balance(user_id: int, amount: int) -> int:
    """Изменить баланс, вернуть новый."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await db.commit()
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def save_message(chat_id: int, user_id: int, username: str, role: str, content: str) -> None:
    """Сохранить сообщение в историю чата и обрезать до HISTORY_LIMIT (100)."""
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_history (chat_id, user_id, username, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, username or "", role, content, now),
        )
        # Обрезка: удаляем всё, кроме последних HISTORY_LIMIT записей чата
        await db.execute(
            """
            DELETE FROM chat_history WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_history WHERE chat_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (chat_id, chat_id, config.HISTORY_LIMIT),
        )
        await db.commit()


async def get_history(chat_id: int, limit: int) -> list[tuple[str, str]]:
    """Последние `limit` сообщений чата в хронологическом порядке."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT role, content FROM chat_history WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]


async def clear_history(chat_id: int) -> int:
    """Очистить chat_history для чата. Вернуть число удалённых записей."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
        await db.commit()
        return cur.rowcount


async def get_group_stats(chat_id: int) -> dict:
    """Статистика чата: всего сообщений в памяти, уникальные юзеры, ответы бота, топ-3."""
    stats = {"total": 0, "bot_answers": 0, "unique_users": 0, "top": []}
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM chat_history WHERE chat_id = ?", (chat_id,)
            )
            stats["total"] = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT COUNT(*) FROM chat_history WHERE chat_id = ? AND role = 'assistant'",
                (chat_id,),
            )
            stats["bot_answers"] = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM chat_history "
                "WHERE chat_id = ? AND role = 'user'",
                (chat_id,),
            )
            stats["unique_users"] = (await cur.fetchone())[0]
            cur = await db.execute(
                """
                SELECT user_id, username, COUNT(*) AS cnt
                FROM chat_history
                WHERE chat_id = ? AND role = 'user'
                GROUP BY user_id
                ORDER BY cnt DESC
                LIMIT 3
                """,
                (chat_id,),
            )
            stats["top"] = await cur.fetchall()
    except Exception as e:
        logger.error("Ошибка получения статистики группы %s: %s", chat_id, e)
    return stats


# ================= АВТОМАТИЧЕСКИЙ ЕЖЕДНЕВНЫЙ БОНУС (00:00 МСК) =================

def msk_now() -> datetime:
    """Текущее время в Москве (UTC+3), naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        hours=config.MSK_UTC_OFFSET_HOURS
    )


def seconds_until_msk_midnight(now_msk: datetime | None = None) -> float:
    """Секунды до ближайших 00:00 МСК."""
    if now_msk is None:
        now_msk = msk_now()
    next_midnight = (now_msk + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (next_midnight - now_msk).total_seconds()


def until_msk_midnight_str() -> str:
    """'через Y ч Z мин' до ближайших 00:00 МСК."""
    secs = int(seconds_until_msk_midnight())
    hours = secs // 3600
    minutes = (secs % 3600) // 60
    return f"через {hours} ч {minutes} мин"


async def give_daily_bonus_to_all() -> None:
    """
    ПРАВИЛО 4: пройтись по всем пользователям БД, начислить каждому
    random.randint(20, 30) монет и отправить уведомление в ЛС.
    Отправка — в try/except (пользователь мог заблокировать бота).
    """
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            cur = await db.execute("SELECT user_id, username FROM users")
            rows = await cur.fetchall()
        now = msk_now().isoformat(timespec="seconds")
        total = 0
        sent_ok = 0
        sent_fail = 0
        for user_id, username in rows:
            bonus = random.randint(config.DAILY_BONUS_MIN, config.DAILY_BONUS_MAX)
            try:
                async with aiosqlite.connect(config.DB_PATH) as db:
                    await db.execute(
                        "UPDATE users SET balance = balance + ?, last_daily_bonus = ? "
                        "WHERE user_id = ?",
                        (bonus, now, user_id),
                    )
                    cur2 = await db.execute(
                        "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                    )
                    new_balance = (await cur2.fetchone())[0]
                    await db.commit()
                total += bonus
                logger.info("Начислено %s монет пользователю %s", bonus, username or user_id)
            except Exception as e:
                logger.error("Ошибка начисления пользователю %s: %s", user_id, e)
                continue

            # Уведомление о начислении (ПРАВИЛО 4)
            if config.NOTIFY_DAILY_BONUS:
                notify_text = (
                    f"🎁 Ежедневное начисление!\n\n"
                    f"Вам начислено {bonus} монет.\n"
                    f"Текущий баланс: {new_balance} монет.\n\n"
                    "Следующее начисление завтра в 00:00 МСК."
                )
                try:
                    await bot.send_message(chat_id=user_id, text=notify_text)
                    sent_ok += 1
                    logger.info("Уведомление доставлено пользователю %s", username or user_id)
                except Exception as e:
                    sent_fail += 1
                    logger.warning(
                        "Не удалось отправить уведомление пользователю %s "
                        "(возможно, заблокировал бота): %s",
                        username or user_id, e,
                    )
        logger.info(
            "Авто-бонус завершён: %s пользователей, всего +%s монет, "
            "уведомлений доставлено: %s, ошибок отправки: %s.",
            len(rows), total, sent_ok, sent_fail,
        )
    except Exception as e:
        logger.error("Ошибка авто-начисления бонуса: %s", e)


async def auto_daily_loop() -> None:
    """
    asyncio-задача: вычисляет время до ближайших 00:00 МСК, спит до него,
    затем каждые 24 часа начисляет всем по 20-30 монет.
    """
    delay = seconds_until_msk_midnight()
    logger.info("Авто-бонус: следующее начисление через %.0f сек (в 00:00 МСК)", delay)
    await asyncio.sleep(delay)
    while True:
        await give_daily_bonus_to_all()
        await asyncio.sleep(24 * 3600)


# ================= КЛАВИАТУРЫ И ТЕКСТЫ =================

def start_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура /start и /menu (ЛС): группа, баланс, канал."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить в группу",
            url=f"https://t.me/{config.BOT_USERNAME.lstrip('@')}?startgroup=true",
        )],
        [
            InlineKeyboardButton(text="💰 Баланс", callback_data="show_balance"),
            InlineKeyboardButton(
                text="📢 Канал",
                url=f"https://t.me/{config.SUPPORT_CHANNEL_USERNAME.lstrip('@')}",
            ),
        ],
    ])


START_TEXT_TEMPLATE = (
    "✌️ Добро пожаловать, {first_name}!\n\n"
    "Я — HentixAI: отвечаю на вопросы, помню диалог и имею доступ в интернет.\n\n"
    "💰 Ваш начальный баланс: {balance} монет (1 монета = 1 запрос)\n\n"
    "🎁 Автоматически вам начисляются монеты {bmin}-{bmax} каждые сутки в 00:00 МСК\n\n"
    "<b>Команды:</b>\n"
    "/menu — меню с кнопками\n"
    "/balance — проверить баланс монет\n"
    "/help — помощь\n"
    "/clear_memory — очистить память чата\n"
    "/members — участники группы (только в группах)\n\n"
    "💡 Чтобы спросить меня в группе или любом чате, напишите:\n"
    "<code>@HentixAI_bot ваш вопрос</code>"
)


# ================= ЭКОНОМИКА (ПРАВИЛО 4) =================

def evaluate_response(response: str) -> bool:
    """True — ответ качественный, монету можно списать."""
    if not response:
        return False
    low = response.lower()
    bad_markers = ("не знаю", "не могу", "извините", "к сожалению")
    if any(m in low for m in bad_markers):
        return False
    if len(response.strip()) < 30:
        return False
    return True


# ================= ПОИСК И AI (ПРАВИЛО 6) =================

def needs_search(query: str) -> bool:
    """Нужен ли веб-поиск: ключевое слово из SEARCH_KEYWORDS или '?' в конце (>15 симв.)."""
    q = query.lower()
    for kw in config.SEARCH_KEYWORDS:
        if kw in q:
            return True
    if len(query) > 15 and query.strip().endswith("?"):
        return True
    return False


async def search_internet(query: str) -> str:
    """Поиск через Tavily API. Возвращает текст сводки или пустую строку."""
    try:
        payload = {
            "api_key": config.TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": 4,
            "include_answer": True,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(config.TAVILY_SEARCH_URL, json=payload) as resp:
                if resp.status != 200:
                    logger.error("Tavily HTTP %s", resp.status)
                    return ""
                data = await resp.json()
        parts = []
        answer = data.get("answer")
        if answer:
            parts.append(f"Краткий ответ: {answer}")
        for res in data.get("results", [])[:4]:
            title = res.get("title", "")
            snippet = res.get("content", "")
            url = res.get("url", "")
            parts.append(f"- {title}: {snippet} ({url})")
        return "\n".join(parts)
    except Exception as e:
        logger.error("Ошибка Tavily-поиска: %s", e)
        return ""


async def process_query(query: str, user_id: int, chat_id: int) -> str:
    """Собрать контекст (последние CONTEXT_LIMIT сообщений) + поиск, отдать в AnyModel."""
    messages: list[dict] = [{"role": "system", "content": config.SYSTEM_PROMPT}]

    history = await get_history(chat_id, config.CONTEXT_LIMIT)
    for role, content in history:
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": content})

    full_query = query
    if needs_search(query):
        logger.info("Запущен веб-поиск по запросу: %s", query)
        results = await search_internet(query)
        if results:
            full_query = (
                f"{query}\n\n"
                f"[РЕЗУЛЬТАТЫ ПОИСКА В ИНТЕРНЕТЕ — используй их для ответа]\n{results}"
            )

    messages.append({"role": "user", "content": full_query})

    headers = {
        "Authorization": f"Bearer {config.ANYMODEL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.ANYMODEL_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                config.ANYMODEL_CHAT_URL, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("AnyModel HTTP %s: %s", resp.status, text[:300])
                    return ""
                data = await resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("Ошибка AnyModel: %s", e)
        return ""


# ================= ИЗВЛЕЧЕНИЕ ЗАПРОСА (ПРАВИЛО 3) =================

def extract_query(text: str) -> str | None:
    """
    Найти триггер упоминания бота (регистронезависимо, от длинного к короткому)
    и извлечь запрос после него. None — если запроса нет.
    """
    if not text:
        return None
    low = text.lower()
    for trigger in config.MENTION_TRIGGERS:  # уже отсортированы от длинного к короткому
        idx = low.find(trigger)
        if idx == -1:
            continue
        after = text[idx + len(trigger):]
        stripped = after.lstrip(" \t\u00a0.,!?;:-—«»\"'()[]{}")
        if len(stripped) < 3:
            return None
        return stripped
    return None


# ================= ПРАВИЛО 7: ПРИВЕТСТВИЕ В ГРУППЕ =================

@router.my_chat_member()
async def on_bot_added(event: Message):
    """Приветствие при добавлении бота в группу."""
    try:
        if event.new_chat_member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        ):
            adder = event.from_user
            adder_name = escape_html(adder.first_name if adder else "аноним")
            await event.answer(
                f"Спасибо что вы добавили меня, {adder_name}!\n\n"
                "Я — HentixAI: отвечаю на вопросы, помню историю чата, "
                "а также ищу информацию в интернете.\n\n"
                "Чтобы поговорить со мной, просто обратитесь ко мне по этим триггерам:\n"
                "@Hentix, @HentixAI, Hentix, Хентикс\n\n"
                "Если нашли ошибку, то пожалуйста сообщите в боте поддержки: "
                f"{config.SUPPORT_CHANNEL_USERNAME}"
            )
    except Exception as e:
        logger.error("Ошибка приветствия группы: %s", e)


# ================= КОМАНДЫ (ПРАВИЛО 5) =================

@router.message(CommandStart())
async def cmd_start(message: Message):
    """/start: создание юзера с балансом 100 (если новый), приветствие.
    Проверка подписки НЕ выполняется (удалена полностью)."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    first_name = escape_html(message.from_user.first_name)
    row = await get_user(user_id)
    balance = row["balance"] if row else config.START_BALANCE

    text = START_TEXT_TEMPLATE.format(
        first_name=first_name,
        balance=balance,
        bmin=config.DAILY_BONUS_MIN,
        bmax=config.DAILY_BONUS_MAX,
    )
    await message.answer(text, reply_markup=start_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    """
    /menu: в ЛС — те же кнопки, что и в /start;
    в группах/супергруппах — статистика чата (сообщения в памяти,
    уникальные юзеры, ответы бота).
    """
    if message.from_user is None:
        return
    await ensure_user(message.from_user.id, message.from_user.username)
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        # --- Статистика группы ---
        try:
            members = await bot.get_chat_members_count(message.chat.id)
        except Exception as e:
            logger.error("get_chat_members_count: %s", e)
            members = 0
        stats = await get_group_stats(message.chat.id)
        title = escape_html(message.chat.title or "Группа")
        lines = [
            f"📊 <b>Статистика группы {title}:</b>",
            "",
            f"👥 Участников: {members}",
            f"💬 Сообщений в памяти: {stats['total']}",
            f"🧑 Уникальных пользователей: {stats['unique_users']}",
            f"🤖 Ответов бота: {stats['bot_answers']}",
            "",
            "<b>Топ-3 активных участника:</b>",
        ]
        if stats["top"]:
            for i, row_ in enumerate(stats["top"], start=1):
                uid, uname, cnt = row_[0], row_[1], row_[2]
                display = escape_html(f"@{uname}") if uname else f"ID <code>{uid}</code>"
                lines.append(f"{i}. {display} — {cnt} сообщений")
        else:
            lines.append("<i>Пока нет данных</i>")
        await bot.send_message(chat_id=message.chat.id, text="\n".join(lines))
        return

    # --- ЛС: меню с кнопками (без проверок подписки) ---
    await message.answer("🎛 <b>Меню HentixAI</b>\n\nВыберите действие:",
                         reply_markup=start_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    """/help: справка."""
    if message.from_user is None:
        return
    await ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "ℹ️ <b>Справка HentixAI</b>\n\n"
        "Напишите мне любой вопрос — я отвечу через AI.\n"
        "Если вопрос требует актуальных данных (курс, новости, погода...) — "
        "я автоматически поищу в интернете.\n"
        "Inline-режим: напишите <code>@HentixAI_bot вопрос</code> в любом чате\n"
        "и нажмите кнопку ⚡️ Получить ответ.\n\n"
        "/start — приветствие и начало работы\n"
        "/menu — главное меню с кнопками\n"
        "/balance — проверить баланс монет\n"
        "/daily — вручную получить дневной бонус (раз в сутки)\n"
        "/debug — диагностика бота\n"
        "/clear_memory — очистить память чата\n"
        "/members — список участников группы (только в группах)\n\n"
        "<b>В группах:</b> упомяните <code>@HentixAI</code> или напишите «хентикс …»."
    )


def balance_text(balance: int) -> str:
    """Единый текст баланса с временем до следующего начисления."""
    t = (
        f"💰 Ваш баланс: <b>{balance}</b> монет\n"
        f"🎁 Следующее начисление в 00:00 МСК ({until_msk_midnight_str()})"
    )
    if balance <= 0:
        t += "\n⚠️ Монеты закончились! Дождитесь начисления в 00:00 МСК."
    return t


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    """/balance: баланс и время до начисления через обычное сообщение."""
    if message.from_user is None:
        return
    await ensure_user(message.from_user.id, message.from_user.username)
    row = await get_user(message.from_user.id)
    balance = row["balance"] if row else config.START_BALANCE
    await message.answer(balance_text(balance))


@router.message(Command("clear_memory"))
async def cmd_clear(message: Message):
    """/clear_memory: очищает chat_history текущего чата."""
    if message.from_user is None:
        return
    deleted = await clear_history(message.chat.id)
    await message.answer(f"🧹 Память очищена. Удалено записей: {deleted}.")


@router.message(Command("members"))
async def cmd_members(message: Message):
    """/members (только в группах): участники с эмодзи ролей (👑 🛡 👤)."""
    if message.from_user is None:
        return
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("⚠️ Команда /members работает только в группах.")
        return
    try:
        count = await bot.get_chat_members_count(message.chat.id)
        lines = [f"👥 Участников в группе: <b>{count}</b>", ""]
        emoji = {
            ChatMemberStatus.CREATOR: "👑",
            ChatMemberStatus.ADMINISTRATOR: "🛡",
            ChatMemberStatus.MEMBER: "👤",
        }
        admins = await bot.get_chat_administrators(message.chat.id)
        for ad in admins:  # список админов всегда небольшой
            name = escape_html(ad.user.first_name or str(ad.user.id))
            icon = emoji.get(ad.status, "👤")
            uname = f" (@{ad.user.username})" if ad.user.username else ""
            lines.append(f"{icon} <b>{name}</b>{uname}")
        lines.append("👤 Остальные участники — обычные члены группы.")
        await message.answer("\n".join(lines))
    except Exception as e:
        logger.error("Ошибка /members: %s", e)
        await message.answer("❌ Не удалось получить данные участников.")


async def give_manual_daily(user_id: int) -> tuple[int, int]:
    """
    /daily: ручное начисление дневного бонуса (20-30 монет), но не чаще
    одного раза в сутки (сравниваем last_daily_bonus по дате МСК).
    Возвращает (bonus, new_balance); bonus == 0 — бонус уже получен сегодня.
    """
    today_msk = msk_now().date().isoformat()
    row = await get_user(user_id)
    if row is None:
        return 0, 0
    last = (row["last_daily_bonus"] or "")[:10]
    if last == today_msk:
        return 0, row["balance"]
    bonus = random.randint(config.DAILY_BONUS_MIN, config.DAILY_BONUS_MAX)
    now = msk_now().isoformat(timespec="seconds")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ?, last_daily_bonus = ? "
            "WHERE user_id = ?",
            (bonus, now, user_id),
        )
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_balance = (await cur.fetchone())[0]
        await db.commit()
    logger.info("/daily: начислено %s монет пользователю %s", bonus, user_id)
    return bonus, new_balance


@router.message(Command("daily"))
async def cmd_daily(message: Message):
    """/daily: вручную получить дневной бонус (не чаще раза в сутки)."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    await ensure_user(user_id, message.from_user.username)
    try:
        bonus, balance = await give_manual_daily(user_id)
        if bonus == 0:
            await message.answer(
                "🎁 Дневной бонус уже получен сегодня." + "\n\n" + balance_text(balance)
            )
        else:
            await message.answer(
                "🎁 Ежедневное начисление!" + "\n\n" +
                f"Вам начислено {bonus} монет." + "\n"+
                f"Текущий баланс: {balance} монет." + "\n\n" +
                "Следующее начисление завтра в 00:00 МСК."
            )
    except Exception as e:
        logger.error("Ошибка /daily: %s", e)
        with contextlib.suppress(Exception):
            await message.answer("❌ Не удалось начислить бонус. Попробуйте позже.")


@router.message(Command("debug"))
async def cmd_debug(message: Message):
    """/debug: диагностика — версии, состояние БД, ключевые настройки."""
    if message.from_user is None:
        return
    try:
        import aiogram
        async with aiosqlite.connect(config.DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            n_users = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM chat_history")
            n_msgs = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT balance FROM users WHERE user_id = ?", (message.from_user.id,)
            )
            r = await cur.fetchone()
        my_balance = r[0] if r else "нет записи"
        text = (
            "🛠 <b>Диагностика HentixAI</b>" + "\n\n" +
            f"aiogram: <code>{aiogram.__version__}</code>" + "\n"+
            f"Python: <code>{sys.version.split()[0]}</code>" + "\n"+
            f"Platform: <code>{sys.platform}</code>" + "\n"+
            f"Пользователей в БД: {n_users}" + "\n"+
            f"Сообщений в памяти: {n_msgs}" + "\n"+
            f"Мой баланс: {my_balance}" + "\n"+
            f"Чат: <code>{message.chat.id}</code> (type={message.chat.type})" + "\n"+
            "Проверка подписки: <b>полностью отключена</b>" + "\n"+
            f"Авто-бонус в 00:00 МСК: {config.AUTO_DAILY_ENABLED}" + "\n"+
            "Inline: карточка id=1 с кнопкой '⚡ Получить ответ' "
            "(callback inline:...)" + "\n"+
            f"Время МСК сейчас: {msk_now().strftime('%H:%M:%S')}"
        )
        await message.answer(text)
    except Exception as e:
        logger.error("Ошибка /debug: %s", e)
        with contextlib.suppress(Exception):
            await message.answer(f"❌ Ошибка диагностики: {e}")


# ================= CALLBACK-ХЕНДЛЕРЫ =================

@router.callback_query(F.data == "show_balance")
async def callback_show_balance(callback: CallbackQuery):
    """Кнопка '💰 Баланс': текущий баланс и время до следующего начисления."""
    await callback.answer()  # обязательно — убирает "часики" на кнопке
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username)
    row = await get_user(user_id)
    balance = row["balance"] if row else config.START_BALANCE
    text = balance_text(balance)
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: callback.message может быть None —
    # шлём новое сообщение напрямую через chat_id.
    chat_id = callback.message.chat.id if callback.message is not None else user_id
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error("Ошибка отправки баланса %s: %s", user_id, e)
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id=user_id, text=text)


# ================= INLINE MODE (ЗАДАЧА 2) =================

@router.inline_query()
async def on_inline_query(inline_query):
    """
    ЗАДАЧА 2: пользователь пишет '@HentixAI_bot [вопрос]' в любом чате.
    Возвращаем ОДНУ карточку InlineQueryResultArticle:
      id="1", title="Ваш запрос", description=query[:50],
      input_message_content=InputTextMessageContent(message_text=query),
      reply_markup — кнопка "⚡ Получить ответ" (callback_data="inline:<query>").
    Нажатие на кнопку обрабатывает callback_inline_answer().
    """
    query = (inline_query.query or "").strip()
    if not query:
        results = [
            InlineQueryResultArticle(
                id="hint",
                title="Напишите ваш вопрос",
                description="Например: сколько сейчас курс доллара?",
                input_message_content=InputTextMessageContent(
                    message_text="ℹ️ Напишите вопрос после @HentixAI_bot"
                ),
            )
        ]
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⚡ Получить ответ",
                # Telegram ограничивает callback_data 64 байтами
                callback_data=("inline:" + query).encode("utf-8")[:64]
                .decode("utf-8", "ignore"),
            )],
        ])
        results = [
            InlineQueryResultArticle(
                id="1",
                title="Ваш запрос",
                description=query[:50],
                input_message_content=InputTextMessageContent(message_text=query),
                reply_markup=kb,
            )
        ]
    try:
        await inline_query.answer(results, cache_time=0, is_personal=True)
    except Exception as e:
        logger.error("Ошибка inline-ответа: %s", e)


# ============ CALLBACK "inline:<query>" (ЗАДАЧА 2) ============

@router.callback_query(F.data.startswith("inline:"))
async def callback_inline_answer(callback: CallbackQuery):
    """
    Нажатие на кнопку "⚡ Получить ответ" в inline-сообщении (ЗАДАЧА 2).

    Шаги строго по ТЗ:
      1. callback.answer() — убрать "часики" на кнопке.
      2. Извлечь query из callback.data (после "inline:"); если запрос был
         длиннее 64 байтов и обрезался — fallback: берём текст сообщения.
      3. Проверить баланс: <= 0 -> edit_text("Монеты закончились! ...").
      4. Баланс > 0:
         a) ОТПРАВИТЬ НОВОЕ СООБЩЕНИЕ "⌨️ Хентикс думает..." (reply);
         b) сохранить message_id этого сообщения;
         c) process_query(query, user_id, chat_id);
         d) evaluate_response(answer);
         e) True  -> списать 1 монету, РЕДАКТИРОВАТЬ статусное сообщение;
         f) False -> РЕДАКТИРОВАТЬ на answer + "🔍 Монета не списана".
      5. Всё обёрнуто в try/except с логированием.

    ВАЖНО: chat_id используется напрямую только при наличии callback.message;
    иначе — callback.inline_message_id. Редактирование всегда через
    bot.edit_message_text(chat_id=..., message_id=...).
    """
    await callback.answer()  # обязательно — убираем "часики"

    user_id = callback.from_user.id
    username = callback.from_user.username
    await ensure_user(user_id, username)

    # ---- chat_id: message.chat.id если message доступен, иначе inline_message_id ----
    if callback.message is not None and callback.message.chat is not None:
        chat_id = callback.message.chat.id
    else:
        chat_id = callback.inline_message_id  # для inline-результатов message=None
    msg_id = callback.message.message_id if callback.message is not None else None

    # ---- извлекаем query из callback_data ----
    raw = callback.data or ""
    query = raw[len("inline:"):] if raw.startswith("inline:") else ""
    # fallback: callback_data ограничена 64 байтами — длинный запрос мог
    # обрезаться; тогда берём текст inline-сообщения целиком.
    if (not query or len(query.strip()) < 3) and callback.message is not None:
        mt = (callback.message.text or "").strip()
        if mt:
            query = extract_query(mt) or mt

    if not query or len(query.strip()) < 3:
        logger.warning("inline-callback: пустой query от пользователя %s", user_id)
        return
    query = query.strip()

    logger.info("Inline-запрос от %s: %s", user_id, query[:80])

    try:
        # ---- проверка баланса ----
        row = await get_user(user_id)
        balance = row["balance"] if row else config.START_BALANCE
        if balance <= 0:
            dead_text = "💸 Монеты закончились! Ждите начисления в 00:00 МСК."
            if chat_id is not None and msg_id is not None:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=dead_text
                )
            else:
                await bot.send_message(chat_id=user_id, text=dead_text)
            return

        # ---- a) отправляем НОВОЕ сообщение "Хентикс думает..." (reply) ----
        with contextlib.suppress(Exception):
            await bot.send_chat_action(
                chat_id if chat_id is not None else user_id, "typing"
            )
        status_msg = await bot.send_message(
            chat_id=callback.from_user.id,
            text="⌨️ Хентикс думает...",
            reply_to_message_id=msg_id,
        )
        # ---- b) сохраняем message_id статусного сообщения ----
        status_chat_id = status_msg.chat.id
        status_msg_id = status_msg.message_id
        asyncio.create_task(_keep_typing(status_chat_id))

        # ---- c) обработка запроса ----
        answer = await process_query(query, user_id, status_chat_id)

        # ---- d/e/f) оценка ответа, списание монеты, финальный текст ----
        if evaluate_response(answer):
            await add_balance(user_id, -config.COST_PER_REQUEST)  # e) -1 монета
            final = answer[:4000]
        else:
            if not answer:
                answer = "😔 Сервис временно недоступен, попробуйте ещё раз."
            # f) монета НЕ списана
            final = (answer + "\n\n" + "🔍 Монета не списана")[:4000]

        # ---- память диалога ----
        try:
            await save_message(status_chat_id, user_id, username, "user", query)
            await save_message(status_chat_id, user_id, username, "assistant", answer)
        except Exception as e:
            logger.error("Ошибка сохранения inline-памяти: %s", e)

        # ---- редактируем СВОЁ статусное сообщение на готовый ответ ----
        await bot.edit_message_text(
            chat_id=status_chat_id, message_id=status_msg_id, text=final
        )
    except Exception as e:
        logger.exception("Ошибка обработки inline-callback: %s", e)
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=callback.from_user.id,
                text="❌ Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
            )


# ================= ОСНОВНОЙ ТЕКСТОВЫЙ ХЕНДЛЕР (ЗАДАЧА 3: группы и ЛС, БЕЗ проверки подписки) =================

@router.message(F.text)
async def handle_text(message: Message):
    """
    Универсальный хендлер текстовых сообщений: ЛС / группы / супергруппы /
    сообщения, отправленные через Inline Mode.

    Проверка подписки НЕ выполняется (полностью удалена).
    Шаги:
    1. Регистрация пользователя (новый -> баланс START_BALANCE=100).
    2. Извлечение запроса: в ЛС — весь текст; в группах — только если найден
       триггер (@HentixAI_bot, @HentixAI, @Hentix, хентикс, hentix).
    3. Проверка баланса (0 -> "Монеты закончились").
    4. typing + статусное сообщение "⌨️ Хентикс думает..." (reply).
    5. process_query -> evaluate_response -> списание монеты (если ок) ->
       bot.edit_message_text(chat_id, message_id статусного сообщения).
    """
    user = message.from_user
    if not user or user.is_bot:
        return

    # Дедуп дублирующихся updates (Telegram может прислать одно сообщение дважды)
    now_ts = asyncio.get_running_loop().time()
    mkey = (message.chat.id, message.message_id)
    prev = _MSG_DEDUP.get(mkey)
    if prev is not None and now_ts - prev < 8.0:
        logger.info("Дубликат message update %s — пропущен", mkey)
        return
    _MSG_DEDUP[mkey] = now_ts
    if len(_MSG_DEDUP) > 200:
        for k in [k for k, v in _MSG_DEDUP.items() if now_ts - v > 60.0]:
            _MSG_DEDUP.pop(k, None)

    # ---- ШАГ 1: регистрация пользователя (без проверок подписки) ----
    await ensure_user(user.id, user.username)

    in_private = message.chat.type == ChatType.PRIVATE
    query = extract_query(message.text)

    # В группах без триггера — просто сохраняем сообщение в память
    if not in_private and query is None:
        if not message.text.startswith("/"):
            await save_message(
                message.chat.id, user.id, user.username, "user", message.text
            )
        return

    # В ЛС: команда не распознана и триггера нет — спрашиваем напрямую
    # (сюда же попадают сообщения, отправленные через Inline Mode)
    if in_private and query is None:
        query = message.text.strip()

    if not query or len(query) < 3:
        return

    logger.info("Запрос от %s в чате %s: %s", user.id, message.chat.id, query[:80])

    # ---- ШАГ 2: ПРОВЕРКА БАЛАНСА ----
    row = await get_user(user.id)
    if row is None or row["balance"] < config.COST_PER_REQUEST:
        await message.answer(
            "💸 Монеты закончились! Дождитесь ежедневного начисления "
            "в 00:00 МСК (+20-30 монет)."
        )
        return

    # ---- ШАГ 3: typing + статусное сообщение "Хентикс думает..." ----
    # ВАЖНО: chat_id берём из message.chat.id.
    with contextlib.suppress(Exception):
        await message.answer_chat_action("typing")
    try:
        status_msg = await bot.send_message(
            chat_id=message.chat.id,
            text="⌨️ Хентикс думает...",
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.warning("Не удалось отправить статусное сообщение: %s", e)
        status_msg = None
    if status_msg is not None:
        asyncio.create_task(_keep_typing(message.chat.id))

    # ---- ШАГ 4: обработка запроса ----
    try:
        answer = await process_query(query, user.id, message.chat.id)
    except Exception as e:
        logger.error("Ошибка обработки запроса: %s", e)
        answer = ""

    # ---- ШАГ 5: оценка ответа и списание монеты ----
    if evaluate_response(answer):
        # Качественный ответ — списываем 1 монету
        await add_balance(user.id, -config.COST_PER_REQUEST)
        final = answer[:4000]
    else:
        # Плохой ответ — монета НЕ списывается
        if not answer:
            answer = "😔 Сервис временно недоступен, попробуйте ещё раз."
        final = (answer + "\n\n🔍 Монета не списана")[:4000]

    # ---- ШАГ 6: память ----
    try:
        await save_message(message.chat.id, user.id, user.username, "user", query)
        await save_message(message.chat.id, user.id, user.username, "assistant", answer)
    except Exception as e:
        logger.error("Ошибка сохранения в память: %s", e)

    # ---- ШАГ 7: редактируем СВОЁ статусное сообщение на готовый ответ ----
    if status_msg is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=final,
            )
        except Exception as e:
            logger.warning("Не удалось отредактировать статус: %s", e)
            with contextlib.suppress(Exception):
                await message.answer(final)
    else:
        with contextlib.suppress(Exception):
            await message.answer(final)


async def _keep_typing(chat_id: int, max_wait: float = 55.0) -> None:
    """Держать статус 'typing', пока идёт генерация ответа."""
    end = asyncio.get_running_loop().time() + max_wait
    try:
        while asyncio.get_running_loop().time() < end:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except Exception:
        pass


# ================= ЗАПУСК =================

async def main():
    await init_db()
    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.error("Не удалось получить info бота: %s", e)
    # Фоновая asyncio-задача авто-начисления монет в 00:00 МСК
    if config.AUTO_DAILY_ENABLED:
        asyncio.create_task(auto_daily_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
