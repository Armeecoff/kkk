import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from urllib.parse import unquote
from aiohttp import web

logger = logging.getLogger(__name__)
DB_PATH = "bot.db"


def db_get():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_init_data_user(init_data: str) -> int | None:
    try:
        params = {}
        for part in init_data.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                params[unquote(k)] = unquote(v)
        user = json.loads(params.get('user', '{}'))
        uid = int(user.get('id', 0))
        return uid if uid else None
    except Exception:
        return None


async def get_admin_id() -> int:
    try:
        from config import ADMIN_ID
        return ADMIN_ID
    except Exception:
        return int(os.getenv('ADMIN_ID', '0'))


async def check_admin(request) -> int | None:
    init_data = request.headers.get('X-Init-Data', '')
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
