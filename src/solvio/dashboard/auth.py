"""Owner access to single-owner knowledge and inbox; no new credential."""
from aiohttp import web
from solvio.security.mobile_approval import browser_sessions as B

OWNER = web.AppKey("solvio_dashboard_owner", str)


async def owner_browser(request, *, mutating=False):
    verified = await B.actor(request, mutating=mutating)
    owner = request.app.get(OWNER)
    return verified if owner and verified and verified.principal == owner else None
