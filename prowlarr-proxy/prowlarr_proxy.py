import os
import re
import logging
import xml.etree.ElementTree as ET

from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Config ---
PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9696"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

SKIP_CATEGORIES = os.environ.get("SKIP_CATEGORIES", "5070,5030,5040").split(",")
SKIP_CATEGORIES = [c.strip() for c in SKIP_CATEGORIES if c.strip()]

# Одна сессия на весь процесс: раньше ClientSession создавалась на каждый запрос
# (новое TCP-соединение и никакого keep-alive). На замерах это миллисекунды, но
# правильнее держать соединение как в двух других прокси.
http_session = None

if not PROWLARR_API_KEY:
    raise ValueError("PROWLARR_API_KEY is required")


def log(msg, level=1):
    if level == 0:
        logger.info(msg)
    elif DEBUG:
        logger.debug(msg)


# --- XML processing ---
def extract_titles(xml_bytes: bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log(f"[XML] parse error: {e}", level=0)
        return None, None

    titles = []
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is not None and title_el.text:
            titles.append((title_el, title_el.text))
    return root, titles


# Маркер сезона/эпизода: S06E01, S6E1-3, 05x01, 1x01-04
SEASON_EPISODE_RE = re.compile(
    r'(?<![A-Za-z0-9])(?:S\d{1,3}E\d{1,3}(?:[-\u2013]\d{1,3})?|\d{1,2}x\d{1,3}(?:[-\u2013]\d{1,3})?)',
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """
    Заменяет круглые скобки на слеши только в «голове» строки — до первого
    маркера сезона/эпизода (S06E01, 1x01 и т.п.).
    Всё, что после маркера — не трогает (там идут MVO, качество, год и пр.)
    """
    m = SEASON_EPISODE_RE.search(title)
    if not m:
        # нет маркера — заменяем только в начале до первого не-названия
        # для безопасности вообще не трогаем
        return title

    head = title[:m.start()]
    tail = title[m.start():]

    # Заменяем скобки в голове
    head = re.sub(r'\s*\(([^)]+)\)', r'/\1', head)
    head = re.sub(r'\s*/\s*', '/', head)
    head = re.sub(r'/+', '/', head)
    head = re.sub(r'^/', '', head)
    head = head.rstrip()

    # Добавляем пробел перед маркером сезона, если он слипся
    if head and not head.endswith((' ', '/')):
        head += ' '

    return head + tail


def replace_titles(root, mapping: dict) -> bytes:
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is not None and title_el.text in mapping:
            new_title = mapping[title_el.text]
            if new_title != title_el.text:
                title_el.text = new_title
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# --- Proxy handler ---
async def handle_request(request: web.Request) -> web.Response:
    path_qs = request.path_qs
    target_url = f"{PROWLARR_URL}{path_qs}"

    log(f"[REQ] {request.method} {path_qs}", level=0)

    headers = dict(request.headers)
    headers["X-Api-Key"] = PROWLARR_API_KEY
    headers.pop("Host", None)
    headers.pop("Content-Length", None)

    body = await request.read() if request.method in ("POST", "PUT", "PATCH") else None

    try:
        async with http_session.request(
            request.method, target_url, headers=headers, data=body,
        ) as resp:
            content = await resp.read()
            content_type = resp.content_type
            status = resp.status
    except Exception as e:
        log(f"[UPSTREAM] error: {type(e).__name__}: {e}", level=0)
        return web.Response(status=502, text="Prowlarr unreachable")

    is_newznab = any(
        f"t={t}" in path_qs
        for t in ("search", "tvsearch", "movie", "music", "book")
    )

    if not is_newznab or status != 200 or "xml" not in content_type.lower():
        return web.Response(body=content, status=status, content_type=content_type)

    has_q = "q=" in path_qs and not re.search(r'[?&]q=(&|$)', path_qs)
    if not has_q:
        log("[SKIP] no q in query", level=0)
        return web.Response(body=content, status=status, content_type=content_type)

    cat_match = re.search(r'[?&]cat=([^&]+)', path_qs)
    if cat_match:
        cats = [c.strip() for c in cat_match.group(1).split(",")]
        if any(c in SKIP_CATEGORIES for c in cats):
            log(f"[SKIP] categories {cats} in skip-list", level=0)
            return web.Response(body=content, status=status, content_type=content_type)

    root, titles = extract_titles(content)
    if root is None or not titles:
        return web.Response(body=content, status=status, content_type=content_type)

    mapping = {}
    for el, text in titles:
        if any("\u0400" <= c <= "\u04FF" for c in text):
            normalized = normalize_title(text)
            if normalized != text:
                mapping[text] = normalized
                log(f"  «{text[:110]}»", level=0)
                log(f"    → «{normalized[:110]}»", level=0)

    if not mapping:
        return web.Response(body=content, status=status, content_type=content_type)

    new_xml = replace_titles(root, mapping)
    return web.Response(body=new_xml, status=200, content_type="application/xml")


async def main():
    global http_session
    http_session = ClientSession(timeout=ClientTimeout(total=60))

    app = web.Application(client_max_size=20 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handle_request)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", LISTEN_PORT)
    await site.start()

    logger.info("=" * 60)
    logger.info("  prowlarr-proxy listening on :%d", LISTEN_PORT)
    logger.info("  Prowlarr:   %s", PROWLARR_URL)
    logger.info("  Skip cats:  %s", ",".join(SKIP_CATEGORIES))
    logger.info("  DEBUG: %s", DEBUG)
    logger.info("=" * 60)

    import asyncio
    await asyncio.Event().wait()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
