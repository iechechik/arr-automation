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

# Sonarr: нужен только для сезонного поиска — узнать, какие серии уже есть.
# Адрес и ключ приходят из окружения контейнера (compose). Нет ключа —
# сезонное сужение просто выключается, прокси работает как раньше.
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
SONARR_CACHE_TTL = int(os.environ.get("SONARR_CACHE_TTL", "120"))

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

# NEW: диапазон S{season}E{a}-{b} (of N) — для схлопывания под запрошенный ep
RANGE_FOR_EP_RE = re.compile(
    r'(?P<prefix>S)(?P<season>\d{1,3})E(?P<a>\d{1,3})[-\u2013](?P<b>\d{1,3})'
    r'(?:\s+of\s+\d{1,3})?',
    re.IGNORECASE,
)


def normalize_marker(title: str) -> str:
    """Приводит маркеры сезона/эпизода к SNE-формату."""
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

    def repl_br(m):
        s = m.group(1)
        rest = m.group(2) or ''
        rest = re.sub(r'^E0*(\d+)', lambda x: f'E{int(x.group(1))}', rest)
        rest = re.sub(r'-0*(\d+)', lambda x: f'-{int(x.group(1))}', rest)
        rest = re.sub(r'\s+of\s+0*(\d+)', lambda x: f' of {int(x.group(1))}', rest)
        return f'S{int(s)}{rest}'

    title = re.sub(
        r'\[\s*S0*(\d+)(E\d+(?:-\d+)?(?:\s+of\s+\d+)?)\s*\]',
        repl_br, title, flags=re.IGNORECASE,
    )

    title = SEASON_ONLY_BRACKET_RE.sub(lambda m: f'S{int(m.group(1))}', title)

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
    head = re.sub(r'([\u0400-\u04FF])\s+([A-Za-z])', r'\1 / \2', head)
    head = re.sub(r'\(\s*([^)]*[\u0400-\u04FF][^)]*)\s*\)', r' / \1 / ', head)
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
    if not LAT_RE.search(title):
        return title

    m = SEASON_EPISODE_INLINE_RE.search(title)
    if not m:
        m2 = re.search(r'\[\s*S\d{1,3}\s*\]', title)
        if m2:
            head = title[:m2.start()].strip()
            tail = title[m2.start():].strip()
        else:
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
    result = re.sub(r'([A-Za-z])\s*(?:[-–—]\s*)?(S\d{1,3}E\d{1,3})', r'\1 / \2', result)
    result = re.sub(r'\s+-\s+(\d{4})(?=\s|$)', r' [\1]', result)

    return re.sub(r'\s+', ' ', result).strip()


# NEW: схлопывание диапазона в одиночный эпизод под запрос
def collapse_range_for_ep(title: str, season: int, ep: int):
    """
    Если заголовок содержит S{season}E{a}-{b} (of N) и a <= ep <= b,
    заменяем на S{season}E{ep} (без "of N").
    Возвращает (новый_заголовок, количество_эпизодов_в_паке) или (title, 0) если не менялось.
    """
    class State:
        count = 0
    state = State()

    def repl_with_state(m):
        s = int(m.group('season'))
        a = int(m.group('a'))
        b = int(m.group('b'))
        if s == season and a <= ep <= b:
            state.count = b - a + 1
            return f'S{s}E{ep}'
        return m.group(0)

    new_title = RANGE_FOR_EP_RE.sub(repl_with_state, title)
    return new_title, state.count


def replace_titles_and_sizes(root, mapping: dict, size_divisors: dict) -> bytes:
    """
    Заменяет заголовки и, если нужно, делит размер во всех местах,
    где Sonarr может его прочитать (size, enclosure, torznab:attr).
    size_divisors: {старый_title: divisor}
    """
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is not None and title_el.text in mapping:
            old_title = title_el.text
            new_title = mapping[old_title]
            if new_title != old_title:
                title_el.text = new_title

                divisor = size_divisors.get(old_title, 0)
                if divisor > 1:
                    # 1) <size>
                    size_el = item.find("size")
                    if size_el is not None and size_el.text:
                        try:
                            old_size = int(size_el.text)
                            new_size = old_size // divisor
                            size_el.text = str(new_size)
                            log(f"  [SIZE] size {old_size} -> {new_size}", level=0)
                        except ValueError:
                            pass

                    # 2) <enclosure length="...">
                    enc = item.find("enclosure")
                    if enc is not None:
                        old_len = enc.get("length")
                        if old_len:
                            try:
                                new_len = int(old_len) // divisor
                                enc.set("length", str(new_len))
                                log(f"  [SIZE] enclosure {old_len} -> {new_len}", level=0)
                            except ValueError:
                                pass

                    # 3) <torznab:attr name="size" value="...">
                    for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
                        if attr.get("name") == "size":
                            old_val = attr.get("value")
                            if old_val:
                                try:
                                    new_val = int(old_val) // divisor
                                    attr.set("value", str(new_val))
                                    log(f"  [SIZE] torznab:size {old_val} -> {new_val}", level=0)
                                except ValueError:
                                    pass
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ======================================================================
# Сезонный поиск: пакет сужается до серий, которых нет в библиотеке
# ======================================================================
import copy
import time
import hashlib
from datetime import datetime, timezone
from difflib import SequenceMatcher

_series_cache = {"ts": 0.0, "items": []}
_eps_cache = {}

EP_RANGE_RE = re.compile(
    r'(?<![A-Za-z0-9])S(?P<s>\d{1,3})E(?P<a>\d{1,3})[-\u2013](?P<b>\d{1,3})'
    r'(?:\s+of\s+\d{1,3})?', re.IGNORECASE)
EP_MULTI_RE = re.compile(
    r'(?<![A-Za-z0-9])S(?P<s>\d{1,3})(?P<eps>(?:E\d{1,3}){2,})'
    r'(?:\s+of\s+\d{1,3})?', re.IGNORECASE)
EP_SINGLE_RE = re.compile(
    r'(?<![A-Za-z0-9])S(?P<s>\d{1,3})E(?P<a>\d{1,3})'
    r'(?:\s+of\s+\d{1,3})?', re.IGNORECASE)
EP_SEASON_ONLY_RE = re.compile(r'(?<![A-Za-z0-9])S(?P<s>\d{1,3})(?![\dEe])', re.IGNORECASE)


async def sonarr_get(path, params=None):
    if not SONARR_API_KEY:
        return None
    url = f"{SONARR_URL}/api/v3/{path}"
    try:
        async with http_session.get(
            url, params=params,
            headers={"X-Api-Key": SONARR_API_KEY},
            timeout=ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log(f"[SONARR] {path}: HTTP {resp.status}", level=0)
                return None
            return await resp.json()
    except Exception as e:
        log(f"[SONARR] {path}: {type(e).__name__}: {e}", level=0)
        return None


async def sonarr_series():
    now = time.monotonic()
    if _series_cache["items"] and now - _series_cache["ts"] < SONARR_CACHE_TTL:
        return _series_cache["items"]
    items = await sonarr_get("series")
    if items:
        _series_cache["ts"] = now
        _series_cache["items"] = items
    return _series_cache["items"]


async def sonarr_episodes(series_id):
    now = time.monotonic()
    cached = _eps_cache.get(series_id)
    if cached and now - cached[0] < SONARR_CACHE_TTL:
        return cached[1]
    items = await sonarr_get("episode", {"seriesId": series_id})
    if items is not None:
        _eps_cache[series_id] = (now, items)
        return items
    return cached[1] if cached else None


def clean_name(value):
    value = (value or "").lower().replace("\u0451", "\u0435")
    value = re.sub(r"\(\s*\d{4}\s*\)", " ", value)
    value = re.sub(r"\[\s*\d{4}\s*\]", " ", value)
    value = re.sub(r"[^0-9a-z\u0430-\u044f]+", " ", value)
    return " ".join(value.split())


def series_names(series):
    names = [series.get("title"), series.get("originalTitle")]
    for alias in series.get("alternateTitles") or []:
        names.append(alias.get("title"))
    return [clean_name(n) for n in names if clean_name(n)]


def match_series(query, series_list):
    """Ищем сериал в Sonarr по строке q из запроса (fuzzy, порог 0.85)."""
    q = clean_name(query)
    if not q:
        return None
    best_score, best = 0.0, None
    for series in series_list:
        for name in series_names(series):
            if name == q:
                return series
            score = SequenceMatcher(None, name, q).ratio()
            if q.startswith(name) or name.startswith(q):
                score = max(score, 0.9)
            if score > best_score:
                best_score, best = score, series
    return best if best_score >= 0.85 else None


def item_matches_series(title, series):
    """Заголовок раздачи относится к этому сериалу? Сверяем куски до маркера."""
    span = episode_span(title, None)
    head = title[:span[0][0]] if span else title
    parts = [clean_name(p) for p in re.split(r"/", head)]
    parts = [p for p in parts if p]
    parts.append(clean_name(head))
    known = series_names(series)
    for part in parts:
        for name in known:
            if part == name:
                return True
            if SequenceMatcher(None, part, name).ratio() >= 0.9:
                return True
    return False


def aired(episode):
    value = episode.get("airDateUtc") or ""
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt <= datetime.now(timezone.utc)


def missing_in_season(episodes, season):
    # только отслеживаемые: иначе в заголовок попадёт серия, которую заказчик
    # сознательно не мониторит, и её скачают «за компанию»
    return {e["episodeNumber"] for e in episodes
            if e.get("seasonNumber") == season and aired(e)
            and e.get("monitored") and not e.get("hasFile")}


def episode_span(title, season):
    """(span, kind, номера) для маркера сезона/серий; season=None — любой сезон."""
    m = EP_RANGE_RE.search(title)
    if m and (season is None or int(m.group("s")) == season):
        a, b = int(m.group("a")), int(m.group("b"))
        if a <= b:
            return m.span(), "range", list(range(a, b + 1))
    m = EP_MULTI_RE.search(title)
    if m and (season is None or int(m.group("s")) == season):
        nums = [int(x) for x in re.findall(r"\d+", m.group("eps"))]
        if nums:
            # У Sonarr дырки дозаполняются (Range(first,last)), поэтому формат
            # «E2E5» он поймёт как 2,3,4,5. Такой item не трогаем: переписывать
            # его — значит врать про количество серий.
            if nums != list(range(min(nums), max(nums) + 1)):
                return None
            return m.span(), "multi", nums
    m = EP_SINGLE_RE.search(title)
    if m and (season is None or int(m.group("s")) == season):
        return m.span(), "single", [int(m.group("a"))]
    return None


def runs(numbers):
    out = []
    for n in numbers:
        if out and n == out[-1][-1] + 1:
            out[-1].append(n)
        else:
            out.append([n])
    return out


def run_marker(season, run):
    return "S%d%s" % (season, "".join("E%d" % e for e in run))


def scale_sizes(item, factor):
    if factor == 1.0:
        return
    size_el = item.find("size")
    if size_el is not None and size_el.text:
        try:
            size_el.text = str(int(int(size_el.text) * factor))
        except ValueError:
            pass
    enc = item.find("enclosure")
    if enc is not None and enc.get("length"):
        try:
            enc.set("length", str(int(int(enc.get("length")) * factor)))
        except ValueError:
            pass
    for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
        if attr.get("name") == "size" and attr.get("value"):
            try:
                attr.set("value", str(int(int(attr.get("value")) * factor)))
            except ValueError:
                pass


def set_guid(item, base_guid, index):
    guid_el = item.find("guid")
    if guid_el is None:
        guid_el = ET.SubElement(item, "guid")
    guid_el.text = hashlib.md5(("%s_%d" % (base_guid, index)).encode()).hexdigest()
    guid_el.set("isPermaLink", "false")


def plan_item(title, season, need, series):
    """
    None  — не трогаем (нет маркера, другой сериал/сезон, или пакет нужен целиком)
    []    — выбросить (нужных серий в пакете нет)
    [ (заголовок, коэффициент_размера), ... ] — заменить на эти item'ы
    """
    base = title
    if CYR_RE.search(title):
        try:
            base = normalize_title(title)
        except Exception:
            base = title
    if not item_matches_series(base, series):
        return None
    span = episode_span(base, season)
    if not span:
        if EP_SEASON_ONLY_RE.search(base):
            log(f"[SEASON] «сезон целиком», не трогаем: {base[:90]}", level=0)
        return None
    (start, end), kind, pack = span
    wanted = sorted(set(pack) & need)
    if not wanted:
        return []
    if len(wanted) == len(pack):
        return None
    # ВАЖНО: ровно ОДИН item на пак. Sonarr в одном поиске берёт все одобренные
    # решения, у которых эпизоды не пересекаются (ProcessDownloadDecisions
    # пропускает только пересекающиеся), — два отрезка одного пака дали бы два
    # добавления одного торрента. Поэтому берём самый длинный отрезок, остальные
    # доберём следующим поиском.
    segments = sorted(runs(wanted), key=len, reverse=True)
    run = segments[0]
    if len(segments) > 1:
        log("[SEASON] отрезков %d, берём %s, откладываем %s"
            % (len(segments), run_marker(season, run),
               ", ".join(run_marker(season, r) for r in segments[1:])), level=0)
    # доля размера — по длине ИМЕННО этого отрезка
    factor = len(run) / float(len(pack))
    new_title = base[:start] + run_marker(season, run) + base[end:]
    return [(new_title, factor)]


def rewrite_items(root, season, need, series):
    """Переписывает item'ы паков. None — если ничего менять не пришлось."""
    channel = root.find("channel")
    if channel is None:
        return None
    old_items = list(channel.findall("item"))
    new_items = []
    changed = False
    for item in old_items:
        title_el = item.find("title")
        title = (title_el.text or "") if title_el is not None else ""
        plan = plan_item(title, season, need, series)
        if plan is None:
            new_items.append(item)
            continue
        changed = True
        if not plan:
            log(f"[SEASON] выбрасываем «{title[:90]}» — нужных серий нет", level=0)
            continue
        guid_el = item.find("guid")
        base_guid = (guid_el.text or title) if guid_el is not None else title
        for idx, (new_title, factor) in enumerate(plan):
            it = copy.deepcopy(item)
            it.find("title").text = new_title
            scale_sizes(it, factor)
            set_guid(it, base_guid, idx)
            log(f"[SEASON] «{title[:70]}» -> «{new_title[:70]}» x{factor:.3f}", level=0)
            new_items.append(it)
    if not changed:
        return None
    for it in old_items:
        channel.remove(it)
    for it in new_items:
        channel.append(it)
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

    # NEW: парсим season/ep из query для схлопывания диапазонов
    season_q = re.search(r'[?&]season=(\d+)', path_qs)
    ep_q = re.search(r'[?&]ep=(\d+)', path_qs)
    query_season = int(season_q.group(1)) if season_q else None
    query_ep = int(ep_q.group(1)) if ep_q else None

    root, titles = extract_titles(content)
    if root is None or not titles:
        return web.Response(body=content, status=status, content_type=content_type)

    # Сезонный поиск (season есть, ep нет): вместо пака целиком отдаём
    # только те серии, которых нет в библиотеке Sonarr.
    if query_season is not None and query_ep is None:
        try:
            query_title = request.query.get("q", "")
            series = match_series(query_title, await sonarr_series()) if query_title else None
            if series:
                episodes = await sonarr_episodes(series["id"])
                if episodes is not None:
                    need = missing_in_season(episodes, query_season)
                    if need:
                        new_xml = rewrite_items(root, query_season, need, series)
                        if new_xml:
                            log(f"[SEASON] «{series.get('title')}» S{query_season}: "
                                f"нет серий {sorted(need)}", level=0)
                            return web.Response(body=new_xml, status=200,
                                                content_type="application/xml")
                    else:
                        log(f"[SEASON] «{series.get('title')}» S{query_season}: "
                            f"недостающих серий нет", level=0)
            else:
                log(f"[SEASON] сериал по запросу «{query_title[:60]}» не найден", level=0)
        except Exception as e:
            log(f"[SEASON] error: {type(e).__name__}: {e}", level=0)

    mapping = {}
    size_divisors = {}  # {старый_title: divisor}
    for el, text in titles:
        if CYR_RE.search(text):
            try:
                normalized = normalize_title(text)
            except Exception as e:
                log(f"[NORM] error on «{text[:80]}»: {type(e).__name__}: {e}", level=0)
                continue

            # NEW: схлопывание диапазона под запрошенный эпизод
            if query_season is not None and query_ep is not None:
                try:
                    collapsed, count = collapse_range_for_ep(normalized, query_season, query_ep)
                    if count > 1:
                        normalized = collapsed
                        size_divisors[text] = count
                except Exception as e:
                    log(f"[COLLAPSE] error on «{normalized[:80]}»: {e}", level=0)

            if normalized != text:
                mapping[text] = normalized
                log(f"  «{text[:120]}»", level=0)
                log(f"    → «{normalized[:120]}»", level=0)

    if not mapping:
        return web.Response(body=content, status=status, content_type=content_type)

    new_xml = replace_titles_and_sizes(root, mapping, size_divisors)
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
