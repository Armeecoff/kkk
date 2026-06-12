import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.parse import unquote
from aiohttp import web

logger = logging.getLogger(__name__)
DB_PATH = "bot.db"

LEVEL_THRESHOLDS = [0, 100, 250, 450, 700, 1000, 1400, 1900, 2500, 3200, 4000]


def db_get():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_init_data_user_full(init_data: str):
    """Returns (user_id, full_name, username)"""
    try:
        params = {}
        for part in init_data.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                params[unquote(k)] = unquote(v)
        user = json.loads(params.get('user', '{}'))
        uid = int(user.get('id', 0))
        first_name = user.get('first_name', '')
        last_name = user.get('last_name', '')
        full_name = (first_name + (' ' + last_name if last_name else '')).strip()
        username = user.get('username', '')
        return (uid if uid else None), full_name, username
    except Exception:
        return None, '', ''


def parse_init_data_user(init_data: str):
    uid, _, _ = parse_init_data_user_full(init_data)
    return uid


def get_or_create_user(user_id: int, username: str = '') -> dict:
    conn = db_get()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?,?)",
            (user_id, username or ''),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row)


def calc_level(total_clicks: int) -> int:
    level = 1
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if total_clicks >= threshold:
            level = i + 1
    return min(level, len(LEVEL_THRESHOLDS))


async def get_admin_id() -> int:
    try:
        from config import ADMIN_ID
        return ADMIN_ID
    except Exception:
        return int(os.getenv('ADMIN_ID', '0'))


async def get_premium_price() -> int:
    try:
        from config import PREMIUM_PRICE_CLICKS
        return PREMIUM_PRICE_CLICKS
    except Exception:
        return int(os.getenv('PREMIUM_PRICE_CLICKS', '50000'))


def get_init_data(request) -> str:
    return request.headers.get('X-Init-Data', '')


async def check_admin(request):
    init_data = get_init_data(request)
    user_id = parse_init_data_user(init_data)
    admin_id = await get_admin_id()
    return user_id if (user_id and user_id == admin_id) else None


async def serve_index(request):
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def api_config(request):
    admin_id = await get_admin_id()
    return web.json_response({"admin_id": admin_id})


async def api_state(request):
    init_data = get_init_data(request)
    user_id, full_name, username = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    admin_id = await get_admin_id()
    user = get_or_create_user(user_id, username)

    conn = db_get()
    items = conn.execute("SELECT * FROM shop_items WHERE is_active=1").fetchall()
    purchases = conn.execute(
        "SELECT item_id, SUM(quantity) as qty FROM purchases WHERE user_id=? GROUP BY item_id",
        (user_id,)
    ).fetchall()
    conn.close()

    now_str = datetime.utcnow().isoformat()
    is_premium = bool(user.get('premium_until') and user['premium_until'] > now_str)
    purch_map = {p['item_id']: p['qty'] for p in purchases}

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

    return web.json_response({
        "ok": True,
        "first_name": full_name or user.get('username') or 'Игрок',
        "username": username or user.get('username') or '',
        "user": {
            "balance": user["balance"],
            "total_clicks": user["total_clicks"],
            "level": user["level"],
            "is_premium": is_premium,
            "premium_until": user["premium_until"],
            "skin": user["skin"],
            "theme": user["theme"],
            "accent_color": user["accent_color"],
            "is_admin": user_id == admin_id,
        },
        "items": items_list,
        "levels": LEVEL_THRESHOLDS,
    })


async def api_click(request):
    init_data = get_init_data(request)
    user_id, _, username = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
        count = min(int(body.get('count', 1)), 50)
    except Exception:
        count = 1

    get_or_create_user(user_id, username)
    now = time.time()
    conn = db_get()
    last_click = conn.execute("SELECT last_click FROM users WHERE user_id=?", (user_id,)).fetchone()["last_click"]
    if now - last_click < 0.08:
        conn.close()
        return web.json_response({"ok": False, "error": "Too fast"})

    user_row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    new_balance = user_row["balance"] + count
    new_total = user_row["total_clicks"] + count
    new_level = calc_level(new_total)
    conn.execute(
        "UPDATE users SET balance=?, total_clicks=?, level=?, last_click=? WHERE user_id=?",
        (new_balance, new_total, new_level, now, user_id)
    )
    conn.execute("INSERT INTO clicks (user_id, click_time) VALUES (?,?)", (user_id, now))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True, "balance": new_balance, "level": new_level, "total_clicks": new_total})


async def api_buy(request):
    init_data = get_init_data(request)
    user_id, _, username = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
        item_id = int(body.get('item_id', 0))
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)

    get_or_create_user(user_id, username)
    conn = db_get()
    item = conn.execute("SELECT * FROM shop_items WHERE id=? AND is_active=1", (item_id,)).fetchone()
    user_row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    if not item:
        conn.close()
        return web.json_response({"ok": False, "error": "Товар не найден"})
    if user_row["balance"] < item["price"]:
        conn.close()
        return web.json_response({"ok": False, "error": "Недостаточно кликов"})
    if user_row["level"] < item["min_level"]:
        conn.close()
        return web.json_response({"ok": False, "error": f"Нужен уровень {item['min_level']}"})
    if item["item_limit"] != -1:
        owned = conn.execute(
            "SELECT SUM(quantity) FROM purchases WHERE user_id=? AND item_id=?",
            (user_id, item_id)
        ).fetchone()[0] or 0
        if owned >= item["item_limit"]:
            conn.close()
            return web.json_response({"ok": False, "error": "Лимит покупок исчерпан"})

    conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (item["price"], user_id))
    conn.execute(
        "INSERT INTO purchases (user_id, item_id, quantity, bought_at) VALUES (?,?,?,?)",
        (user_id, item_id, 1, datetime.utcnow().isoformat())
    )

    extra = {}
    if item["type"] == "skin":
        conn.execute("UPDATE users SET skin=? WHERE user_id=?", (item["name"], user_id))
    elif item["type"] == "vpn":
        vpn = conn.execute("SELECT * FROM vpn_configs WHERE is_sold=0 LIMIT 1").fetchone()
        if vpn:
            conn.execute("UPDATE vpn_configs SET is_sold=1 WHERE id=?", (vpn["id"],))
            extra["vpn_config"] = vpn["config_text"]
    elif item["type"] in ("multiplier", "boost", "autoclicker"):
        duration = 3600 if item["type"] == "multiplier" else (600 if item["type"] == "boost" else None)
        extra["boost"] = {"type": item["type"], "value": item["value"], "duration_seconds": duration}

    conn.commit()
    new_balance = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]
    conn.close()
    return web.json_response({"ok": True, "balance": new_balance, **extra})


async def api_set_skin(request):
    init_data = get_init_data(request)
    user_id, _, username = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        body = await request.json()
        skin = body.get('skin', 'basic')
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)
    get_or_create_user(user_id, username)
    conn = db_get()
    conn.execute("UPDATE users SET skin=? WHERE user_id=?", (skin, user_id))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True})


async def api_buy_premium(request):
    init_data = get_init_data(request)
    user_id, _, username = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    premium_price = await get_premium_price()
    get_or_create_user(user_id, username)
    conn = db_get()
    user_row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    if user_row["balance"] < premium_price:
        conn.close()
        return web.json_response({"ok": False, "error": f"Нужно {premium_price} кликов"})

    now_dt = datetime.utcnow()
    current_until = user_row["premium_until"]
    if current_until and current_until > now_dt.isoformat():
        base = datetime.fromisoformat(current_until)
    else:
        base = now_dt
    new_until = (base + timedelta(days=30)).isoformat()

    conn.execute(
        "UPDATE users SET balance=balance-?, premium_until=? WHERE user_id=?",
        (premium_price, new_until, user_id)
    )
    conn.commit()
    new_balance = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]
    conn.close()
    return web.json_response({"ok": True, "balance": new_balance, "premium_until": new_until})


async def api_set_theme(request):
    init_data = get_init_data(request)
    user_id, _, username = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        body = await request.json()
        theme = body.get('theme', 'dark')
        accent = body.get('accent_color', 'purple')
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)
    get_or_create_user(user_id, username)
    conn = db_get()
    conn.execute("UPDATE users SET theme=?, accent_color=? WHERE user_id=?", (theme, accent, user_id))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True})


async def api_set_premium_skin_url(request):
    init_data = get_init_data(request)
    user_id, _, _ = parse_init_data_user_full(init_data)
    if not user_id:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    return web.json_response({"ok": True})


async def api_admin_stats(request):
    if not await check_admin(request):
        return web.json_response({"ok": False, "error": "Нет доступа"}, status=403)
    conn = db_get()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_clicks = conn.execute("SELECT SUM(total_clicks) FROM users").fetchone()[0] or 0
    now_str = datetime.utcnow().isoformat()
    premium = conn.execute(
        "SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > ?",
        (now_str,)
    ).fetchone()[0]
    vpn_total = conn.execute("SELECT COUNT(*) FROM vpn_configs").fetchone()[0]
    vpn_free = conn.execute("SELECT COUNT(*) FROM vpn_configs WHERE is_sold=0").fetchone()[0]
    top10 = conn.execute(
        "SELECT username, user_id, total_clicks FROM users ORDER BY total_clicks DESC LIMIT 10"
    ).fetchall()
    conn.close()
    return web.json_response({
        "ok": True,
        "users": users,
        "total_clicks": total_clicks,
        "premium": premium,
        "vpn_total": vpn_total,
        "vpn_free": vpn_free,
        "top10": [{"name": r["username"] or str(r["user_id"]), "clicks": r["total_clicks"]} for r in top10],
    })


async def api_admin_vpns(request):
    if not await check_admin(request):
        return web.json_response({"ok": False, "error": "Нет доступа"}, status=403)
    page = max(1, int(request.rel_url.query.get('page', 1)))
    per_page = 20
    offset = (page - 1) * per_page
    conn = db_get()
    total = conn.execute("SELECT COUNT(*) FROM vpn_configs").fetchone()[0]
    configs = conn.execute(
        "SELECT id, config_text, is_sold FROM vpn_configs ORDER BY id LIMIT ? OFFSET ?",
        (per_page, offset)
    ).fetchall()
    conn.close()
    return web.json_response({
        "ok": True,
        "total": total,
        "page": page,
        "configs": [{"id": c["id"], "config_text": c["config_text"], "is_sold": bool(c["is_sold"])} for c in configs],
    })


async def api_admin_parse_vpn(request):
    if not await check_admin(request):
        return web.json_response({"ok": False, "error": "Нет доступа"}, status=403)
    import aiohttp as _aiohttp
    VPN_SOURCE = "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt"
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(VPN_SOURCE, timeout=_aiohttp.ClientTimeout(total=20)) as resp:
                text = await resp.text()
        configs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        conn = db_get()
        added = 0
        for cfg in configs:
            if not conn.execute("SELECT id FROM vpn_configs WHERE config_text=?", (cfg,)).fetchone():
                conn.execute("INSERT INTO vpn_configs (config_text) VALUES (?)", (cfg,))
                added += 1
        conn.commit()
        conn.close()
        return web.json_response({"ok": True, "total": len(configs), "added": added})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})


async def api_admin_balance(request):
    if not await check_admin(request):
        return web.json_response({"ok": False, "error": "Нет доступа"}, status=403)
    try:
        body = await request.json()
        target_uid = int(body["user_id"])
        amount = int(body["amount"])
    except Exception:
        return web.json_response({"ok": False, "error": "Неверные параметры"}, status=400)
    conn = db_get()
    if not conn.execute("SELECT user_id FROM users WHERE user_id=?", (target_uid,)).fetchone():
        conn.close()
        return web.json_response({"ok": False, "error": "Пользователь не найден"})
    conn.execute("UPDATE users SET balance=MAX(0, balance+?) WHERE user_id=?", (amount, target_uid))
    conn.commit()
    new_bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (target_uid,)).fetchone()["balance"]
    conn.close()
    return web.json_response({"ok": True, "new_balance": new_bal})


async def start_web():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/index.html", serve_index)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/state", api_state)
    app.router.add_post("/api/click", api_click)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_post("/api/set_skin", api_set_skin)
    app.router.add_post("/api/buy_premium", api_buy_premium)
    app.router.add_post("/api/set_theme", api_set_theme)
    app.router.add_post("/api/set_premium_skin_url", api_set_premium_skin_url)
    app.router.add_post("/api/admin/stats", api_admin_stats)
    app.router.add_get("/api/admin/vpns", api_admin_vpns)
    app.router.add_post("/api/admin/parse_vpn", api_admin_parse_vpn)
    app.router.add_post("/api/admin/balance", api_admin_balance)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 5000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")
    return runner


async def main():
    logging.basicConfig(level=logging.INFO)
    runner = await start_web()
    try:
        from config import BOT_TOKEN
        if BOT_TOKEN:
            from main import main as bot_main, init_db
            init_db()
            logger.info("Starting Telegram bot...")
            await bot_main()
        else:
            logger.warning("BOT_TOKEN not configured — web server only")
            while True:
                await asyncio.sleep(3600)
    except Exception as e:
        logger.error(f"Bot error: {e}")
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
