import asyncio
import os
import logging
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
DELETE_FILES = os.environ.get("DELETE_FILES", "true").lower() == "true"

_rescan_task = None


async def _rescan_later(delay=30):
    await asyncio.sleep(delay)
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        url = f"{SONARR_URL}/api/v3/command"
        headers = {"X-Api-Key": SONARR_API_KEY}
        async with session.post(url, headers=headers, json={"name": "RescanSeries"}) as r:
            logger.info("Sonarr: RescanSeries (all) -> HTTP %s", r.status)


def schedule_rescan():
    """Дебаунс: каскад событий Episode/Season -> один rescan через 30 с тишины."""
    global _rescan_task
    if _rescan_task and not _rescan_task.done():
        _rescan_task.cancel()
    _rescan_task = asyncio.create_task(_rescan_later())


if not RADARR_URL or not RADARR_API_KEY:
    logger.warning("Radarr not configured — movie deletions will be skipped")
if not SONARR_URL or not SONARR_API_KEY:
    logger.warning("Sonarr not configured — series deletions will be skipped")


async def find_radarr_movie(session, tmdb_id):
    """Find movie in Radarr by TMDB id. Returns movie dict or None.

    Полный список библиотеки, а не /movie/lookup/tmdb: у Radarr lookup-ответ
    для фильма из библиотеки поля id НЕ содержит (проверено), а по id мы удаляем.
    При библиотеке в десятки-тысячи фильмов это всё ещё один быстрый запрос.
    """
    url = f"{RADARR_URL}/api/v3/movie"
    headers = {"X-Api-Key": RADARR_API_KEY}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            logger.error("Radarr list failed: HTTP %s", r.status)
            return None
        movies = await r.json()
    for m in movies:
        if str(m.get("tmdbId")) == str(tmdb_id):
            return m
    return None


async def delete_radarr_movie(session, movie):
    url = f"{RADARR_URL}/api/v3/movie/{movie['id']}"
    headers = {"X-Api-Key": RADARR_API_KEY}
    params = {"deleteFiles": str(DELETE_FILES).lower()}
    async with session.delete(url, headers=headers, params=params) as r:
        if r.status in (200, 202, 204):
            logger.info("Radarr: deleted movie «%s» (id=%s, deleteFiles=%s)",
                        movie.get("title"), movie.get("id"), DELETE_FILES)
            return True
        body = await r.text()
        logger.error("Radarr delete failed: HTTP %s | %s", r.status, body[:200])
        return False


async def find_sonarr_series(session, tvdb_id):
    """Find series in Sonarr by TVDB id. Returns series dict or None."""
    url = f"{SONARR_URL}/api/v3/series"
    headers = {"X-Api-Key": SONARR_API_KEY}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            logger.error("Sonarr list failed: HTTP %s", r.status)
            return None
        series = await r.json()
    for s in series:
        if str(s.get("tvdbId")) == str(tvdb_id):
            return s
    return None


async def delete_sonarr_series(session, series):
    url = f"{SONARR_URL}/api/v3/series/{series['id']}"
    headers = {"X-Api-Key": SONARR_API_KEY}
    params = {"deleteFiles": str(DELETE_FILES).lower()}
    async with session.delete(url, headers=headers, params=params) as r:
        if r.status in (200, 202, 204):
            logger.info("Sonarr: deleted series «%s» (id=%s, deleteFiles=%s)",
                        series.get("title"), series.get("id"), DELETE_FILES)
            return True
        body = await r.text()
        logger.error("Sonarr delete failed: HTTP %s | %s", r.status, body[:200])
        return False


async def handle_webhook(request):
    try:
        data = await request.json()
    except Exception as e:
        logger.error("Invalid JSON: %s", e)
        return web.json_response({"status": "bad_json"}, status=400)

    event = data.get("Event")
    item = data.get("Item") or {}
    item_type = item.get("Type")
    item_name = item.get("Name", "?")
    provider_ids = item.get("ProviderIds") or {}

    logger.info("Event=%s Type=%s Name=«%s»", event, item_type, item_name)

    if event != "ItemRemoved":
        return web.json_response({"status": "ignored_event"})

    if item_type == "Movie":
        tmdb_id = provider_ids.get("Tmdb")
        if not tmdb_id:
            logger.warning("Movie «%s» has no TMDB id — skipping", item_name)
            return web.json_response({"status": "no_tmdb_id"})

        if not RADARR_URL or not RADARR_API_KEY:
            logger.warning("Radarr not configured — skipping movie «%s»", item_name)
            return web.json_response({"status": "radarr_not_configured"})

        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            movie = await find_radarr_movie(session, tmdb_id)
            if not movie:
                logger.warning("Movie «%s» (tmdb=%s) not found in Radarr", item_name, tmdb_id)
                return web.json_response({"status": "not_in_radarr"})
            ok = await delete_radarr_movie(session, movie)
            return web.json_response({"status": "deleted" if ok else "delete_failed"})

    if item_type == "Series":
        tvdb_id = provider_ids.get("Tvdb")
        if not tvdb_id:
            logger.warning("Series «%s» has no TVDB id — skipping", item_name)
            return web.json_response({"status": "no_tvdb_id"})

        if not SONARR_URL or not SONARR_API_KEY:
            logger.warning("Sonarr not configured — skipping series «%s»", item_name)
            return web.json_response({"status": "sonarr_not_configured"})

        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            series = await find_sonarr_series(session, tvdb_id)
            if not series:
                logger.warning("Series «%s» (tvdb=%s) not found in Sonarr", item_name, tvdb_id)
                return web.json_response({"status": "not_in_sonarr"})
            ok = await delete_sonarr_series(session, series)
            return web.json_response({"status": "deleted" if ok else "delete_failed"})

    if item_type in ("Episode", "Season") and SONARR_URL and SONARR_API_KEY:
        logger.info("Type=%s «%s» removed — Sonarr rescan scheduled", item_type, item_name)
        schedule_rescan()
        return web.json_response({"status": "rescan_scheduled"})

    # Episode, Season, и всё остальное — игнорируем
    logger.info("Type=%s — ignored | item=%s", item_type, str(item)[:500])
    return web.json_response({"status": "ignored_type"})


async def main():
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9100)
    await site.start()

    logger.info("=" * 60)
    logger.info("  jelly-arr-reaper listening on :9100")
    logger.info("  Radarr: %s", RADARR_URL or "(not configured)")
    logger.info("  Sonarr: %s", SONARR_URL or "(not configured)")
    logger.info("  DELETE_FILES=%s", DELETE_FILES)
    logger.info("=" * 60)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
