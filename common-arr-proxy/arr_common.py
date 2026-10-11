# /opt/docker-compose/configs/common-arr-proxy/arr_common.py
#
# Общий код для sonarr-proxy и radarr-proxy.
# Здесь живёт ТОЛЬКО то, что обязано совпадать у обоих сервисов:
#   - выбор постера TMDb (ru -> без языка -> en) и подстановка его в картинки;
#   - прокси картинок TMDb с локальным кэшем;
#   - JSON-кэши (атомарная запись);
#   - вызов LLM и разбор её ответа.
# Специфика сервиса (адреса, откуда берём id, формат ответа) остаётся
# в sonarr_proxy.py / radarr_proxy.py.
#
# Подключается монтированием (см. docker-compose.yaml):
#   volumes: - /opt/docker-compose/configs/common-arr-proxy:/common:ro
#   import sys; sys.path.insert(0, "/common"); import arr_common

import asyncio
import json
import os
import re
from datetime import datetime

from aiohttp import web

# ---------------------------------------------------------------------------
# Логирование: сервис отдаёт свои log/log_err (у них свой DEBUG_LEVEL)
# ---------------------------------------------------------------------------

_log_fn = None
_log_err_fn = None


def configure(log_fn, log_err_fn):
    """Подключает логирование сервиса. До вызова сообщения идут в stdout."""
    global _log_fn, _log_err_fn
    _log_fn, _log_err_fn = log_fn, log_err_fn


def log(msg, level=1):
    if _log_fn is not None:
        _log_fn(msg, level)
    else:
        print(f"[{datetime.now().strftime('%y.%m.%d %H:%M:%S')}] {msg}", flush=True)


def log_err(msg):
    if _log_err_fn is not None:
        _log_err_fn(msg)
    else:
        print(f"[{datetime.now().strftime('%y.%m.%d %H:%M:%S')}] [ERR] {msg}", flush=True)


# ---------------------------------------------------------------------------
# JSON-кэш в файле
# ---------------------------------------------------------------------------

class JsonCache(dict):
    """Словарь, который сам читается из файла и сохраняется атомарно.

    Атомарность (tmp + os.replace) нужна, чтобы падение или рестарт посреди
    записи не оставил обрезанный файл — раньше кэш перезаписывался на месте,
    и при сбое терялся весь русский кэш.

    ttl — если задан, при загрузке выбрасываются записи, у которых поле "ts"
    старше ttl секунд (для LLM-кэша). Записи без "ts" при заданном ttl тоже
    выбрасываются.
    """

    def __init__(self, path, kind="кэш", ttl=None):
        super().__init__()
        self.path = path
        self.kind = kind
        self.ttl = ttl
        self.load()

    def load(self):
        self.clear()
        if not os.path.exists(self.path):
            print(f"[{datetime.now().strftime('%y.%m.%d %H:%M:%S')}] "
                  f"[INIT] {self.kind}: создан новый пустой ({self.path})", flush=True)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log_err(f"[INIT] {self.kind} не прочитан ({self.path}): "
                    f"{type(e).__name__}: {e}")
            return

        dropped = 0
        now_ts = int(datetime.now().timestamp())
        for key, entry in (raw.items() if isinstance(raw, dict) else []):
            if self.ttl is not None:
                try:
                    ts = int((entry or {}).get("ts", 0))
                except Exception:
                    dropped += 1
                    continue
                if now_ts - ts > self.ttl:
                    dropped += 1
                    continue
            self[key] = entry

        log(f"[INIT] {self.kind}: загружено {len(self)} записей"
            f"{f', выброшено устаревших {dropped}' if dropped else ''}"
            f" ({self.path})", level=0)

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            log_err(f"{self.kind} не сохранён ({self.path}): "
                    f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# «TMDb не знает» — сколько это помнить
# ---------------------------------------------------------------------------

NEG_TTL = 90 * 24 * 3600   # «честное нет» держим 90 дней, потом переспрашиваем


def _now_ts():
    return int(datetime.now().timestamp())


def should_ask(cache, key):
    """Надо ли спрашивать TMDb: записи нет, запись испорчена или «не знает» просрочено."""
    entry = cache.get(key)
    if entry is None:                      # нет записи (в т.ч. старый формат null)
        return True
    if isinstance(entry, dict) and "_no" in entry:
        try:
            ts = int(entry.get("_no") or 0)
        except Exception:
            return True
        return (_now_ts() - ts) > NEG_TTL
    return False


def mark_no(cache, key):
    """Запоминает «TMDb не знает» со временем, чтобы потом можно было переспросить."""
    cache[key] = {"_no": _now_ts()}
    cache.save()


def is_no(entry):
    """Запись означает «данных нет»: нет записи, старый null или наше «не знает»."""
    return entry is None or (isinstance(entry, dict) and "_no" in entry)


# ---------------------------------------------------------------------------
# Постер TMDb: выбор
# ---------------------------------------------------------------------------

def _num(value):
    """Число из ответа TMDb. Он отдаёт vote_* числами, но полагаться на это
    нельзя: строка или None сломают форматирование vote в логе и в _vote."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pick_poster(payload):
    """Выбирает постер из ответа TMDb: сначала ru, потом без языка, потом en.

    Внутри группы побеждает максимальный vote_average (как было в обоих прокси).
    Возвращает словарь {path, why, vote, vote_count} или None.
    """
    posters = ((payload or {}).get("images") or {}).get("posters") or []

    ru = [p for p in posters if p.get("iso_639_1") == "ru"]
    no_lang = [p for p in posters if p.get("iso_639_1") in (None, "xx", "")]
    en = [p for p in posters if p.get("iso_639_1") == "en"]

    log(f"  ├─ [TMDB] постеров: ru={len(ru)} null={len(no_lang)} en={len(en)}", level=2)

    for candidates, why in ((ru, "ru"), (no_lang, "null/xx"), (en, "en")):
        if not candidates:
            continue
        best = max(candidates, key=lambda p: p.get("vote_average", 0) or 0)
        path = (best.get("file_path") or "").lstrip("/")
        if not path:
            continue
        info = {
            "path": path,
            "why": why,
            "vote": _num(best.get("vote_average")),
            "vote_count": int(_num(best.get("vote_count"))),
        }
        log(f"  ├─ [TMDB] Выбран постер [{why}] "
            f"vote={info['vote']:.2f}({info['vote_count']}) path=/{path}", level=2)
        return info

    return None


# ---------------------------------------------------------------------------
# Постер: подстановка в картинки ответа
# ---------------------------------------------------------------------------

def set_poster(item, new_url, fill_all_keys, create_if_missing):
    """Подставляет URL постера в массив картинок элемента ответа.

    Два правила, из-за которых этот код вынесен в общий модуль:

    1. Тип картинки сравнивается БЕЗ учёта регистра: апстримы отдают
       "Poster"/"CoverType" с большой буквы, а регистрозависимое сравнение
       с 'poster' не срабатывало — постер не заменялся на месте и добавлялся
       вторым, и Sonarr показывал оригинал.

    2. Если постера в ответе нет, свой добавляется В КОНЕЦ массива. Sonarr и
       Radarr качают все картинки с типом poster в один и тот же файл
       (MediaCover/<id>/poster.jpg) в порядке массива — побеждает ПОСЛЕДНЯЯ,
       поэтому постер в начале массива затирается оригиналом.

    fill_all_keys     — True: писать и url, и remoteUrl (так делал sonarr);
                        False: писать только уже существующие ключи (radarr).
    create_if_missing — создавать ли массив картинок, если его нет вовсе.

    Возвращает (сколько заменено, старый_url_или_None).
    """
    key = None
    for k in ("images", "Images"):
        if isinstance(item.get(k), list):
            key = k
            break

    if key is None:
        if not create_if_missing:
            return 0, None
        key = "images"
        item[key] = []

    imgs = item[key]
    pascal = (key == "Images")
    replaced = 0
    old_url = None

    for img in imgs:
        if not isinstance(img, dict):
            continue
        ctype = str(img.get("coverType") or img.get("CoverType") or "").lower()
        if ctype != "poster":
            continue

        if old_url is None:
            old_url = (img.get("remoteUrl") or img.get("RemoteUrl")
                       or img.get("url") or img.get("Url"))

        if fill_all_keys:
            img["url"] = new_url
            img["remoteUrl"] = new_url
        else:
            for k in ("url", "remoteUrl", "Url", "RemoteUrl"):
                if k in img:
                    img[k] = new_url
        replaced += 1

    if replaced == 0:
        if pascal:
            imgs.append({"CoverType": "poster", "Url": new_url, "RemoteUrl": new_url})
        else:
            imgs.append({"coverType": "poster", "url": new_url, "remoteUrl": new_url})

    return replaced, old_url


# ---------------------------------------------------------------------------
# Прокси картинок TMDb
# ---------------------------------------------------------------------------

async def serve_tmdb_image(request, session, cache_dir, fake_headers, img_path):
    """Отдаёт картинку TMDb: из локального кэша, иначе скачивает и кэширует.

    img_path — путь после "tmdb_proxy/" (например "t/p/original/abc.jpg").
    """
    target_url = f"https://image.tmdb.org/{img_path}"
    local_file_path = os.path.join(cache_dir, img_path.replace("/", "_"))
    os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

    if request.method == "HEAD":
        if os.path.exists(local_file_path):
            log(f"[IMG-HEAD-LOCAL] {img_path} (Найдено на диске)", level=2)
            return web.Response(status=200)
        log(f"[IMG-HEAD] {target_url}", level=2)
        try:
            async with session.head(target_url, headers=fake_headers,
                                    timeout=10, ssl=False) as r:
                return web.Response(status=r.status)
        except Exception as e:
            log_err(f"[IMG-HEAD] {img_path}: {type(e).__name__}: {e or '(no message)'}")
            return web.Response(status=502)

    if os.path.exists(local_file_path):
        log(f"[IMG-GET-LOCAL] {img_path} (Отдано из локального кэша)", level=2)
        with open(local_file_path, "rb") as f:
            content = f.read()
        return web.Response(body=content, status=200, content_type="image/jpeg")

    log(f"[IMG] {target_url} (Загрузка с TMDB...)", level=2)
    try:
        async with session.get(target_url, headers=fake_headers,
                               timeout=20, ssl=False) as r:
            if r.status != 200:
                log_err(f"[IMG] HTTP {r.status} for {img_path}")
                return web.Response(status=r.status)
            content = await r.read()
            # Пишем через .part: обрыв или перезапуск в этот момент иначе оставит
            # в кэше обрезанный jpeg, который потом отдаётся как есть (файл есть —
            # перекачивать не будем), и постер в Sonarr/Radarr остаётся битым.
            tmp_path = local_file_path + ".part"
            with open(tmp_path, "wb") as f:
                f.write(content)
            os.replace(tmp_path, local_file_path)
            return web.Response(body=content, status=200,
                                content_type=r.content_type)
    except Exception as e:
        log_err(f"[IMG] {img_path}: {type(e).__name__}: {e or '(no message)'}")
        return web.Response(status=502)


# ---------------------------------------------------------------------------
# LLM: нормализация поискового запроса
# ---------------------------------------------------------------------------

# Фразы-маркеры рассуждений: строка, начинающаяся с них, — не название.
REASONING_PREFIXES = (
    "we need to", "we have", "we should", "we can",
    "the user", "the query", "user query", "user wants",
    "it's russian", "it is russian", "it's a ", "it is a ",
    "let me", "let's ", "i need to", "i think", "i'm not",
    "i am not", "i don't", "i do not",
    "could be", "maybe ", "possibly", "perhaps",
    "not sure", "unsure", "apparently",
    "answer:", "result:", "output:",
)

# Заглушки «ничего не найдено» — это не названия, их нельзя отправлять в апстрим.
PLACEHOLDERS = {
    "nothing", "none", "n/a", "na", "null", "nil", "unknown",
    "no result", "no results", "no match", "no matches",
    "not found", "not applicable", "undefined", "empty",
    "ничего", "нет", "не найдено", "не найдена", "неизвестно",
    "-", "—", "–", "?", "??", "...",
}

# Утечка промпта в ответ
PROMPT_LEAK_MARKERS = ("query:", "rules:", "titles:", "prompt:", "strict rules:")

# Признаки рефлексии внутри строки
REFLECTION_MARKERS = ("not sure", "i'm not aware", "i don't know",
                      "could be a ", "maybe they meant", "possibly they mean")


def _looks_like_garbage(s):
    if len(s) > 120:
        return True
    low = s.lower().strip()
    if low in PLACEHOLDERS:
        return True
    low = s.lower()
    if any(k in low for k in PROMPT_LEAK_MARKERS):
        return True
    if any(low.startswith(pref) for pref in REASONING_PREFIXES):
        return True
    if any(k in low for k in REFLECTION_MARKERS):
        return True
    if len(s.split()) > 12:
        return True
    return False


def parse_llm_variants(raw, original_term, max_variants):
    """Разбирает ответ LLM в список вариантов названий.

    Сначала пробуем JSON (массив/объект/строка), если не вышло — построчно.
    Отбрасываем рассуждения, заглушки и обрубки исходного запроса.
    """
    if not raw:
        return []

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    candidates = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            candidates = [str(x) for x in parsed]
        elif isinstance(parsed, dict):
            for key in ("titles", "title", "variants", "results"):
                if key in parsed:
                    v = parsed[key]
                    candidates = [str(x) for x in v] if isinstance(v, list) else [str(v)]
                    break
        elif isinstance(parsed, str):
            candidates = [parsed]
    except Exception:
        candidates = text.splitlines()

    cleaned = []
    for c in candidates:
        s = c.strip()
        s = re.sub(r"^\s*(?:\d+[\.\)]|[-*•])\s*", "", s)
        s = s.strip().strip('"').strip("'").strip("«»").strip()
        if not s:
            continue
        if _looks_like_garbage(s):
            continue
        cleaned.append(s)

    # Отсев обрубков исходного запроса
    orig_nospace = re.sub(r"\s+", "", original_term.strip().lower())
    seen = set()
    filtered = []
    for s in cleaned:
        low = s.lower()
        if low == original_term.strip().lower():
            continue
        if low in seen:
            continue
        s_nospace = re.sub(r"\s+", "", low)
        if s_nospace and orig_nospace:
            if s_nospace == orig_nospace:
                continue
            if len(s_nospace) >= 4 and (orig_nospace.startswith(s_nospace)
                                        or orig_nospace.endswith(s_nospace)):
                continue
        seen.add(low)
        filtered.append(s)

    # Сначала латиница (апстримы её знают), потом остальное
    latin = [s for s in filtered if any("a" <= ch.lower() <= "z" for ch in s)]
    nonlatin = [s for s in filtered if not any("a" <= ch.lower() <= "z" for ch in s)]
    result = (latin + nonlatin)[:max_variants]

    if len(filtered) > 1:
        log(f"[LLM-SORT] было={filtered} стало={result}", level=2)

    return result


def is_meaningful_term(term):
    """Мусорные короткие запросы (1 символ, только пунктуация) в LLM не отправляем."""
    if not term:
        return False
    s = term.strip()
    if len(s) < 2:
        return False
    if not any(ch.isalpha() or ch.isdigit() for ch in s):
        return False
    return True


async def llm_resolve_term(session, cfg, term, cache):
    """Спрашивает LLM оригинальные названия по поисковому запросу.

    cfg: enable, url, key, timeout, max_variants, prompt, model, plus_to_space.
    cache: JsonCache(ttl=...) — удачные ответы (в том числе пустые) кэшируются.
    При любой ошибке/таймауте/мусоре возвращает [].
    """
    if not cfg.get("enable") or not term:
        return []

    if cfg.get("plus_to_space"):
        term = term.replace("+", " ").strip()

    if not is_meaningful_term(term):
        log(f"[LLM] «{term}» — мусорный/короткий запрос, пропуск", level=2)
        return []

    cached = cache.get(term)
    if cached is not None:
        variants = cached.get("v", [])
        log(f"[LLM-CACHE] «{term}» → {variants} (из локального кэша)", level=1)
        return variants

    payload = {
        "model": cfg.get("model", "auto"),
        "messages": [{"role": "user", "content": cfg["prompt"].format(term=term)}],
        "temperature": 0.2,
        "stop": ["\n\n"],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('key', '')}",
    }

    try:
        async with session.post(
            cfg["url"], json=payload, headers=headers,
            timeout=cfg.get("timeout", 5.0),
        ) as r:
            if r.status != 200:
                body = await r.read()
                log_err(f"[LLM] HTTP {r.status} | "
                        f"{body[:200].decode('utf-8', errors='replace')}")
                return []
            data = await r.json()
    except Exception as e:
        log_err(f"[LLM] {type(e).__name__}: {e or '(no message)'}")
        return []

    try:
        raw = data["choices"][0]["message"]["content"]
    except Exception:
        log_err(f"[LLM] Неожиданный формат ответа: {str(data)[:200]}")
        return []

    log(f"[LLM-RAW] «{term}» -> {raw!r}", level=1)

    variants = parse_llm_variants(raw, term, cfg.get("max_variants", 3))
    if variants:
        log(f"[LLM] «{term}» → {variants}", level=1)
    else:
        log(f"[LLM] «{term}» → (нет вариантов)", level=2)

    cache[term] = {"v": variants, "ts": int(datetime.now().timestamp())}
    cache.save()

    return variants


# ---------------------------------------------------------------------------
# Ограничитель обращений к TMDb
# ---------------------------------------------------------------------------

class Throttle:
    """Не больше N одновременных обращений к TMDb плюс пауза перед каждым.

    Раньше ограничитель был только у sonarr, а radarr бил по TMDb всеми
    результатами поиска сразу — одним и тем же ключом на оба сервиса, из-за
    чего ловил 429 (а ошибку TMDb sonarr раньше запоминал навсегда).
    """

    def __init__(self, limit, delay=0.3):
        self.limit = limit
        self.delay = delay
        self._sem = None

    @property
    def sem(self):
        # семафор создаём при первом обращении: модуль импортируется до старта loop
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.limit)
        return self._sem

    async def __aenter__(self):
        await self.sem.acquire()
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, *exc_info):
        self.sem.release()
        return False
