import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import ADMIN_ID, BOT_TOKEN, PREMIUM_PRICE_CLICKS, WEBAPP_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "bot.db"
VPN_LIST_PATH = "vpn_list.json"
VPN_SOURCE_URL = "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt"

LEVEL_THRESHOLDS = [0, 100, 250, 450, 700, 1000, 1400, 1900, 2500, 3200, 4000]


# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            balance     INTEGER DEFAULT 0,
            total_clicks INTEGER DEFAULT 0,
            level       INTEGER DEFAULT 1,
            premium_until TEXT DEFAULT NULL,
            skin        TEXT DEFAULT 'basic',
            last_click  REAL DEFAULT 0,
            theme       TEXT DEFAULT 'dark',
            accent_color TEXT DEFAULT 'purple'
        );

        CREATE TABLE IF NOT EXISTS clicks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            click_time  REAL
        );

        CREATE TABLE IF NOT EXISTS purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            item_id     INTEGER,
            quantity    INTEGER DEFAULT 1,
            bought_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS shop_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            description TEXT,
            type        TEXT,
            price       INTEGER,
            item_limit  INTEGER DEFAULT -1,
            min_level   INTEGER DEFAULT 1,
            image_url   TEXT DEFAULT '',
            value       REAL DEFAULT 1.0,
            is_active   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS vpn_configs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            config_text TEXT,
            is_sold     INTEGER DEFAULT 0
        );
    """)
    conn.commit()

    c.execute("SELECT COUNT(*) FROM shop_items")
    if c.fetchone()[0] == 0:
        default_items = [
            ("Множитель x2", "Удваивает каждый клик на 1 час", "multiplier", 500, -1, 1, "", 2.0),
            ("Множитель x3", "Утраивает каждый клик на 1 час", "multiplier", 1500, -1, 3, "", 3.0),
            ("Автокликер (5/сек)", "Автоматически кликает 5 раз в секунду", "autoclicker", 3000, 1, 5, "", 5.0),
            ("Буст на 10 минут", "x5 к кликам на 10 минут", "boost", 800, -1, 2, "", 5.0),
            ("Радужный скин", "Красивый радужный скин кнопки", "skin", 5000, 1, 1, "", 1.0),
        ]
        c.executemany(
            "INSERT INTO shop_items (name, description, type, price, item_limit, min_level, image_url, value) VALUES (?,?,?,?,?,?,?,?)",
            default_items,
        )
        conn.commit()
    conn.close()


def get_or_create_user(user_id: int, username: str = ""):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute(
            "INSERT INTO users (user_id, username) VALUES (?,?)",
            (user_id, username or ""),
        )
        conn.commit()
        c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
    conn.close()
    return dict(row)


def calc_level(total_clicks: int) -> int:
    level = 1
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if total_clicks >= threshold:
            level = i + 1
    return min(level, len(LEVEL_THRESHOLDS))


# ─── FSM States ──────────────────────────────────────────────────────────────

class AdminStates(StatesGroup):
    waiting_add_name = State()
    waiting_add_desc = State()
    waiting_add_type = State()
    waiting_add_price = State()
    waiting_add_limit = State()
    waiting_add_min_level = State()
    waiting_add_image = State()
    waiting_add_value = State()

    waiting_edit_item_id = State()
    waiting_edit_field = State()
    waiting_edit_value = State()

    waiting_delete_id = State()

    waiting_balance_user_id = State()
    waiting_balance_amount = State()

    waiting_vpn_item_name = State()
    waiting_vpn_item_price = State()
    waiting_vpn_config_id = State()


# ─── Keyboards ───────────────────────────────────────────────────────────────

def admin_main_kb():
    buttons = [
        [
            InlineKeyboardButton(text="🛍 Товары", callback_data="admin_items"),
            InlineKeyboardButton(text="💰 Баланс", callback_data="admin_balance"),
        ],
        [
            InlineKeyboardButton(text="🌐 VPN парсер", callback_data="admin_vpn"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_items_kb():
    buttons = [
        [
            InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_item"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data="admin_edit_item"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить товар", callback_data="admin_delete_item"),
            InlineKeyboardButton(text="📋 Список товаров", callback_data="admin_list_items"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_kb():
    buttons = [
        [
            InlineKeyboardButton(text="🔄 Парсить VPN", callback_data="admin_parse_vpn"),
            InlineKeyboardButton(text="📋 Список конфигов", callback_data="admin_list_vpn:0"),
        ],
        [
            InlineKeyboardButton(text="➕ VPN-товар", callback_data="admin_add_vpn_item"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_kb(callback: str = "admin_back"):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=callback)]]
    )


# ─── Router ──────────────────────────────────────────────────────────────────

router = Router()


# ─── /start ──────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    user = get_or_create_user(message.from_user.id, message.from_user.username or "")
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎮 Открыть игру", web_app=WebAppInfo(url=WEBAPP_URL))]
        ],
        resize_keyboard=True,
    )
    await message.answer(
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        f"🎮 Добро пожаловать в <b>Tap & Earn</b>!\n"
        f"Кликай, зарабатывай, прокачивайся!\n\n"
        f"💰 Баланс: <b>{user['balance']}</b> кликов\n"
        f"⭐ Уровень: <b>{user['level']}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ─── /admin ──────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("⛔ Нет доступа.")
    await message.answer(
        "⚙️ <b>Панель администратора</b>\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=admin_main_kb(),
    )


@router.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "⚙️ <b>Панель администратора</b>\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=admin_main_kb(),
    )


@router.callback_query(F.data == "admin_close")
async def admin_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()


# ─── Admin: Items ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_items")
async def admin_items(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_text(
        "🛍 <b>Управление товарами</b>",
        parse_mode="HTML",
        reply_markup=admin_items_kb(),
    )


@router.callback_query(F.data == "admin_list_items")
async def admin_list_items(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    items = conn.execute("SELECT * FROM shop_items WHERE is_active=1").fetchall()
    conn.close()
    if not items:
        text = "📭 Товаров нет."
    else:
        text = "📋 <b>Список товаров:</b>\n\n"
        for item in items:
            text += (
                f"<b>#{item['id']}</b> {item['name']}\n"
                f"   Тип: {item['type']} | Цена: {item['price']} | Лимит: {item['item_limit']} | Уровень: {item['min_level']}\n"
                f"   Описание: {item['description']}\n\n"
            )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb("admin_items"))


@router.callback_query(F.data == "admin_add_item")
async def admin_add_item_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_add_name)
    await call.message.edit_text(
        "➕ <b>Добавление товара</b>\n\nВведите <b>название</b> товара:",
        parse_mode="HTML",
        reply_markup=back_kb("admin_items"),
    )


@router.message(AdminStates.waiting_add_name)
async def admin_add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AdminStates.waiting_add_desc)
    await message.answer("Введите <b>описание</b> товара:", parse_mode="HTML")


@router.message(AdminStates.waiting_add_desc)
async def admin_add_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AdminStates.waiting_add_type)
    await message.answer(
        "Выберите <b>тип</b> товара:\n"
        "<code>multiplier</code> — множитель кликов\n"
        "<code>autoclicker</code> — автокликер\n"
        "<code>boost</code> — временный буст\n"
        "<code>skin</code> — скин\n"
        "<code>vpn</code> — VPN-конфиг",
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_add_type)
async def admin_add_type(message: Message, state: FSMContext):
    valid = ["multiplier", "autoclicker", "boost", "skin", "vpn"]
    if message.text.strip().lower() not in valid:
        return await message.answer("❌ Неверный тип. Введите один из: " + ", ".join(valid))
    await state.update_data(type=message.text.strip().lower())
    await state.set_state(AdminStates.waiting_add_price)
    await message.answer("Введите <b>цену</b> (в кликах):", parse_mode="HTML")


@router.message(AdminStates.waiting_add_price)
async def admin_add_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите целое число.")
    await state.update_data(price=int(message.text))
    await state.set_state(AdminStates.waiting_add_limit)
    await message.answer("Введите <b>лимит покупок</b> на пользователя (-1 = бесконечно):", parse_mode="HTML")


@router.message(AdminStates.waiting_add_limit)
async def admin_add_limit(message: Message, state: FSMContext):
    try:
        val = int(message.text)
    except ValueError:
        return await message.answer("❌ Введите целое число.")
    await state.update_data(item_limit=val)
    await state.set_state(AdminStates.waiting_add_min_level)
    await message.answer("Введите <b>минимальный уровень</b> для покупки:", parse_mode="HTML")


@router.message(AdminStates.waiting_add_min_level)
async def admin_add_min_level(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите целое число.")
    await state.update_data(min_level=int(message.text))
    await state.set_state(AdminStates.waiting_add_image)
    await message.answer("Введите <b>URL изображения</b> (или <code>-</code> чтобы пропустить):", parse_mode="HTML")


@router.message(AdminStates.waiting_add_image)
async def admin_add_image(message: Message, state: FSMContext):
    img = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(image_url=img)
    await state.set_state(AdminStates.waiting_add_value)
    await message.answer(
        "Введите <b>значение</b> (для множителя — число вроде 2.0 или 3.0, для автокликера — количество кликов/сек):",
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_add_value)
async def admin_add_value(message: Message, state: FSMContext):
    try:
        val = float(message.text)
    except ValueError:
        return await message.answer("❌ Введите число.")
    data = await state.get_data()
    await state.clear()
    conn = get_db()
    conn.execute(
        "INSERT INTO shop_items (name, description, type, price, item_limit, min_level, image_url, value) VALUES (?,?,?,?,?,?,?,?)",
        (data["name"], data["description"], data["type"], data["price"], data["item_limit"], data["min_level"], data["image_url"], val),
    )
    conn.commit()
    conn.close()
    await message.answer(
        f"✅ Товар <b>{data['name']}</b> добавлен!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню товаров", callback_data="admin_items")]]
        ),
    )


@router.callback_query(F.data == "admin_edit_item")
async def admin_edit_item_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_edit_item_id)
    await call.message.edit_text(
        "✏️ <b>Редактирование товара</b>\n\nВведите <b>ID товара</b>:",
        parse_mode="HTML",
        reply_markup=back_kb("admin_items"),
    )


@router.message(AdminStates.waiting_edit_item_id)
async def admin_edit_item_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите число.")
    conn = get_db()
    item = conn.execute("SELECT * FROM shop_items WHERE id=?", (int(message.text),)).fetchone()
    conn.close()
    if not item:
        return await message.answer("❌ Товар не найден.")
    await state.update_data(edit_item_id=int(message.text))
    await state.set_state(AdminStates.waiting_edit_field)
    await message.answer(
        f"Товар: <b>{item['name']}</b>\n\n"
        "Введите поле для редактирования:\n"
        "<code>name, description, type, price, item_limit, min_level, image_url, value</code>",
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_edit_field)
async def admin_edit_field(message: Message, state: FSMContext):
    valid_fields = ["name", "description", "type", "price", "item_limit", "min_level", "image_url", "value"]
    if message.text.strip() not in valid_fields:
        return await message.answer("❌ Неверное поле.")
    await state.update_data(edit_field=message.text.strip())
    await state.set_state(AdminStates.waiting_edit_value)
    await message.answer(f"Введите новое значение для поля <code>{message.text.strip()}</code>:", parse_mode="HTML")


@router.message(AdminStates.waiting_edit_value)
async def admin_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    conn = get_db()
    conn.execute(
        f"UPDATE shop_items SET {data['edit_field']}=? WHERE id=?",
        (message.text.strip(), data["edit_item_id"]),
    )
    conn.commit()
    conn.close()
    await message.answer("✅ Товар обновлён!")


@router.callback_query(F.data == "admin_delete_item")
async def admin_delete_item_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_delete_id)
    await call.message.edit_text(
        "🗑 <b>Удаление товара</b>\n\nВведите <b>ID товара</b>:",
        parse_mode="HTML",
        reply_markup=back_kb("admin_items"),
    )


@router.message(AdminStates.waiting_delete_id)
async def admin_delete_item(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите число.")
    await state.clear()
    conn = get_db()
    conn.execute("UPDATE shop_items SET is_active=0 WHERE id=?", (int(message.text),))
    conn.commit()
    conn.close()
    await message.answer("✅ Товар удалён!")


# ─── Admin: Balance ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_balance")
async def admin_balance_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_balance_user_id)
    await call.message.edit_text(
        "💰 <b>Корректировка баланса</b>\n\nВведите <b>Telegram ID</b> пользователя:",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )


@router.message(AdminStates.waiting_balance_user_id)
async def admin_balance_user_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите числовой ID.")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (int(message.text),)).fetchone()
    conn.close()
    if not user:
        return await message.answer("❌ Пользователь не найден.")
    await state.update_data(target_user_id=int(message.text))
    await state.set_state(AdminStates.waiting_balance_amount)
    await message.answer(
        f"Пользователь: <b>{user['username'] or user['user_id']}</b>\n"
        f"Текущий баланс: <b>{user['balance']}</b>\n\n"
        "Введите сумму (<code>+500</code> или <code>-200</code>):",
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_balance_amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.replace("+", ""))
    except ValueError:
        return await message.answer("❌ Неверный формат. Пример: +500 или -200")
    data = await state.get_data()
    await state.clear()
    conn = get_db()
    conn.execute(
        "UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id=?",
        (amount, data["target_user_id"]),
    )
    conn.commit()
    user = conn.execute("SELECT balance FROM users WHERE user_id=?", (data["target_user_id"],)).fetchone()
    conn.close()
    sign = "+" if amount >= 0 else ""
    await message.answer(
        f"✅ Баланс изменён на <b>{sign}{amount}</b>\n"
        f"Новый баланс: <b>{user['balance']}</b>",
        parse_mode="HTML",
    )


# ─── Admin: VPN ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_vpn")
async def admin_vpn(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM vpn_configs").fetchone()[0]
    available = conn.execute("SELECT COUNT(*) FROM vpn_configs WHERE is_sold=0").fetchone()[0]
    conn.close()
    await call.message.edit_text(
        f"🌐 <b>VPN Конфиги</b>\n\n"
        f"Всего: <b>{total}</b>\nДоступно: <b>{available}</b>",
        parse_mode="HTML",
        reply_markup=admin_vpn_kb(),
    )


@router.callback_query(F.data == "admin_parse_vpn")
async def admin_parse_vpn(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("⏳ Парсинг...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(VPN_SOURCE_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
        configs = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
        with open(VPN_LIST_PATH, "w") as f:
            json.dump(configs, f, ensure_ascii=False)
        conn = get_db()
        added = 0
        for cfg in configs:
            exists = conn.execute("SELECT id FROM vpn_configs WHERE config_text=?", (cfg,)).fetchone()
            if not exists:
                conn.execute("INSERT INTO vpn_configs (config_text) VALUES (?)", (cfg,))
                added += 1
        conn.commit()
        conn.close()
        await call.message.edit_text(
            f"✅ Парсинг завершён!\nНайдено конфигов: <b>{len(configs)}</b>\nДобавлено новых: <b>{added}</b>",
            parse_mode="HTML",
            reply_markup=admin_vpn_kb(),
        )
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}", reply_markup=admin_vpn_kb())


@router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("admin_list_vpn"))
async def admin_list_vpn(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    page = 0
    if ":" in call.data:
        try:
            page = int(call.data.split(":")[1])
        except Exception:
            page = 0
    per_page = 8
    offset = page * per_page
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM vpn_configs").fetchone()[0]
    configs = conn.execute(
        "SELECT id, config_text, is_sold FROM vpn_configs ORDER BY id LIMIT ? OFFSET ?",
        (per_page, offset)
    ).fetchall()
    conn.close()
    total_pages = max(1, (total + per_page - 1) // per_page)
    if not configs and page == 0:
        text = "📭 Конфигов нет. Нажмите «Парсить VPN»."
    else:
        text = f"📋 <b>VPN Конфиги</b> (стр. {page+1}/{total_pages}, всего {total}):\n\n"
        for c in configs:
            status = "✅" if not c["is_sold"] else "❌ продан"
            cfg_text = c["config_text"][:150] + ("…" if len(c["config_text"]) > 150 else "")
            text += f"<b>#{c['id']}</b> {status}\n<code>{cfg_text}</code>\n\n"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_list_vpn:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_list_vpn:{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav, [InlineKeyboardButton(text="◀ Назад", callback_data="admin_vpn")]])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_add_vpn_item")
async def admin_add_vpn_item(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_vpn_item_name)
    await call.message.edit_text(
        "Введите <b>название</b> VPN-товара:",
        parse_mode="HTML",
        reply_markup=back_kb("admin_vpn"),
    )


@router.message(AdminStates.waiting_vpn_item_name)
async def admin_vpn_item_name(message: Message, state: FSMContext):
    await state.update_data(vpn_name=message.text.strip())
    await state.set_state(AdminStates.waiting_vpn_item_price)
    await message.answer("Введите <b>цену</b> (кликов):", parse_mode="HTML")


@router.message(AdminStates.waiting_vpn_item_price)
async def admin_vpn_item_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите целое число.")
    await state.update_data(vpn_price=int(message.text))
    await state.set_state(AdminStates.waiting_vpn_config_id)
    await message.answer(
        "Введите <b>ID конфига</b> из базы (или <code>auto</code> — выдавать следующий доступный):",
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_vpn_config_id)
async def admin_vpn_config_id(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    config_id = message.text.strip()
    img_url = ""
    if config_id != "auto" and not config_id.isdigit():
        return await message.answer("❌ Введите число или 'auto'.")
    conn = get_db()
    conn.execute(
        "INSERT INTO shop_items (name, description, type, price, item_limit, min_level, image_url, value) VALUES (?,?,?,?,?,?,?,?)",
        (data["vpn_name"], f"VPN-конфиг (config_id={config_id})", "vpn", data["vpn_price"], 1, 1, img_url, float(config_id) if config_id != "auto" else -1),
    )
    conn.commit()
    conn.close()
    await message.answer(f"✅ VPN-товар <b>{data['vpn_name']}</b> добавлен!", parse_mode="HTML")


# ─── Admin: Stats ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_clicks = conn.execute("SELECT SUM(total_clicks) FROM users").fetchone()[0] or 0
    now_str = datetime.utcnow().isoformat()
    premium_count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > ?",
        (now_str,),
    ).fetchone()[0]
    top10 = conn.execute(
        "SELECT username, user_id, total_clicks FROM users ORDER BY total_clicks DESC LIMIT 10"
    ).fetchall()
    conn.close()

    top_text = ""
    for i, u in enumerate(top10, 1):
        name = u["username"] or str(u["user_id"])
        top_text += f"  {i}. {name} — {u['total_clicks']}\n"

    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🖱 Всего кликов: <b>{total_clicks:,}</b>\n"
        f"⭐ Активных подписок: <b>{premium_count}</b>\n\n"
        f"🏆 <b>Топ-10 по кликам:</b>\n{top_text}"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())


# ─── WebApp Data Handler ──────────────────────────────────────────────────────

@router.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        return

    action = data.get("action")
    user_id = message.from_user.id
    username = message.from_user.username or ""

    user = get_or_create_user(user_id, username)

    if action == "click":
        count = int(data.get("count", 1))
        count = min(count, 50)
        import time
        now = time.time()
        conn = get_db()
        last_click = conn.execute("SELECT last_click FROM users WHERE user_id=?", (user_id,)).fetchone()["last_click"]
        if now - last_click < 0.08:
            conn.close()
            return
        new_balance = user["balance"] + count
        new_total = user["total_clicks"] + count
        new_level = calc_level(new_total)
        conn.execute(
            "UPDATE users SET balance=?, total_clicks=?, level=?, last_click=? WHERE user_id=?",
            (new_balance, new_total, new_level, now, user_id),
        )
        conn.execute("INSERT INTO clicks (user_id, click_time) VALUES (?,?)", (user_id, now))
        conn.commit()
        conn.close()
        await message.answer(
            json.dumps({"ok": True, "balance": new_balance, "level": new_level, "total": new_total}),
            reply_markup=ReplyKeyboardRemove(),
        )

    elif action == "get_state":
        conn = get_db()
        user_row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        items = conn.execute("SELECT * FROM shop_items WHERE is_active=1").fetchall()
        purchases = conn.execute(
            "SELECT item_id, SUM(quantity) as qty FROM purchases WHERE user_id=? GROUP BY item_id",
            (user_id,),
        ).fetchall()
        conn.close()
        now_str = datetime.utcnow().isoformat()
        is_premium = (
            user_row["premium_until"] is not None and user_row["premium_until"] > now_str
        )
        purch_map = {p["item_id"]: p["qty"] for p in purchases}
        items_list = [
            {
                "id": it["id"],
                "name": it["name"],
                "description": it["description"],
                "type": it["type"],
                "price": it["price"],
                "item_limit": it["item_limit"],
                "min_level": it["min_level"],
                "image_url": it["image_url"],
                "value": it["value"],
                "owned": purch_map.get(it["id"], 0),
            }
            for it in items
        ]
        resp = {
            "ok": True,
            "user": {
                "balance": user_row["balance"],
                "total_clicks": user_row["total_clicks"],
                "level": user_row["level"],
                "is_premium": is_premium,
                "premium_until": user_row["premium_until"],
                "skin": user_row["skin"],
                "theme": user_row["theme"],
                "accent_color": user_row["accent_color"],
                "is_admin": user_id == ADMIN_ID,
            },
            "items": items_list,
            "levels": LEVEL_THRESHOLDS,
        }
        await message.answer(json.dumps(resp, ensure_ascii=False))

    elif action == "buy":
        item_id = int(data.get("item_id", 0))
        conn = get_db()
        item = conn.execute("SELECT * FROM shop_items WHERE id=? AND is_active=1", (item_id,)).fetchone()
        user_row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

        if not item:
            conn.close()
            return await message.answer(json.dumps({"ok": False, "error": "Товар не найден"}))

        if user_row["balance"] < item["price"]:
            conn.close()
            return await message.answer(json.dumps({"ok": False, "error": "Недостаточно кликов"}))

        if user_row["level"] < item["min_level"]:
            conn.close()
            return await message.answer(json.dumps({"ok": False, "error": f"Нужен уровень {item['min_level']}"}))

        if item["item_limit"] != -1:
            owned = conn.execute(
                "SELECT SUM(quantity) FROM purchases WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            ).fetchone()[0] or 0
            if owned >= item["item_limit"]:
                conn.close()
                return await message.answer(json.dumps({"ok": False, "error": "Лимит покупок исчерпан"}))

        conn.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (item["price"], user_id),
        )
        conn.execute(
            "INSERT INTO purchases (user_id, item_id, quantity, bought_at) VALUES (?,?,?,?)",
            (user_id, item_id, 1, datetime.utcnow().isoformat()),
        )

        extra = {}
        if item["type"] == "skin":
            conn.execute("UPDATE users SET skin=? WHERE user_id=?", (item["name"], user_id))
        elif item["type"] == "vpn":
            cfg_id = int(item["value"]) if item["value"] != -1 else None
            if cfg_id and cfg_id > 0:
                vpn = conn.execute("SELECT * FROM vpn_configs WHERE id=?", (cfg_id,)).fetchone()
            else:
                vpn = conn.execute("SELECT * FROM vpn_configs WHERE is_sold=0 LIMIT 1").fetchone()
            if vpn:
                conn.execute("UPDATE vpn_configs SET is_sold=1 WHERE id=?", (vpn["id"],))
                extra["vpn_config"] = vpn["config_text"]
                await message.bot.send_message(
                    user_id,
                    f"🌐 <b>Ваш VPN-конфиг:</b>\n\n<code>{vpn['config_text']}</code>",
                    parse_mode="HTML",
                )

        conn.commit()
        new_balance = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]
        conn.close()
        await message.answer(json.dumps({"ok": True, "balance": new_balance, **extra}))

    elif action == "buy_premium":
        conn = get_db()
        user_row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user_row["balance"] < PREMIUM_PRICE_CLICKS:
            conn.close()
            return await message.answer(json.dumps({"ok": False, "error": f"Нужно {PREMIUM_PRICE_CLICKS} кликов"}))

        now_dt = datetime.utcnow()
        current_until = user_row["premium_until"]
        if current_until and current_until > now_dt.isoformat():
            base = datetime.fromisoformat(current_until)
        else:
            base = now_dt
        new_until = (base + timedelta(days=30)).isoformat()

        conn.execute(
            "UPDATE users SET balance=balance-?, premium_until=? WHERE user_id=?",
            (PREMIUM_PRICE_CLICKS, new_until, user_id),
        )
        conn.commit()
        new_balance = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]
        conn.close()
        await message.answer(json.dumps({"ok": True, "balance": new_balance, "premium_until": new_until}))

    elif action == "set_skin":
        skin = data.get("skin", "basic")
        conn = get_db()
        conn.execute("UPDATE users SET skin=? WHERE user_id=?", (skin, user_id))
        conn.commit()
        conn.close()
        await message.answer(json.dumps({"ok": True}))

    elif action == "set_theme":
        theme = data.get("theme", "dark")
        accent = data.get("accent_color", "purple")
        conn = get_db()
        conn.execute(
            "UPDATE users SET theme=?, accent_color=? WHERE user_id=?",
            (theme, accent, user_id),
        )
        conn.commit()
        conn.close()
        await message.answer(json.dumps({"ok": True}))

    elif action == "set_premium_skin_url":
        url = data.get("url", "")
        if not re.match(r"^https?://[^\s<>\"']+\.(jpg|jpeg|png|gif|webp)(\?.*)?$", url, re.IGNORECASE):
            return await message.answer(json.dumps({"ok": False, "error": "Неверный URL изображения"}))
        conn = get_db()
        conn.execute("UPDATE users SET skin=? WHERE user_id=?", (f"premium:{url}", user_id))
        conn.commit()
        conn.close()
        await message.answer(json.dumps({"ok": True}))


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
