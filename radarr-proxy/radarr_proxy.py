# /opt/docker-compose/configs/radarr-proxy/radarr_proxy.py
#
# Radarr-proxy с LLM-нормализацией поискового запроса.
# - Проксирует api.radarr.video / radarr.servarr.com, картинки TMDb — своим
#   путём tmdb_proxy.
# - Обогащает ответы TMDb (русские названия/описания/постеры).
# - При ENABLE_LLM=true спрашивает LLM оригинальные названия фильмов
#   по поисковому запросу и повторяет запрос к Radarr с найденными вариантами.

import asyncio, aiohttp, json, ssl, socket, os
from aiohttp import web
from datetime import datetime

# Общий код для sonarr/radarr-прокси (монтируется в /common, см. docker-compose.yaml)
import sys
sys.path.insert(0, "/common")
import arr_common

# --- КОНФИГУРАЦИЯ ---
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

HOST_MAP = {
    "radarr.servarr.com": "https://radarr.servarr.com",
    "api.radarr.video":   "https://api.radarr.video",
}

FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# DEBUG_LEVEL: 0 = только ошибки, 1 = компактно, 2 = полный дебаг
DEBUG_LEVEL = int(os.environ.get("DEBUG_LEVEL", "1"))

# --- LLM ---
ENABLE_LLM       = os.environ.get("ENABLE_LLM", "false").lower() == "true"
LLM_URL          = os.environ.get("LLM_URL", "http://freellmapi:3001/v1/chat/completions")
LLM_KEY          = os.environ.get("LLM_KEY", "")
LLM_TIMEOUT      = float(os.environ.get("LLM_TIMEOUT", "5.0"))
LLM_MAX_VARIANTS = int(os.environ.get("LLM_MAX_VARIANTS", "3"))

MAX_CONCURRENT = 20
TMDb_DELAY     = 0.1
ENRICH_LIMIT   = 10      # обогащаем только первые N результатов поиска
http_session = None

# не больше MAX_CONCURRENT одновременных обращений к TMDb + пауза между ними
throttle = arr_common.Throttle(MAX_CONCURRENT, TMDb_DELAY)

# --- КЭШ TMDB (общий модуль: запись атомарная) ---
CACHE_FILE = "/app/radarr_cache.json"
CACHE_DIR  = "/app/cached_images"

radarr_cache = arr_common.JsonCache(CACHE_FILE, kind="TMDb-кэш фильмов")

# --- КЭШ LLM (общий модуль, с TTL) ---
LLM_CACHE_FILE = "/app/llm_cache.json"
LLM_CACHE_TTL  = 30 * 24 * 3600
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
# LLM: нормализация поискового запроса (для фильмов)
# ---------------------------------------------------------------------------

LLM_PROMPT = """Ты — определитель названий фильмов.

Пользователь вводит поисковую строку для фильма. Она может быть на любом языке,
с опечатками, транслитом, неполной или описывать сюжет по смыслу.

Верни РОВНО ОДНО оригинальное название фильма на ЯЗЫКЕ ПРОИСХОЖДЕНИЯ
(на языке, на котором фильм был снят), НЕ перевод и НЕ локализованное название.

ВАЖНО: русское название фильма часто НЕ является дословным переводом
оригинала. Оно может быть придумано российскими локализаторами и не иметь
общих слов с оригинальным названием. Поэтому при поиске НЕ опирайся на
дословный перевод — вспоминай, какому известному фильму соответствует
такое русское название.

Формат вывода:
- Только название, одной строкой.
- Без кавычек. Без нумерации. Без пояснений. Без лишнего текста.
- НЕ описывай свои рассуждения.
- НЕ выводи несколько вариантов.
- Если не можешь уверенно определить фильм — выведи: —

Запрос: {term}"""


LLM_CFG = {
    "enable":        ENABLE_LLM,
    "url":           LLM_URL,
    "key":           LLM_KEY,
    "timeout":       LLM_TIMEOUT,
    "max_variants":  LLM_MAX_VARIANTS,
    "prompt":        LLM_PROMPT,
    "model":         "auto",
    "plus_to_space": True,   # Radarr кодирует пробелы в запросе как "+"
}


async def llm_resolve_term(term):
    """Спрашивает LLM оригинальные названия фильмов по поисковому запросу.

    При любой ошибке / таймауте / мусоре возвращает [].
    Успешные ответы (включая пустые) кэшируются на LLM_CACHE_TTL.
    Вызов, разбор ответа и кэш — в общем модуле (arr_common).
    """
    return await arr_common.llm_resolve_term(http_session, LLM_CFG, term, llm_cache)


# ---------------------------------------------------------------------------
# TMDB: получение русских данных
# ---------------------------------------------------------------------------

async def get_ru_data(tmdb_id):
    if not tmdb_id:
        return None

    if not arr_common.should_ask(radarr_cache, tmdb_id):
        log(f"  [CACHE] Найдено в текстовом кэше tmdbId={tmdb_id}", level=2)
        return radarr_cache.get(tmdb_id)

    url = (
        f"https://api.themoviedb.org/3/movie/{tmdb_id}"
        f"?api_key={TMDB_API_KEY}"
        f"&language=ru-RU"
        f"&append_to_response=images"
        f"&include_image_language=ru,en"
    )
    log(f"  ├─ [TMDB] GET {url}", level=2)

    try:
        # throttle: как у sonarr — не больше 10 обращений к TMDb сразу
        async with throttle, http_session.get(url, headers=FAKE_HEADERS,
                                             timeout=10, ssl=False) as r:
            body = await r.read()
            if r.status == 404:
                log_err(f"[TMDB] ID={tmdb_id} нет у TMDb (404) — запоминаем на 90 дней")
                arr_common.mark_no(radarr_cache, tmdb_id)
                return None
            if r.status != 200:
                log_err(f"[TMDB] ID={tmdb_id} HTTP {r.status} | "
                        f"{body[:300].decode('utf-8', errors='replace')}")
                # ошибку TMDb НЕ запоминаем — повторим при следующем запросе
                return None
            res = json.loads(body)

        title    = res.get('title', '—')
        overview = res.get('overview', '')

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
            log(f"  ├─ [TMDB] Постеров нет, fallback poster_path: {final_path}", level=2)

        data = {
            't': title,
            'o': overview,
            'p': final_path or None,
            '_why': chosen_why,
            '_vote': vote_str,
        }
        radarr_cache[tmdb_id] = data
        radarr_cache.save()
        return data

    except Exception as e:
        log_err(f"[TMDB-EXC] ID={tmdb_id}: {type(e).__name__}: {e or '(no message)'}")
    return None


# ---------------------------------------------------------------------------
# ОСНОВНОЙ ОБРАБОТЧИК
# ---------------------------------------------------------------------------

async def handle_request(request):
    host = request.headers.get('Host', 'api.radarr.video')

    # ── Прокси картинок TMDB (общий модуль) ──────────────────────────────
    if "tmdb_proxy" in request.path:
        parts = request.path.split("tmdb_proxy/")
        if len(parts) > 1:
            return await arr_common.serve_tmdb_image(
                request, http_session, CACHE_DIR, FAKE_HEADERS, parts[1].lstrip("/"))

    # ── Стандартный прокси ────────────────────────────────────────────────
    target_host = HOST_MAP.get(host)
    if not target_host:
        log_err(f"Неизвестный Host: {host}")
        return web.Response(status=404)

    # ── LLM-нормализация поиска ───────────────────────────────────────────
    # Поиск Radarr: GET /v1/search?q=<term>&year=&language=ru-RU на api.radarr.video
    is_radarr_search = (
        host == "api.radarr.video"
        and request.path.startswith("/v1/search")
        and request.method == "GET"
    )
    original_term = request.query.get('q', '') if is_radarr_search else ''

    log_sep(level=2)
    if is_radarr_search and original_term:
        log(f"\n[SEARCH] «{original_term}»", level=1)

    # Запрос к LLM идёт ПАРАЛЛЕЛЬНО с первым запросом к Radarr
    llm_task = None
    if is_radarr_search and ENABLE_LLM and original_term:
        llm_task = asyncio.create_task(llm_resolve_term(original_term))

    qs = dict(request.query)
    if any(x in request.path for x in ["/search", "/movie"]):
        qs['language'] = 'ru-RU'

    req_headers = dict(request.headers)
    req_headers['Host'] = target_host.replace("https://", "").replace("http://", "")
    req_headers.pop('Accept-Encoding', None)

    body = await request.read()

    async def _radarr_one(t, label):
        """Один запрос к Radarr с заданным q. Возвращает (kind, payload)."""
        qs_local = dict(qs)
        if t is not None:
            qs_local['q'] = t
        url = f"{target_host.rstrip('/')}/{request.path.lstrip('/')}"

        log(f"  GET {label}{url}  params={qs_local}", level=2)

        try:
            async with http_session.request(
                request.method, url,
                headers=req_headers, params=qs_local, data=body,
                ssl=False, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                content = await resp.read()
                if resp.status != 200 or "application/json" not in resp.content_type:
                    return ('http', (resp.status, resp.content_type, content))
                try:
                    data = json.loads(content)
                except Exception as e:
                    log_err(f"[JSON] {type(e).__name__}: {e}")
                    return ('http', (resp.status, resp.content_type, content))
                return ('ok', data)
        except asyncio.TimeoutError:
            log_err(f"Таймаут (60с) при ожидании ответа от Radarr: {url}")
            return ('exc', None)
        except Exception as e:
            log_err(f"[UPSTREAM-EXC] {type(e).__name__}: {e or '(no message)'}")
            return ('exc', None)

    # 1) Первый запрос (всегда)
    first_kind, first_payload = await _radarr_one(
        original_term if original_term else None, "")

    # результат LLM нужен только сейчас — забираем его
    extra_terms = []
    if llm_task is not None:
        try:
            extra_terms = await llm_task
        except Exception as e:
            log_err(f"[LLM] Непредвиденная ошибка: {type(e).__name__}: {e}")

    first_data = None
    first_items = []
    first_was_list = False
    first_failed = (first_kind == 'exc')

    if first_kind == 'ok':
        first_data = first_payload
        first_items = first_data if isinstance(first_data, list) else [first_data]
        first_was_list = isinstance(first_data, list)
    elif first_kind == 'http':
        # не-JSON или не-200 — если есть LLM-варианты, попробуем их, иначе отдаём как есть
        if not extra_terms:
            status, content_type, content = first_payload
            return web.Response(body=content, status=status,
                                headers={"Content-Type": content_type})
        log(f"[LLM] Первый запрос вернул не-JSON/не-200 — "
            f"пробуем варианты от LLM", level=1)

    # 2) Варианты LLM: берём ПЕРВЫЙ, который что-то нашёл. Его результаты встанут
    #    в начало списка, а результаты исходного запроса останутся ниже (дубли по
    #    tmdbId уберём) — так транслит/иное название не прячет исходную выдачу.
    llm_items = []
    used_llm_term = None

    if extra_terms:
        for i, variant in enumerate(extra_terms, start=1):
            kind, payload = await _radarr_one(variant, f"[LLM-var {i}] ")
            if kind != 'ok':
                log(f"  [LLM-var {i}] не удался — пробуем следующий", level=2)
                continue
            items = payload if isinstance(payload, list) else [payload]
            if items:
                llm_items = items
                used_llm_term = variant
                log(f"  [LLM-var {i}] «{variant}» → {len(items)} фильм(ов) "
                    f"(ставим в начало, ниже остаётся исходный запрос)", level=1)
                break
            else:
                log(f"  [LLM-var {i}] «{variant}» → 0 результатов", level=2)

        if used_llm_term is None and not first_failed:
            log(f"[LLM] Ни один вариант не дал результатов — "
                f"fallback на исходный запрос ({len(first_items)} фильм(ов))", level=1)

    # Если первый запрос упал и ни один LLM-вариант не дал результата — отдаём 504
    if first_failed and used_llm_term is None:
        return web.Response(status=504, text="Upstream Gateway Timeout (Proxy)")

    # Если первый запрос вернул не-JSON и LLM-варианты не помогли — отдаём как было
    if first_kind == 'http' and used_llm_term is None:
        status, content_type, content = first_payload
        return web.Response(body=content, status=status,
                            headers={"Content-Type": content_type})

    if extra_terms and used_llm_term:
        log(f"[LLM] Итог по «{original_term}»: сверху «{used_llm_term}» "
            f"({len(llm_items)}), ниже исходный запрос ({len(first_items)})", level=1)

    # вариант LLM — вперёд, исходный запрос — следом; дубли по tmdbId убираем
    merged, seen_ids = [], set()
    for it in (llm_items + first_items if llm_items else first_items):
        tid = (it.get('tmdbId') or it.get('TmdbId')) if isinstance(it, dict) else None
        if tid:
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
        merged.append(it)
    all_items = merged

    # ── TMDb-обогащение ─────────────────────────────────────────────────
    # Только реальные API-пути: раньше проверялись подстроки
    is_api_search = (host == "api.radarr.video") and \
                    request.path.startswith(("/v1/search", "/v1/movie"))

    if not (is_api_search and all_items):
        return web.json_response(
            all_items[0] if (len(all_items) == 1 and not first_was_list) else all_items,
            dumps=lambda obj: json.dumps(obj, ensure_ascii=False))

    q = qs.get('q', '?')
    log(f"[SEARCH] q={q!r}  найдено={len(all_items)}", level=1)

    async def process_item(item):
        if not isinstance(item, dict):
            return
        tid        = item.get('tmdbId') or item.get('TmdbId')
        title_orig = item.get('title') or item.get('Title') or '—'
        if not tid:
            return

        log(f"\n  ┌─ Обрабатываем: «{title_orig}» (tmdbId={tid})", level=2)

        ru = await get_ru_data(str(tid))
        if not ru or arr_common.is_no(ru):
            log(f"  └─ [SKIP] Нет данных от TMDB", level=2)
            return
        if not isinstance(ru, dict):
            log_err(f"[CACHE] tmdbId={tid}: запись не того типа "
                    f"({type(ru).__name__}) — пропуск")
            return

        if ru.get('t'):
            item['title'] = item['Title'] = ru['t']
        if ru.get('o'):
            item['overview'] = item['Overview'] = ru['o']

        poster_result = "—"
        if ru.get('p'):
            new_proxy_url = f"https://{host}/tmdb_proxy/t/p/original/{ru['p']}"
            # подстановка постера — в общем модуле (тип картинки без учёта регистра,
            # у radarr пишем только существующие ключи, как было)
            replaced_count, old_url = arr_common.set_poster(
                item, new_proxy_url, fill_all_keys=False, create_if_missing=False)

            if replaced_count == 0:
                poster_result = f"[{ru.get('_why')}] vote={ru.get('_vote')} (new)"
                log(f"  ├─ [POSTER-NEW] {new_proxy_url}", level=2)
            else:
                poster_result = f"[{ru.get('_why')}] vote={ru.get('_vote')} (x{replaced_count})"
                log(f"  ├─ [POSTER] Было : {old_url}", level=2)
                log(f"  ├─ [POSTER] Стало: {new_proxy_url}", level=2)
        else:
            log(f"  ├─ [POSTER-SKIP] TMDB не вернул путь к постеру", level=2)
            poster_result = "no-path"

        log(f"  └─ Готово: «{item.get('title', '?')}»", level=2)
        log(f"  «{title_orig}» → «{ru.get('t')}»  poster={poster_result}", level=1)

    for res in await asyncio.gather(*(process_item(i) for i in all_items[:ENRICH_LIMIT]),
                                    return_exceptions=True):
        if isinstance(res, BaseException):
            log_err(f"[RADARR] обработка элемента упала: "
                    f"{type(res).__name__}: {res or '(no message)'}")
    log_sep("═", level=2)

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
    conn = aiohttp.TCPConnector(ssl=False, limit=100, family=socket.AF_INET)
    http_session = aiohttp.ClientSession(connector=conn)

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle_request)
    runner = web.AppRunner(app)
    await runner.setup()

    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain("cert.pem", "key.pem")
    await web.TCPSite(runner, '0.0.0.0', 443, ssl_context=ssl_ctx).start()

    print("=" * 72, flush=True)
    print(f"  RADARR-PROXY READY  |  DEBUG_LEVEL={DEBUG_LEVEL}"
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

