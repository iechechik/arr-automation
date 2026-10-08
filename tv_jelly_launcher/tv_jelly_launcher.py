import os
import time
import json
import logging
import threading
import subprocess

from pywebostv.connection import WebOSClient
from pywebostv.controls import ApplicationControl

# --- Logging ---
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Configuration ---
TVS_RAW = os.environ.get("TVS", "").strip()
if not TVS_RAW:
    raise ValueError("TVS environment variable is required (comma-separated IPs or hostnames)")

# Список телевизоров: [{"name": "tv1", "host": "192.168.1.32"}, ...]
TVS = []
for entry in TVS_RAW.split(","):
    entry = entry.strip()
    if not entry:
        continue
    if "=" in entry:
        name, host = entry.split("=", 1)
        TVS.append({"name": name.strip(), "host": host.strip()})
    else:
        TVS.append({"name": entry, "host": entry})

if not TVS:
    raise ValueError("TVS is empty after parsing")

PING_INTERVAL = int(os.environ.get("PING_INTERVAL", "5"))
KEY_DIR = "/app/tv_key"
DEBUG_PING = os.environ.get("DEBUG_PING", "false").lower() == "true"
JELLYFIN_MATCH = os.environ.get("JELLYFIN_MATCH", "jellyfin").lower()

# Подробные логи пинга включаются только DEBUG_PING=true, иначе не шумим
# (DEBUG-уровень нужен pywebostv/ws4py только при отладке).
if not DEBUG_PING:
    logger.setLevel(logging.INFO)
    logging.getLogger().setLevel(logging.INFO)

# Запуск идёт в отдельном потоке: register() может ждать подтверждения на
# телевизоре до 60 с (его внутренний таймаут), и всё это время основной цикл
# не опрашивал бы остальные телевизоры.
_launching = set()
_launch_lock = threading.Lock()


# --- Ping ---
def ping(host: str) -> bool:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "1", host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ok = result.returncode == 0
    if DEBUG_PING:
        logger.debug("Ping %s -> %s", host, "OK" if ok else "FAIL")
    return ok


# --- Key storage (per-TV) ---
def key_file_for(tv: dict) -> str:
    safe = tv["name"].replace("/", "_").replace(" ", "_")
    return os.path.join(KEY_DIR, f"{safe}_client_key.json")


def id_cache_for(tv: dict) -> str:
    safe = tv["name"].replace("/", "_").replace(" ", "_")
    return os.path.join(KEY_DIR, f"{safe}_jellyfin_id.txt")


def load_store(tv: dict) -> dict:
    path = key_file_for(tv)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("[%s] Could not read key file, will re-pair", tv["name"])
    return {}


def save_store(tv: dict, store: dict) -> None:
    os.makedirs(KEY_DIR, exist_ok=True)
    path = key_file_for(tv)
    with open(path, "w") as f:
        json.dump(store, f)
    logger.info("[%s] Client key saved to %s", tv["name"], path)


# --- Jellyfin launch ---
def launch_jellyfin(tv: dict) -> bool:
    host = tv["host"]
    name = tv["name"]
    store = load_store(tv)
    client = WebOSClient(host)

    logger.info("[%s] Connecting to TV at %s ...", name, host)
    try:
        client.connect()
    except Exception as e:
        logger.error("[%s] Failed to connect: %s", name, e)
        return False

    try:
        for status in client.register(store):
            if status == WebOSClient.PROMPTED:
                logger.warning("[%s] *** ACCEPT THE CONNECTION PROMPT ON YOUR TV ***", name)
            elif status == WebOSClient.REGISTERED:
                logger.info("[%s] Registration OK", name)
                break
    except Exception as e:
        logger.error("[%s] Registration failed: %s", name, e)
        return False

    save_store(tv, store)

    app = ApplicationControl(client)

    # Пробуем кэш
    cache_path = id_cache_for(tv)
    jellyfin_id = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                jellyfin_id = f.read().strip()
            logger.info("[%s] Using cached Jellyfin id: %s", name, jellyfin_id)
        except OSError:
            jellyfin_id = None

    if jellyfin_id:
        try:
            app.launch(jellyfin_id)
            logger.info("[%s] Jellyfin launched (cached id)", name)
            return True
        except Exception as e:
            logger.warning("[%s] Cached launch failed (%s), full scan", name, e)
            jellyfin_id = None

    # Полный поиск
    try:
        apps = app.list_apps()
    except Exception as e:
        logger.error("[%s] Failed to list apps: %s", name, e)
        return False

    jellyfin = None
    for a in apps:
        title = (a["title"] or "").lower()
        app_id = (a["id"] or "").lower()
        if JELLYFIN_MATCH in title or JELLYFIN_MATCH in app_id:
            jellyfin = a
            break

    if jellyfin is None:
        logger.error("[%s] Jellyfin not found in installed apps", name)
        return False

    try:
        os.makedirs(KEY_DIR, exist_ok=True)
        with open(cache_path, "w") as f:
            f.write(jellyfin["id"])
        logger.info("[%s] Cached Jellyfin id: %s", name, jellyfin["id"])
    except OSError as e:
        logger.warning("[%s] Could not save id cache: %s", name, e)

    logger.info("[%s] Launching Jellyfin: %s (%s)", name, jellyfin["title"], jellyfin["id"])
    try:
        app.launch(jellyfin)
        logger.info("[%s] Jellyfin launched successfully", name)
        return True
    except Exception as e:
        logger.error("[%s] Failed to launch Jellyfin: %s", name, e)
        return False


def launch_async(tv: dict) -> None:
    """Запускает launch_jellyfin в потоке, по одному запуску на телевизор."""
    name = tv["name"]
    with _launch_lock:
        if name in _launching:
            logger.info("[%s] Launch already in progress — skip", name)
            return
        _launching.add(name)

    def worker() -> None:
        try:
            time.sleep(1)          # даём webOS подняться после включения
            launch_jellyfin(tv)
        except Exception as e:     # поток не должен умирать молча
            logger.exception("[%s] Unexpected launch error: %s", name, e)
        finally:
            with _launch_lock:
                _launching.discard(name)

    threading.Thread(target=worker, name=f"launch-{name}", daemon=True).start()


# --- Main ---
def main() -> None:
    logger.info("=== Jellyfin Launcher started ===")
    logger.info("TVs: %s", ", ".join(f'{t["name"]}({t["host"]})' for t in TVS))
    logger.info("PING_INTERVAL=%ds  DEBUG_PING=%s  JELLYFIN_MATCH=%r",
                PING_INTERVAL, DEBUG_PING, JELLYFIN_MATCH)

    # Состояние каждого ТВ
    online_state = {t["name"]: False for t in TVS}

    while True:
        try:
            for tv in TVS:
                name = tv["name"]
                host = tv["host"]
                was_online = online_state[name]
                is_online = ping(host)

                if is_online and not was_online:
                    logger.info("[%s] TV came online", name)
                    launch_async(tv)
                elif not is_online and was_online:
                    logger.info("[%s] TV went offline", name)

                online_state[name] = is_online

            time.sleep(PING_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Shutting down ...")
            break
        except Exception as e:
            logger.exception("Unexpected error in main loop: %s", e)
            time.sleep(PING_INTERVAL)


if __name__ == "__main__":
    main()
