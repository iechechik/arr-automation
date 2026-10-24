# /opt/docker-compose/configs/sonarr-proxy/sonarr_proxy.py
#
# Sonarr-proxy с LLM-нормализацией поискового запроса.
# - Проксирует Skyhook.
# - Обогащает ответы TMDb (русские названия/описания/постеры).
# - При ENABLE_LLM=true спрашивает LLM оригинальные названия сериалов
#   по поисковому запросу и повторяет запрос к Skyhook с найденными вариантами.
#
# Поддержка Sonarr v4 (пути /v1/tvdb/, /v3/, /series/lookup) и
# Sonarr v5 (пути /v2/tvdb/, /vN/tvdb/...) — см. is_api ниже.

import asyncio, aiohttp, json, re, ssl, socket, os
from aiohttp import web
from datetime import datetime

# Общий код для sonarr/radarr-прокси (монтируется в /common, см. docker-compose.yaml)
import sys
sys.path.insert(0, "/common")
import arr_common

# --- КОНФИГУРАЦИЯ ---
TMDB_API_KEY = "ab35147ab4e8b094369e401c2c68b51f"
TARGET_URL   = "https://skyhook.sonarr.tv"
CERT_FILE, KEY_FILE = "cert.pem", "key.pem"

FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# DEBUG_LEVEL:
#   0 = только ошибки и старт
#   1 = одна строка на сериал (что нашли / перевели)
#   2 = полный дебаг (URL, кэш, детали)
DEBUG_LEVEL = int(os.environ.get("DEBUG_LEVEL", "1"))

# --- LLM ---
ENABLE_LLM       = os.environ.get("ENABLE_LLM", "false").lower() == "true"
LLM_URL          = os.environ.get("LLM_URL", "http://freellmapi:3001/v1/chat/completions")
LLM_KEY          = os.environ.get("LLM_KEY", "")
LLM_TIMEOUT      = float(os.environ.get("LLM_TIMEOUT", "5.0"))
LLM_MAX_VARIANTS = int(os.environ.get("LLM_MAX_VARIANTS", "3"))

# Хост sonarr-proxy — через него Sonarr запрашивает картинки.
PROXY_HOST = "skyhook.sonarr.tv"

MAX_CONCURRENT = 20
TMDb_DELAY     = 0.1
ENRICH_LIMIT   = 10      # обогащаем только первые N результатов поиска
http_session   = None

# не больше MAX_CONCURRENT одновременных обращений к TMDb + пауза между ними
throttle = arr_common.Throttle(MAX_CONCURRENT, TMDb_DELAY)

CACHE_FILE   = "/app/tmdb_cache.json"
CACHE_DIR    = "/app/cached_images"

# ---------------------------------------------------------------------------
# КЭШ (общий модуль: запись атомарная)
# ---------------------------------------------------------------------------

tmdb_cache = arr_common.JsonCache(CACHE_FILE, kind="TMDb-кэш")

# ---------------------------------------------------------------------------
# КЭШ LLM: term -> варианты ответа (общий модуль, с TTL)
# ---------------------------------------------------------------------------

LLM_CACHE_FILE = "/app/llm_cache.json"
LLM_CACHE_TTL  = 30 * 24 * 3600  # 30 дней в секундах
llm_cache = arr_common.JsonCache(LLM_CACHE_FILE, kind="LLM-кэш", ttl=LLM_CACHE_TTL)


# ---------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# ---------------------------------------------------------------------------

def now():
    return datetime.now().strftime('%y.%m.%d %H:%M:%S')

def log(msg, level=1):
    if DEBUG_LEVEL >= level:
        print(f"[{now()}] {msg}", flush=True)

def log_err(msg):
    print(f"[{now()}] [ERR] {msg}", flush=True)

def log_sep(char="─", width=72, level=2):
    if DEBUG_LEVEL >= level:
        print(char * width, flush=True)

# общий модуль пишет в наши логи
arr_common.configure(log, log_err)

# ---------------------------------------------------------------------------
# LLM: нормализация поискового запроса
# ---------------------------------------------------------------------------

LLM_PROMPT = """Ты — определитель названий сериалов.

Пользователь вводит поисковую строку для сериала. Она может быть на любом языке,
с опечатками, транслитом, неполной или описывать сюжет по смыслу.

Верни РОВНО ОДНО оригинальное название сериала на ЯЗЫКЕ ПРОИСХОЖДЕНИЯ
(на языке, на котором сериал был снят), НЕ перевод и НЕ локализованное название.

ВАЖНО: русское название сериала часто НЕ является дословным переводом
оригинала. Оно может быть придумано российскими локализаторами и не иметь
общих слов с оригинальным названием. Поэтому при поиске НЕ опирайся на
дословный перевод — вспоминай, какому известному сериалу соответствует
такое русское название.

Формат вывода:
- Только название, одной строкой.
- Без кавычек. Без нумерации. Без пояснений. Без лишнего текста.
- НЕ описывай свои рассуждения.
- НЕ выводи несколько вариантов.
- Если не можешь уверенно определить сериал — выведи: —

Запрос: {term}"""

LLM_CFG = {
    "enable":        ENABLE_LLM,
    "url":           LLM_URL,
    "key":           LLM_KEY,
    "timeout":       LLM_TIMEOUT,
    "max_variants":  LLM_MAX_VARIANTS,
    "prompt":        LLM_PROMPT,
    "model":         "auto",
    "plus_to_space": False,
}


async def llm_resolve_term(term):
    """Спрашивает LLM оригинальные названия сериалов по поисковому запросу.

    При любой ошибке / таймауте / мусоре возвращает [].
    Успешные ответы (включая пустые) кэшируются на LLM_CACHE_TTL.
    Вызов, разбор ответа и кэш — в общем модуле (arr_common).
    """
    return await arr_common.llm_resolve_term(http_session, LLM_CFG, term, llm_cache)

# ---------------------------------------------------------------------------
# TMDB: получение русских данных для сериала по tvdbId
# ---------------------------------------------------------------------------

async def get_ru_data(tvdb_id, original_title=""):
    if not arr_common.should_ask(tmdb_cache, tvdb_id):
        log(f"  [CACHE] tvdbId={tvdb_id} «{original_title}»", level=2)
        return tmdb_cache.get(tvdb_id)

    async with throttle:

        find_url = (
            f"https://api.themoviedb.org/3/find/{tvdb_id}"
            f"?api_key={TMDB_API_KEY}"
            f"&external_source=tvdb_id"
        )
        log(f"  ├─ [TMDB] FIND tvdbId={tvdb_id} «{original_title}»", level=2)

        try:
            async with http_session.get(find_url, headers=FAKE_HEADERS,
                                        timeout=10, ssl=False) as r:
                if r.status != 200:
                    body = await r.read()
                    log_err(f"[TMDB/find] tvdbId={tvdb_id} HTTP {r.status} | "
                            f"{body[:200].decode('utf-8', errors='replace')}")
                    # ошибку TMDb НЕ запоминаем: иначе сериал навсегда остаётся
                    # без русского названия и постера (кэшируем только «не знает»)
                    return None
                find_res = await r.json()
        except Exception as e:
            log_err(f"[TMDB/find-EXC] tvdbId={tvdb_id}: {type(e).__name__}: {e or '(no message)'}")
            return None

        tv_results = find_res.get('tv_results', [])
        if not tv_results:
            log(f"  ├─ [TMDB] tvdbId={tvdb_id} — не найден в TMDB", level=2)
            # «честное нет» помним 90 дней: TMDb пополняется, потом переспросим
            arr_common.mark_no(tmdb_cache, tvdb_id)
            return None

        tmdb_id = tv_results[0].get('id')
        log(f"  ├─ [TMDB] tvdbId={tvdb_id} → tmdbId={tmdb_id}", level=2)

        detail_url = (
            f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            f"?api_key={TMDB_API_KEY}"
            f"&language=ru-RU"
            f"&append_to_response=images"
            f"&include_image_language=ru,en"
        )
        log(f"  ├─ [TMDB] GET {detail_url}", level=2)

        try:
            async with http_session.get(detail_url, headers=FAKE_HEADERS,
                                        timeout=10, ssl=False) as r:
                if r.status != 200:
                    body = await r.read()
                    log_err(f"[TMDB/tv] tmdbId={tmdb_id} HTTP {r.status} | "
                            f"{body[:200].decode('utf-8', errors='replace')}")
                    # ошибку TMDb НЕ запоминаем (см. выше)
                    return None
                res = await r.json()
        except Exception as e:
            log_err(f"[TMDB/tv-EXC] tmdbId={tmdb_id}: {type(e).__name__}: {e or '(no message)'}")
            return None

    if not isinstance(res, dict):
        log_err(f"[TMDB] tmdbId={tmdb_id}: неожиданный ответ "
                f"{type(res).__name__} — пропуск")
        return None

    title    = res.get('name', '')
    overview = res.get('overview', '')

    log(f"  ├─ [TMDB] tmdbId={tmdb_id} «{title}»", level=2)

    # выбор постера (ru -> без языка -> en) — в общем модуле
    poster = arr_common.pick_poster(res)

    if poster:
        final_path = poster['path']
        chosen_why = poster['why']
        vote       = poster['vote']
        vote_str   = f"{vote:.2f}({poster['vote_count']})"
    else:
        raw_path   = res.get('poster_path') or ''
        final_path = raw_path.lstrip('/') if raw_path else None
        chosen_why = "нет"
        vote_str   = "—"
        log(f"  ├─ [TMDB] Постеров нет, fallback: {final_path}", level=2)

    translated_title = title if (
        title and title.strip().lower() != original_title.strip().lower()
    ) else None

    data = {
        't': translated_title,
        'o': overview or None,
        'p': final_path or None,
        '_why':  chosen_why,
        '_vote': vote_str,
    }
    tmdb_cache[tvdb_id] = data
    tmdb_cache.save()
    return data

# ---------------------------------------------------------------------------
# SKYHOOK: запрос
# ---------------------------------------------------------------------------

async def fetch_skyhook(method, url, headers, body):
    """
    Запрос к Skyhook.
    Возвращает (status, content_type, content_bytes) или None при ошибке.
    """
    try:
        async with http_session.request(
            method, url, headers=headers, data=body,
            ssl=False, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            content = await resp.read()
            return resp.status, resp.content_type, content
    except asyncio.TimeoutError:
        log_err(f"Таймаут (60с) при ожидании ответа от апстрима Skyhook: {url}")
        return None
    except Exception as e:
        log_err(f"[UPSTREAM-EXC] {type(e).__name__}: {e or '(no message)'}")
        return None

# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНОЕ
# ---------------------------------------------------------------------------

def dedup_by_tvdb(items):
    """Убирает дубли по tvdbId (побеждает первое вхождение).

    Элементы, которые не являются объектами, отбрасываются: апстрим иногда
    подмешивает к массиву сериалов постороннее, а Sonarr ждёт только объекты.
    """
    seen = set()
    result = []
    for it in items:
        if not isinstance(it, dict):
            log_err(f"[DEDUP] элемент не объект ({type(it).__name__}) — отброшен")
            continue
        tid = it.get('tvdbId')
        if tid is None:
            result.append(it)
            continue
        key = str(tid)
        if key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result

# ---------------------------------------------------------------------------
# ОСНОВНОЙ ОБРАБОТЧИК
# ---------------------------------------------------------------------------

async def handle_request(request):
    path = request.path

    # ── Прокси картинок TMDB (общий модуль) ──────────────────────────────
    if "tmdb_proxy" in path:
        parts = path.split("tmdb_proxy/")
        if len(parts) > 1:
            return await arr_common.serve_tmdb_image(
                request, http_session, CACHE_DIR, FAKE_HEADERS, parts[1].lstrip("/"))

    # ── Проксирование к Skyhook ───────────────────────────────────────────
    is_lookup = ("/series/lookup" in path) or ("/tvdb/search/" in path)
    is_search = "search" in path.lower() or is_lookup

    original_term = request.query.get('term', '') if is_lookup else ''

    log_sep(level=2)
    if is_search and original_term:
        log(f"\n[SEARCH] «{original_term}»", level=1)
    else:
        log(f"[REQ] {request.method} {path}", level=2)

    # ── LLM-нормализация поискового запроса ──────────────────────────────
    # Запрос к LLM идёт ПАРАЛЛЕЛЬНО с первым запросом к Skyhook: раньше поиск
    # сначала ждал модель (до LLM_TIMEOUT) и только потом шёл в апстрим.
    llm_task = None
    if is_lookup and ENABLE_LLM and original_term:
        llm_task = asyncio.create_task(llm_resolve_term(original_term))

    # ── Запросы к Skyhook ────────────────────────────────────────────────
    req_headers = dict(request.headers)
    req_headers['Host'] = "skyhook.sonarr.tv"
    req_headers.pop('Accept-Encoding', None)

    body = await request.read()

    # Вспомогательная функция — один запрос к Skyhook с заданным term.
    # Возвращает (kind, payload) где kind: 'ok' | 'http' | 'exc'
    async def _skyhook_one(t, label):
        if t is None:
            url = f"{TARGET_URL}{request.path_qs}"
        else:
            from urllib.parse import urlencode
            qs = dict(request.query)
            qs['term'] = t
            url = f"{TARGET_URL}{request.path}?{urlencode(qs)}"

        log(f"  GET {label}{url}", level=2)

        res = await fetch_skyhook(request.method, url, req_headers, body)
        if res is None:
            return ('exc', None)
        status, content_type, content = res
        if status != 200 or "application/json" not in content_type:
            return ('http', (status, content_type, content))
        try:
            data = json.loads(content)
        except Exception as e:
            log_err(f"[JSON] {type(e).__name__}: {e}")
            return ('http', (status, content_type, content))
        return ('ok', data)

    # 1) Исходный запрос к Skyhook (всегда)
    first_kind, first_payload = await _skyhook_one(
        original_term if original_term else None, "")

    # результат LLM нужен только сейчас — забираем его
    extra_terms = []
    if llm_task is not None:
        try:
            extra_terms = await llm_task
        except Exception as e:
            log_err(f"[LLM] Непредвиденная ошибка: {type(e).__name__}: {e}")

    first_data     = None
    first_items    = []
    first_was_list = False
    first_failed   = (first_kind == 'exc')

    if first_kind == 'ok':
        first_data     = first_payload
        first_items    = first_data if isinstance(first_data, list) else [first_data]
        first_was_list = isinstance(first_data, list)
        log(f"  [UPSTREAM] Skyhook вернул {len(first_items)} сериал(ов)", level=2)
    elif first_kind == 'http':
        if not extra_terms:
            status, content_type, content = first_payload
            return web.Response(body=content, status=status,
                                headers={"Content-Type": content_type})
        log(f"[LLM] Первый запрос вернул не-JSON/не-200 — пробуем варианты от LLM", level=1)

    # 2) Варианты LLM: берём ПЕРВЫЙ, который что-то нашёл. Его результаты встанут
    #    в начало списка, а результаты исходного запроса останутся ниже (дубли по id
    #    уберём) — так транслит/иное название не прячет исходную выдачу целиком.
    llm_items = []
    used_llm_term = None

    if extra_terms:
        for i, variant in enumerate(extra_terms, start=1):
            kind, payload = await _skyhook_one(variant, f"[LLM-var {i}] ")
            if kind != 'ok':
                log(f"  [LLM-var {i}] не удался — пробуем следующий", level=2)
                continue
            items = payload if isinstance(payload, list) else [payload]
            if items:
                llm_items = items
                used_llm_term = variant
                log(f"  [LLM-var {i}] «{variant}» → {len(items)} сериал(ов) "
                    f"(ставим в начало, ниже остаётся исходный запрос)", level=1)
                break
            else:
                log(f"  [LLM-var {i}] «{variant}» → 0 результатов", level=2)

        if used_llm_term is None and not first_failed:
            log(f"[LLM] Ни один вариант не дал результатов — "
                f"fallback на исходный запрос ({len(first_items)} сериал(ов))", level=1)

    if first_failed and used_llm_term is None:
        return web.Response(status=504, text="Upstream Gateway Timeout (Proxy)")

    if first_kind == 'http' and used_llm_term is None:
        status, content_type, content = first_payload
        return web.Response(body=content, status=status,
                            headers={"Content-Type": content_type})

    # вариант LLM — вперёд, исходный запрос — следом; дубли по tvdbId убираем
    all_items = dedup_by_tvdb(llm_items + first_items if llm_items
                              else first_items)

    if extra_terms and used_llm_term:
        log(f"[LLM] Итог по «{original_term}»: сверху «{used_llm_term}» "
            f"({len(llm_items)}), ниже исходный запрос ({len(first_items)})", level=1)

    # ── TMDb-обогащение ──────────────────────────────────────────────────
    # Универсально для Sonarr v4 и v5:
    #   v4: /v1/tvdb/..., /v3/..., /series/lookup
    #   v5: /v2/tvdb/..., а на будущее — любой /vN/tvdb/
    # Раньше проверялись только v1/v3, из-за чего запросы v5 (/v2/tvdb/search/)
    # попадали в [SKIP-ENRICH] и метаданные не переводились на русский.
    is_api = bool(re.match(r"^/v\d+/tvdb/", path)) or path.startswith(
        ("/v1/", "/v2/", "/v3/", "/series/lookup", "/tvdb/search"))

    if not is_api:
        log(f"  [SKIP-ENRICH] не API-путь: {path}", level=2)
        return web.json_response(
            all_items[0] if (len(all_items) == 1 and not first_was_list) else all_items,
            dumps=lambda obj: json.dumps(obj, ensure_ascii=False))

    tasks = [
        get_ru_data(str(item['tvdbId']), item.get('title', ''))
        for item in all_items[:ENRICH_LIMIT]
        if item.get('tvdbId') and arr_common.should_ask(tmdb_cache, str(item['tvdbId']))
    ]
    if tasks:
        log(f"  [API] Новых запросов к TMDB: {len(tasks)} "
            f"(кэш: {len(tmdb_cache)})", level=2)
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, BaseException):
                log_err(f"[TMDB] обогащение упало: "
                        f"{type(res).__name__}: {res or '(no message)'}")

    translated = []
    skipped    = []

    for item in all_items[:ENRICH_LIMIT]:
        if not isinstance(item, dict):
            continue
        tid        = str(item.get('tvdbId', ''))
        orig_title = item.get('title', '?')

        if arr_common.is_no(tmdb_cache.get(tid)):
            skipped.append(orig_title)
            continue

        ru = tmdb_cache[tid]
        if not isinstance(ru, dict):
            log_err(f"[CACHE] tvdbId={tid}: запись не того типа "
                    f"({type(ru).__name__}) — пропуск")
            skipped.append(orig_title)
            continue

        if ru.get('t'):
            item['title'] = ru['t']
            if 'alternateTitles' in item:
                if not any(t.get('title') == ru['t']
                           for t in item['alternateTitles']):
                    item['alternateTitles'].append(
                        {'title': ru['t'], 'source': 'TMDB_Proxy'}
                    )

        if ru.get('o'):
            item['overview'] = ru['o']

        poster_res = "—"
        if ru.get('p'):
            new_url = f"https://{PROXY_HOST}/tmdb_proxy/t/p/original/{ru['p']}"
            # подстановка постера — в общем модуле: замена на месте,
            # тип картинки сравнивается без учёта регистра
            replaced_count, old_url = arr_common.set_poster(
                item, new_url, fill_all_keys=True, create_if_missing=True)

            if replaced_count == 0:
                poster_res = f"[{ru.get('_why')}] vote={ru.get('_vote')} (new)"
                log(f"  ├─ [POSTER-NEW] {new_url}", level=2)
            else:
                poster_res = f"[{ru.get('_why')}] vote={ru.get('_vote')} (x{replaced_count})"
                log(f"  ├─ [POSTER] Было : {old_url}", level=2)
                log(f"  ├─ [POSTER] Стало: {new_url}", level=2)

        translated.append(
            f"«{orig_title}» → «{item.get('title', orig_title)}»  "
            f"poster={poster_res}"
        )

    if translated:
        log(f"  [OK] Переведено {len(translated)}/"
            f"{min(len(all_items), ENRICH_LIMIT)}:", level=1)
        for t in translated:
            log(f"       {t}", level=1)
    if skipped:
        log(f"  [--] Без перевода ({len(skipped)}): " +
            ", ".join(f"«{s}»" for s in skipped), level=1)

    log_sep("═", level=2)

    # Возвращаем в том же формате, что пришёл: список — список, объект — объект
    if len(all_items) == 1 and not first_was_list:
        return web.json_response(all_items[0],
                                 dumps=lambda obj: json.dumps(obj, ensure_ascii=False))
    return web.json_response(all_items,
                             dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

async def main():
    global http_session
    conn         = aiohttp.TCPConnector(ssl=False, limit=100, family=socket.AF_INET)
    http_session = aiohttp.ClientSession(connector=conn)

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle_request)

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(CERT_FILE, KEY_FILE)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 443, ssl_context=ssl_ctx).start()

    print("=" * 72, flush=True)
    print(f"  SONARR-PROXY READY  |  DEBUG_LEVEL={DEBUG_LEVEL}"
          f"  ({'errors only' if DEBUG_LEVEL==0 else 'compact' if DEBUG_LEVEL==1 else 'verbose'})",
          flush=True)
    print(f"  LLM: {'ENABLED' if ENABLE_LLM else 'disabled'}"
          f"{' | ' + LLM_URL if ENABLE_LLM else ''}"
          f"{' | timeout=' + str(LLM_TIMEOUT) + 's' if ENABLE_LLM else ''}"
          f"{' | max_variants=' + str(LLM_MAX_VARIANTS) if ENABLE_LLM else ''}"
          f"{' | cache=' + str(len(llm_cache)) + ' terms (TTL ' + str(LLM_CACHE_TTL // 86400) + 'd)' if ENABLE_LLM else ''}",
          flush=True)
    print("=" * 72, flush=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
