import os
from aiohttp import web
from bot import client  # so /health can show bot name

PORT = int(os.getenv("PORT", "10000"))

async def handle_root(request):
    return web.Response(text="OK")

async def handle_health(request):
    name = str(client.user) if client.user else "not-ready"
    return web.json_response({"status": "alive", "bot": name})

async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start() 
