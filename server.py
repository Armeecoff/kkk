import asyncio
import logging
import os
from aiohttp import web

logger = logging.getLogger(__name__)

async def serve_index(request):
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html", charset="utf-8")

async def start_web():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/index.html", serve_index)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 5000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")
    return runner

async def main():
    logging.basicConfig(level=logging.INFO)

    # Start web server
    runner = await start_web()

    # Import and start bot (only if token is configured)
    try:
        from config import BOT_TOKEN
        if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
            from main import main as bot_main, init_db
            init_db()
            logger.info("Starting Telegram bot...")
            await bot_main()
        else:
            logger.warning("BOT_TOKEN not configured — running web server only (preview mode)")
            # Keep running forever
            while True:
                await asyncio.sleep(3600)
    except Exception as e:
        logger.error(f"Bot error: {e}")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
