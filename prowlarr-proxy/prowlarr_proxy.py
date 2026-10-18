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


CYR_RE = re.compile(r'[\u0400-\u04FF]')
LAT_RE = re.compile(r'[A-Za-z]')

# Маркер сезона/эпизода внутри строки (не в скобках) — для поиска начала хвоста
SEASON_EPISODE_INLINE_RE = re.compile(
    r'(?<![A-Za-z0-9])'
    r'(?:S\d{1,3}E\d{1,3}(?:[-\u2013]\d{1,3})?(?:\s+of\s+\d{1,3})?'
    r'|\d{1,2}x\d{1,3}(?:[-\u2013]\d{1,3})?)',
    re.IGNORECASE,
)

# Сезон без эпизода: [S05], S5
SEASON_ONLY_BRACKET_RE = re.compile(r'\[\s*S(\d{1,3})\s*\]', re.IGNORECASE)

# "(сезон 6, серии 1-4 из 6)" и подобное
SEASON_EPISODE_RU_RE = re.compile(
    r'\(\s*[СC]езон\s+(\d+)(?:\s*,\s*[СC]ери[ияй]\s+(\d+)(?:-(\d+))?\s+из\s+(\d+))?\s*\)',
    re.IGNORECASE,
)


def normalize_marker(title: str) -> str:
    """Приводит маркеры сезона/эпизода к SNE-формату."""
    # (сезон 6, серии 1-4 из 6) -> S6E1-4 of 6
    def repl_ru(m):
        s = m.group(1)
        e1 = m.group(2)
        e2 = m.group(3)
        total = m.group(4)
        if e1 and e2:
            return f'S{int(s)}E{int(e1)}-{int(e2)} of {total}'
        if e1:
            return f'S{int(s)}E{int(e1)} of {total}'
        return f'S{int(s)}'
    title = SEASON_EPISODE_RU_RE.sub(repl_ru, title)

    # [S06E01-04 of 06] -> S6E1-4 of 6
    def repl_br(m):
        s = m.group(1)
        rest = m.group(2) or ''
        # нормализуем E01-04 of 06
        rest = re.sub(r'^E0*(\d+)', lambda x: f'E{int(x.group(1))}', rest)
        rest = re.sub(r'-0*(\d+)', lambda x: f'-{int(x.group(1))}', rest)
        rest = re.sub(r'\s+of\s+0*(\d+)', lambda x: f' of {int(x.group(1))}', rest)
        return f'S{int(s)}{rest}'

    title = re.sub(
        r'\[\s*S0*(\d+)(E\d+(?:-\d+)?(?:\s+of\s+\d+)?)\s*\]',
        repl_br, title, flags=re.IGNORECASE,
    )

    # [S05] -> S5
    title = SEASON_ONLY_BRACKET_RE.sub(lambda m: f'S{int(m.group(1))}', title)

    # S06E01-04 of 06 (без скобок) -> S6E1-4 of 6
    def repl_inline(m):
        t = m.group(0)
        t = re.sub(r'^[Ss]0*(\d+)', lambda x: f'S{int(x.group(1))}', t)
        t = re.sub(r'[Ee]0*(\d+)', lambda x: f'E{int(x.group(1))}', t)
        t = re.sub(r'[-–]0*(\d+)', lambda x: f'-{int(x.group(1))}', t)
        t = re.sub(r'\s+of\s+0*(\d+)', lambda x: f' of {int(x.group(1))}', t)
        return t
    title = SEASON_EPISODE_INLINE_RE.sub(repl_inline, title)

    return title


def split_names(head: str):
    """
    Разбирает голову (до маркера) на перевод / оригинал.
    Возвращает (translation, original).
    - кириллица без скобок → перевод
    - кириллица в скобках     → альтернатива (игнор)
    - латиница                → оригинал
    """
    # Вставить / между кириллицей и латиницей, если их разделяет только пробел
    # "Медленные лошади Slow Horses" -> "Медленные лошади / Slow Horses"
    head = re.sub(r'([\u0400-\u04FF])\s+([A-Za-z])', r'\1 / \2', head)
    # Круглые скобки с кириллицей → заменить на слэш-разделитель
    head = re.sub(r'\(\s*([^)]*[\u0400-\u04FF][^)]*)\s*\)', r' / \1 / ', head)
    # Одиночные слэши нормализуем
    head = re.sub(r'\s*/\s*', ' / ', head)
    parts = [p.strip() for p in head.split(' / ') if p.strip()]

    translation = None
    original = None
    for p in parts:
        if LAT_RE.search(p):
            if original is None:
                original = p
        elif CYR_RE.search(p):
            if translation is None:
                translation = p

    return translation, original


def normalize_title(title: str) -> str:
    # 1. Если латиницы нет — не трогаем (оригинал на русском, типа "Оно (2017)")
    if not LAT_RE.search(title):
        return title

    # 2. Найти маркер сезона/эпизода
    m = SEASON_EPISODE_INLINE_RE.search(title)
    if not m:
        # Фильм или сезон без эпизода: [S05] или просто ничего
        m2 = re.search(r'\[\s*S\d{1,3}\s*\]', title)
        if m2:
            head = title[:m2.start()].strip()
            tail = title[m2.start():].strip()
        else:
            head = re.sub(r'[\s\-–—|]+$', '', head)   # убрать " -" перед маркером
            # Чистый фильм: переводим всю строку
            translation, original = split_names(title)
            if translation and original:
                return f'{translation} / {original}'
            return title
    else:
        head = title[:m.start()].strip()
        tail = title[m.start():].strip()

    translation, original = split_names(head)

    if translation and original:
        head_out = f'{translation} / {original}'
    elif translation:
        head_out = translation
    elif original:
        head_out = original
    else:
        head_out = head

    result = f'{head_out} {tail}' if head_out else tail
    result = normalize_marker(result)
    # Вставить " / " между латинским оригиналом и маркером SxxExx
    # "Slow Horses S6E1-4 of 6" -> "Slow Horses / S6E1-4 of 6"
    result = re.sub(r'([A-Za-z])\s*(?:[-–—]\s*)?(S\d{1,3}E\d{1,3})', r'\1 / \2', result)
    
    # Kinozal: " - 2026 MVO" -> " [2026] MVO" (иначе Sonarr ломается на дефисе перед годом)
    result = re.sub(r'\s+-\s+(\d{4})(?=\s|$)', r' [\1]', result)
    
    return re.sub(r'\s+', ' ', result).strip()


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
        if CYR_RE.search(text):
            normalized = normalize_title(text)
            if normalized != text:
                mapping[text] = normalized
                log(f"  «{text[:120]}»", level=0)
                log(f"    → «{normalized[:120]}»", level=0)

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
