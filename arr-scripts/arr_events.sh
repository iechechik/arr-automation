#!/bin/bash
# arr_events.sh — обработчик событий Sonarr/Radarr (Connect -> Custom Script: Grab, Manual Interaction Required).
#
# Grab (qBittorrent): алгоритм подстраивается под фактическое состояние торрента:
#   * торрент уже качается (Initial State = Start/Force Start) -> скрипт только ставит приоритет 0
#     невыбранным эпизодам;
#   * торрент остановлен (Initial State = Stop) -> то же самое, затем скрипт его запускает;
#   * при любом сбое/раннем выходе остановленный торрент всё равно запускается (trap на выход).
# Radarr: фильтрации файлов нет, только гарантия, что торрент не остался остановленным.
#
# ManualInteractionRequired: файлы с «already imported» пропускаются, а после цикла
# (только если ошибок не было) записи этого хеша снимаются из очереди Sonarr и из клиента
# (removeFromClient=true) — так уходят фантомы. Внутри цикла ничего не удаляется.
#
# Доступ к qBittorrent задаётся переменными окружения контейнера Sonarr/Radarr:
#   QBIT_URL (по умолчанию http://qbittorrent:8080),
#   QBIT_API_KEY (qBittorrent >= 5.2) либо QBIT_USER + QBIT_PASS. Ключ приоритетнее.
# Прочее: LOG_FILE, FILES_WAIT.

LOG_FILE=${LOG_FILE:-/config/scripts/logs/arr_events.log}
LOG_MAX_BYTES=1048576
CURL_TIMEOUT=30
QBIT_URL=${QBIT_URL:-http://qbittorrent:8080}
QBIT_USER=${QBIT_USER:-}
QBIT_PASS=${QBIT_PASS:-}
QBIT_API_KEY=${QBIT_API_KEY:-}
# В compose запись "- VAR="значение"" передаёт кавычки как часть значения: срезаем их.
QBIT_URL=${QBIT_URL//[\"\']/}; QBIT_USER=${QBIT_USER//[\"\']/}; QBIT_PASS=${QBIT_PASS//[\"\']/}; QBIT_API_KEY=${QBIT_API_KEY//[\"\']/}
GRAB_HISTORY=${GRAB_HISTORY:-/config/scripts/logs/grabs.tsv}   # журнал захватов: видно, повторялся ли хеш
FILES_WAIT=${FILES_WAIT:-30}    # сколько секунд ждать список файлов торрента (магнет: метаданные)
STOPPED_GRACE=5                 # через сколько секунд запускать остановленный торрент без списка файлов

SCRIPT_NAME=$(basename "$0" .sh)
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] [$SCRIPT_NAME] $*" | tee -a "$LOG_FILE"
}

# --- Каталог логов и ротация ---
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$GRAB_HISTORY")" 2>/dev/null

if [ -f "$LOG_FILE" ]; then
    SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt "$LOG_MAX_BYTES" ]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1"
        log "Лог превысил $LOG_MAX_BYTES байт — предыдущий убран в $LOG_FILE.1"
    fi
fi

log "=== Старт $SCRIPT_NAME ==="

# =======================================================================
# 0. ОБЩАЯ ИНИЦИАЛИЗАЦИЯ
# =======================================================================
if [ -n "${sonarr_eventtype:-}" ]; then
    APP_TYPE="Sonarr"
    EVENT_TYPE="$sonarr_eventtype"
    SERIES_ID="${sonarr_series_id:-}"
    DOWNLOAD_ID="${sonarr_download_id:-}"
    DL_NAME="${sonarr_download_client:-}"
    DL_TYPE="${sonarr_download_client_type:-}"
    TITLE="${sonarr_series_title:-}"
    PORT=8989
    API_KEY="${SONARR_API_KEY:-}"
elif [ -n "${radarr_eventtype:-}" ]; then
    APP_TYPE="Radarr"
    EVENT_TYPE="$radarr_eventtype"
    SERIES_ID="${radarr_movie_id:-}"
    DOWNLOAD_ID="${radarr_download_id:-}"
    DL_NAME="${radarr_download_client:-}"
    DL_TYPE="${radarr_download_client_type:-}"
    TITLE="${radarr_movie_title:-}"
    PORT=7878
    API_KEY="${RADARR_API_KEY:-}"
else
    log "ОШИБКА: не удалось определить тип приложения."
    exit 1
fi

log "Приложение: $APP_TYPE, Событие: $EVENT_TYPE"

if [ "$EVENT_TYPE" = "Test" ]; then
    log "Тестовый запуск — выходим успешно"
    exit 0
fi

API_URL="http://localhost:$PORT/api/v3"
if [ -z "$API_KEY" ] && [ -f /config/config.xml ]; then
    API_KEY=$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml | head -1)
fi
NO_API=""
if [ -z "$API_KEY" ]; then
    log "ВНИМАНИЕ: не удалось получить API-ключ Sonarr/Radarr (нет переменной и не прочитан config.xml)."
    NO_API=1
fi

# =======================================================================
# qBittorrent
# =======================================================================
AUTH=()      # аргументы curl для авторизации (заголовок с ключом или cookie)
HASH=$(echo "$DOWNLOAD_ID" | tr 'A-Z' 'a-z')

qb_login() {
    local c code
    if [ -z "$QBIT_API_KEY" ] && [ -z "$QBIT_USER" ]; then
        log "Не заданы QBIT_API_KEY (или QBIT_USER/QBIT_PASS) в окружении контейнера."
        return 1
    fi
    if [ -n "$QBIT_API_KEY" ]; then
        log "API-ключ из окружения: длина ${#QBIT_API_KEY}, начало '${QBIT_API_KEY:0:4}'"
        AUTH=(-H "Authorization: Bearer $QBIT_API_KEY")
        code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$QBIT_URL/api/v2/app/version")
        [ "$code" = "200" ] && return 0
        log "API-ключ не принят qBittorrent (HTTP $code)."
        AUTH=(); return 1
    fi
    c=$(curl -s -m 10 -i --data-urlencode "username=$QBIT_USER" --data-urlencode "password=$QBIT_PASS" \
        "$QBIT_URL/api/v2/auth/login" | grep -i '^set-cookie:' | awk '{print $2}' | tr -d '\r')
    [ -n "$c" ] || return 1
    AUTH=(-b "$c")
}

torrent_state() {   # пусто, если торрента ещё нет
    curl -s -m 10 "${AUTH[@]}" "$QBIT_URL/api/v2/torrents/info?hashes=$HASH" | jq -r '.[0].state // empty' 2>/dev/null
}

is_stopped() {      # qBittorrent 4.x: pausedDL, 5.x: stoppedDL
    case "$1" in pausedDL|stoppedDL) return 0 ;; *) return 1 ;; esac
}

start_torrent() {
    local code
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "${AUTH[@]}" -d "hashes=$HASH" "$QBIT_URL/api/v2/torrents/start")
    if [ "$code" = "404" ]; then   # qBittorrent 4.x: метод называется resume
        code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "${AUTH[@]}" -d "hashes=$HASH" "$QBIT_URL/api/v2/torrents/resume")
    fi
    log "запуск торрента $HASH: HTTP $code"
}

ensure_started() {   # 0 — торрент работает, 1 — поднять не удалось
    local st i
    for i in $(seq 1 60); do
        st=$(torrent_state)
        case "$st" in
            "")
                sleep 1; continue ;;
            checking*|metaDL|queuedDL|allocating|forcedMetaDL|moving)
                # клиент проверяет данные / тянет метаданные — ждём
                sleep 1; continue ;;
            pausedDL|stoppedDL)
                log "ensure_started: торрент остановлен ($st), запускаем."
                start_torrent
                sleep 2 ;;
            error|missingFiles)
                log "ensure_started: состояние '$st' — торрент нерабочий."
                return 1 ;;
            *)
                return 0 ;;   # downloading/uploading/stalled*/stoppedUP — уже в работе
        esac
        st=$(torrent_state)
        case "$st" in
            ""|pausedDL|stoppedDL|checking*|metaDL|queuedDL|allocating|forcedMetaDL|moving)
                : ;;
            error|missingFiles)
                log "ensure_started: состояние '$st' — торрент нерабочий."
                return 1 ;;
            *)
                return 0 ;;
        esac
    done
    log "ensure_started: торрент не поднялся за отведённое время (последнее состояние '$st')."
    return 1
}

drop_grab() {   # снять захват в Sonarr: записи этого хэша из очереди и из клиента
    local h_upper items id code
    h_upper=$(echo "$HASH" | tr 'a-z' 'A-Z')
    log "drop_grab: снимаю захват $h_upper из очереди Sonarr..."
    items=$(curl -s -m "$CURL_TIMEOUT" -H "X-Api-Key: $API_KEY" \
        "$API_URL/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true" \
        | jq -r --arg h "$h_upper" '.records[] | select((.downloadId // "" | ascii_upcase) == $h) | .id' 2>/dev/null)
    if [ -z "$items" ]; then
        log "drop_grab: записей этого хэша в очереди нет."
        return 0
    fi
    for id in $items; do
        code=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' -X DELETE \
            -H "X-Api-Key: $API_KEY" \
            "$API_URL/queue/$id?removeFromClient=true&blocklist=false&changeCategory=false")
        log "drop_grab: DELETE /queue/$id -> HTTP $code"
    done
    curl -s -m 5 -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
        -d '{"name":"RefreshMonitoredDownloads"}' "$API_URL/command" > /dev/null
}

rename_torrent() {   # $1 — новое имя торрента в клиенте
    local name="$1" code
    [ -z "$name" ] && return 0
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "${AUTH[@]}" \
        --data-urlencode "hash=$HASH" --data-urlencode "name=$name" \
        "$QBIT_URL/api/v2/torrents/rename")
    if [ "$code" = "200" ]; then
        log "Торрент переименован: «$name»"
    else
        log "Переименование не удалось: HTTP $code (продолжаем без него)"
    fi
}

on_exit() {
    # Грабя не было (нет хэша) или событие не Grab — делать нечего.
    if [ "$EVENT_TYPE" != "Grab" ] || [ -z "$HASH" ]; then
        return 0
    fi
    if [ ${#AUTH[@]} -eq 0 ]; then
        if qb_login; then
            log "on_exit: авторизация в клиенте восстановлена."
        else
            log "on_exit: клиент недоступен — снимаю захват, чтобы очередь не висела."
            drop_grab
            return 0
        fi
    fi
    if ensure_started; then
        log "on_exit: торрент в работе."
    else
        log "on_exit: торрент запустить не удалось — снимаю захват (removeFromClient=true)."
        drop_grab
    fi
}
trap on_exit EXIT

# =======================================================================
# УТИЛИТА: сброс залипших состояний в Sonarr (sonarr-untrack)
# =======================================================================
sonarr_untrack() {
    local h_upper=$(echo "$HASH" | tr 'a-z' 'A-Z')
    log "Проверка залипших записей в очереди Sonarr (всё, кроме downloading/importing)..."
    
    local queue_json=$(curl -s -m "$CURL_TIMEOUT" -H "X-Api-Key: $API_KEY" \
        "$API_URL/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true&includeEpisode=true")
    
    if [ -z "$queue_json" ]; then
        log "  Не удалось получить очередь."
        return
    fi
    
    # Ищем записи с нашим хешем и состоянием imported/ignored
    local items=$(echo "$queue_json" | jq -r --arg h "$h_upper" \
        '.records[] | select((.downloadId // "" | ascii_upcase) == $h and (["downloading","importing"] | index(.trackedDownloadState // "") | not)) | .id')
    
    if [ -n "$items" ]; then
        for id in $items; do
            log "  Сброс залипшей записи ID=$id..."
            CODE=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
                -H "X-Api-Key: $API_KEY" \
                "$API_URL/queue/$id?removeFromClient=false&blocklist=false&changeCategory=true")
            log "    DELETE /queue/$id: HTTP $CODE"
        done
        # Просим обновить список
        curl -s -m 5 -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
            -d '{"name":"RefreshMonitoredDownloads"}' "$API_URL/command" > /dev/null
        log "  Запрошен RefreshMonitoredDownloads."
    else
        log "  Залипших записей не найдено."
    fi
}

# =======================================================================
# 1. GRAB
# =======================================================================
if [ "$EVENT_TYPE" = "Grab" ]; then
    log "--- Обработка Grab ---"

    # Без API-ключа очередь не проверить и файлы не отфильтровать,
    # но торрент всё равно обязан быть запущен — это сделает trap on_exit.
    if [ -n "$NO_API" ]; then
        log "Нет API-ключа: пропускаю проверки и фильтрацию, торрент будет запущен."
        exit 0
    fi

    if [ -z "$HASH" ]; then
        log "Нет download_id. Выход."
        exit 0
    fi

    # Вызываем сброс залипших записей в начале
    if [ "$APP_TYPE" = "Sonarr" ]; then
        sonarr_untrack
    fi

    log "Релиз: ${sonarr_release_title:-${radarr_release_title:-?}} | индексер: ${sonarr_release_indexer:-${radarr_release_indexer:-?}} | хеш: $HASH"
    log "Эпизоды в запросе Sonarr: сезон ${sonarr_release_seasonnumber:-?}, номера ${sonarr_release_episodenumbers:-?}"
    PREV=$(grep -F "$HASH" "$GRAB_HISTORY" 2>/dev/null | tail -3)
    if [ -n "$PREV" ]; then
        log "ЭТОТ ХЕШ УЖЕ ПОПАДАЛСЯ РАНЬШЕ (дата, хеш, эпизоды):"
        echo "$PREV" | while IFS= read -r l; do log "    $l"; done
    else
        log "Хеш новый (в журнале $GRAB_HISTORY не встречался)."
    fi
    printf '%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$HASH" "S${sonarr_release_seasonnumber:-?}E${sonarr_release_episodenumbers:-?}" >> "$GRAB_HISTORY" 2>/dev/null
    if [ "$(wc -l < "$GRAB_HISTORY" 2>/dev/null || echo 0)" -gt 500 ]; then
        tail -n 300 "$GRAB_HISTORY" > "$GRAB_HISTORY.tmp" && mv -f "$GRAB_HISTORY.tmp" "$GRAB_HISTORY"
    fi
    if [ -n "$DL_TYPE" ] && [[ "$DL_TYPE" != *[Qq][Bb]ittorrent* ]]; then
        log "Клиент '$DL_NAME' ($DL_TYPE) не qBittorrent. Выход."
        exit 0
    fi
    if [ -z "$DL_TYPE" ] && [[ "$DL_NAME" != *[Qq][Bb]ittorrent* ]]; then
        log "Клиент '$DL_NAME' не похож на qBittorrent (тип не передан). Выход."
        exit 0
    fi
    if ! qb_login; then
        log "ОШИБКА: не удалось авторизоваться в qBittorrent (см. строки выше)."
        exit 1
    fi

    CS=$(curl -s -m 5 -H "X-Api-Key: $API_KEY" "$API_URL/downloadclient" | \
        jq -r --arg n "$DL_NAME" '.[]? | select(.implementation=="QBittorrent" and ($n=="" or .name==$n)) | (.fields[]? | select(.name=="initialState") | .value) // empty' 2>/dev/null | head -1)
    ST=$(torrent_state)
    case "$CS" in 0) CSN=Start ;; 1) CSN=ForceStart ;; 2) CSN=Stop ;; *) CSN="${CS:-неизвестно}" ;; esac
    log "Initial State клиента в $APP_TYPE: $CSN; фактическое состояние торрента: ${ST:-нет в клиенте}"

    REQUESTED_EPS="${sonarr_release_episodenumbers:-}"
    REQ_SEASON="${sonarr_release_seasonnumber:-}"
    if [ "$APP_TYPE" != "Sonarr" ] || [ -z "$REQUESTED_EPS" ]; then
        log "Фильтрация файлов не нужна. Выход (торрент будет запущен, если остановлен)."
        exit 0
    fi
    REQUESTED_EPS_LIST=$(echo "$REQUESTED_EPS" | tr -d ' ' | tr ',' '\n')
    log "Запрошены эпизоды (сезон ${REQ_SEASON:-?}): $(echo $REQUESTED_EPS_LIST | tr '\n' ' ')"

    # Имя торрента в клиенте -> по нему Sonarr решает, сколько серий ждать при импорте.
    # Ставим маркер запрошенных серий, иначе пак считается полным сезоном.
    if [ -n "$REQ_SEASON" ]; then
        MARK="S${REQ_SEASON}"
        while IFS= read -r EN; do
            [ -n "$EN" ] && MARK="${MARK}E${EN}"
        done <<< "$REQUESTED_EPS_LIST"
        CUR_NAME=$(curl -s -m 10 "${AUTH[@]}" "$QBIT_URL/api/v2/torrents/info?hashes=$HASH" \
            | jq -r '.[0].name // empty' 2>/dev/null)
        if [ -n "$CUR_NAME" ] && [[ "$CUR_NAME" == *"$MARK"* ]]; then
            log "Имя торрента уже содержит $MARK: «$CUR_NAME»"
        else
            NEW_NAME="${TITLE:+$TITLE }${MARK}"
            log "Текущее имя: «${CUR_NAME:-нет}» -> «$NEW_NAME»"
            rename_torrent "$NEW_NAME"
        fi
    fi

    FILES_JSON=""
    STARTED_EARLY=0
    TICKS=$((FILES_WAIT * 2))
    for ((i = 0; i < TICKS; i++)); do
        FILES_JSON=$(curl -s -m 5 "${AUTH[@]}" "$QBIT_URL/api/v2/torrents/files?hash=$HASH")
        if [ -n "$FILES_JSON" ] && [ "$FILES_JSON" != "[]" ] && echo "$FILES_JSON" | jq -e 'type=="array"' >/dev/null 2>&1; then
            break
        fi
        FILES_JSON=""
        if [ "$STARTED_EARLY" -eq 0 ] && [ "$i" -ge $((STOPPED_GRACE * 2)) ] && is_stopped "$(torrent_state)"; then
            log "Файлов нет за ${STOPPED_GRACE} сек, торрент остановлен — запускаем ради метаданных."
            start_torrent
            STARTED_EARLY=1
        fi
        sleep 0.5
    done
    if [ -z "$FILES_JSON" ]; then
        log "Список файлов не появился за ${FILES_WAIT} сек. Выход (торрент останется целиком)."
        exit 0
    fi

    UNWANTED=""
    WANTED=""
    for ID in $(echo "$FILES_JSON" | jq -r 'keys[]'); do
        FILE_NAME=$(echo "$FILES_JSON" | jq -r ".[$ID].name")
        SN=""; EN=""
        read -r SN EN < <(echo "$FILE_NAME" | sed -nE 's/.*[sS]0*([0-9]+)[eE]0*([0-9]+).*/\1 \2/p')

        # Русские имена файлов: «Серия 05», «5 серия», «Эпизод 3», «.E05.mkv».
        # Классы вида [Сс] в sed на многобайтной кириллице не работают — только
        # литеральные варианты в скобках. Неверно распознать хуже, чем не распознать:
        # непонятый файл просто скачается целиком, а неверно обнулённый сломает импорт.
        if [ -z "$EN" ]; then
            EN=$(echo "$FILE_NAME" | sed -nE 's/.*(Серия|серия|СЕРИЯ|Серии|серии|СЕРИИ|серию)[ _.-]*0*([0-9]{1,3}).*/\2/p' | head -1)
            [ -n "$EN" ] && log "  (номер из «Серия N»)"
        fi
        if [ -z "$EN" ]; then
            EN=$(echo "$FILE_NAME" | sed -nE 's/(^|.*[^0-9])0*([0-9]{1,3})[ _.-]*(серия|Серия|СЕРИЯ|серии|Серии|СЕРИИ|серию).*/\2/p' | head -1)
            [ -n "$EN" ] && log "  (номер из «N серия»)"
        fi
        if [ -z "$EN" ]; then
            EN=$(echo "$FILE_NAME" | sed -nE 's/.*(Эпизод|эпизод|ЭПИЗОД|Эпизоды|эпизоды|ЭПИЗОДЫ)[ _.-]*0*([0-9]{1,3}).*/\2/p' | head -1)
            [ -n "$EN" ] && log "  (номер из «Эпизод N»)"
        fi
        if [ -z "$EN" ]; then
            EN=$(echo "$FILE_NAME" | sed -nE 's/.*[^A-Za-z0-9][Ee]0*([0-9]{1,3})\.[A-Za-z0-9]+$/\1/p' | head -1)
            [ -n "$EN" ] && log "  (номер из «.E05.ext»)"
        fi
        if [ -n "$EN" ] && [ -z "$SN" ]; then
            SN="$REQ_SEASON"
        fi
        if [ -z "$EN" ]; then
            log "  [--] '$FILE_NAME' — номер эпизода не распознан. Не трогаем."
            continue
        fi
        if echo "$REQUESTED_EPS_LIST" | grep -qx "$EN" && { [ -z "$REQ_SEASON" ] || [ "$SN" = "$REQ_SEASON" ]; }; then
            log "  [OK] '$FILE_NAME' — качаем."
            WANTED="${WANTED:+$WANTED|}$ID"
        else
            log "  [0]  '$FILE_NAME' — не запрошен. Приоритет 0."
            UNWANTED="${UNWANTED:+$UNWANTED|}$ID"
        fi
    done
    if [ -n "$UNWANTED" ]; then
        CODE=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' -X POST "${AUTH[@]}" \
            -d "hash=$HASH&id=$UNWANTED&priority=0" "$QBIT_URL/api/v2/torrents/filePrio")
        log "filePrio=0 для файлов $UNWANTED: HTTP $CODE"
    fi
    # Торрент может остаться в клиенте между захватами: файл, обнулённый в прошлый раз,
    # надо вернуть в нормальный приоритет, иначе нужная серия так и не скачается.
    if [ -n "$WANTED" ]; then
        CODE=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' -X POST "${AUTH[@]}" \
            -d "hash=$HASH&id=$WANTED&priority=1" "$QBIT_URL/api/v2/torrents/filePrio")
        log "filePrio=1 для файлов $WANTED: HTTP $CODE"
    fi
    log "=== Готово (Grab) ==="
    exit 0
fi

# =======================================================================
# 2. ЛОГИКА ДЛЯ MANUAL INTERACTION REQUIRED
# =======================================================================
if [ "$EVENT_TYPE" = "ManualInteractionRequired" ]; then
    log "--- Обработка Manual Interaction Required ---"

    if [ -n "$NO_API" ]; then
        log "Нет API-ключа: ручной импорт обработать не могу. Выход."
        exit 1
    fi

    if [ -z "$SERIES_ID" ] || [ -z "$DOWNLOAD_ID" ]; then
        log "Не хватает данных (id объекта или downloadId). Пропускаем."
        exit 0
    fi

    log "Объект: «$TITLE» (id=$SERIES_ID)"
    log "Download ID: $DOWNLOAD_ID"

    # 1. Получаем файлы для импорта
    FILES_JSON=$(curl -s -m "$CURL_TIMEOUT" \
        -H "X-Api-Key: $API_KEY" \
        "$API_URL/manualimport?downloadId=$DOWNLOAD_ID&filterExistingFiles=true")

    FILES_COUNT=$(echo "$FILES_JSON" | jq '. | length')
    log "Файлов в очереди: $FILES_COUNT"

    if [ "$FILES_COUNT" -eq 0 ] || [ "$FILES_COUNT" = "null" ]; then
        log "Нет файлов для импорта (уже импортированы или удалены). Пропускаем."
        exit 0
    fi

    if [ "$APP_TYPE" = "Sonarr" ]; then
        log "Загружаем эпизоды..."
        EPISODES_JSON=$(curl -s -m "$CURL_TIMEOUT" \
            -H "X-Api-Key: $API_KEY" \
            "$API_URL/episode?seriesId=$SERIES_ID")
        EP_COUNT=$(echo "$EPISODES_JSON" | jq '. | length')
        log "Эпизодов в базе: $EP_COUNT"
    fi

    extract_episode_number() {
        local name="$1"
        local num
        num=$(echo "$name" | sed -nE 's/^([0-9]+)[. ].*/\1/p'); [ -n "$num" ] && echo "$((10#$num))" && return
        num=$(echo "$name" | sed -nE 's/.*[Ss][0-9]+[Ee]([0-9]+).*/\1/p'); [ -n "$num" ] && echo "$((10#$num))" && return
        num=$(echo "$name" | sed -nE 's/.*[0-9]+[Xx]([0-9]+).*/\1/p'); [ -n "$num" ] && echo "$((10#$num))" && return
        if echo "$name" | grep -qi "сери"; then
            num=$(echo "$name" | grep -oE '[0-9]+' | head -1); [ -n "$num" ] && echo "$((10#$num))" && return
        fi
        num=$(echo "$name" | sed -E 's/.*[^0-9]([0-9]+)\..*/\1/'); [ -n "$num" ] && [ "$num" != "$name" ] && echo "$((10#$num))" && return
        num=$(echo "$name" | grep -oE '[0-9]+' | tail -1); [ -n "$num" ] && echo "$((10#$num))" && return
        echo ""
    }

    extract_season_number() {
        local name="$1"
        local num
        num=$(echo "$name" | sed -nE 's/.*[Ss]([0-9]+)[Ee][0-9]+.*/\1/p'); [ -n "$num" ] && echo "$((10#$num))" && return
        num=$(echo "$name" | sed -nE 's/.*([0-9]+)[Xx][0-9]+.*/\1/p'); [ -n "$num" ] && echo "$((10#$num))" && return
        echo "1"
    }

    TMPFILE=$(mktemp)
    echo "$FILES_JSON" | jq -c '.[]' > "$TMPFILE"

    SUCCESS=0
    FAIL=0
    SKIPPED=0
    CMD_IDS=""

    while IFS= read -r file_item; do
        FILE_PATH=$(echo "$file_item" | jq -r '.path')
        FILE_NAME=$(echo "$file_item" | jq -r '.name')
        REJECTION=$(echo "$file_item" | jq -r '.rejections[0].reason // "none"')
        IS_ALREADY=$(echo "$file_item" | jq -r '[.rejections[]?.reason // ""] | any(ascii_downcase | test("already imported"))')

        log "---"
        log "Файл    : $FILE_NAME"
        log "Путь    : $FILE_PATH"
        log "Отказ   : $REJECTION"

        # Уже импортированный файл просто пропускаем. Очередь чистим ОДИН раз после
        # цикла и только при FAIL=0 — иначе первый же такой файл удалит торрент
        # вместе с ещё не обработанными файлами.
        if [ "$IS_ALREADY" = "true" ] || echo "$REJECTION" | grep -qi "already imported"; then
            log "Файл уже импортирован Sonarr'ом — пропускаем (очередь почистим после цикла)."
            SKIPPED=$((SKIPPED + 1))
            continue
        fi

        QUALITY_ID=$(echo "$file_item"    | jq '.quality.quality.id // 1')
        QUALITY_REV=$(echo "$file_item"   | jq '.quality.revision // {"version":1,"real":0,"isRepack":false}')
        LANG_ID=$(echo "$file_item"       | jq '.languages[0].id // 1')
        RELEASE_GROUP=$(echo "$file_item" | jq -r '.releaseGroup // "Unknown"')

        if [ "$APP_TYPE" = "Sonarr" ]; then
            EP_NUM=$(extract_episode_number "$FILE_NAME")
            SEASON_NUM=$(extract_season_number "$FILE_NAME")
            log "Определено: сезон=$SEASON_NUM эпизод=$EP_NUM"

            if [ -z "$EP_NUM" ]; then
                log "ПРОПУСК: не удалось извлечь номер эпизода"
                FAIL=$((FAIL + 1)); continue
            fi

            EPISODE_ID=$(echo "$EPISODES_JSON" | jq --argjson ep "$EP_NUM" --argjson sn "$SEASON_NUM" '.[] | select(.seasonNumber==$sn and .episodeNumber==$ep) | .id' | head -1)

            if [ -z "$EPISODE_ID" ] || [ "$EPISODE_ID" = "null" ]; then
                log "ПРОПУСК: эпизод S${SEASON_NUM}E${EP_NUM} не найден"
                FAIL=$((FAIL + 1)); continue
            fi

            log "Найден episodeId=$EPISODE_ID"

            IMPORT_BODY=$(jq -n \
                --arg     path         "$FILE_PATH" \
                --argjson seriesId     "$SERIES_ID" \
                --argjson seasonNumber "$SEASON_NUM" \
                --argjson episodeIds   "[$EPISODE_ID]" \
                --argjson qualityId    "$QUALITY_ID" \
                --argjson qualityRev   "$QUALITY_REV" \
                --argjson langId       "$LANG_ID" \
                --arg     releaseGroup "$RELEASE_GROUP" \
                --arg     downloadId   "$DOWNLOAD_ID" \
                '{ name: "ManualImport", files: [{ path: $path, seriesId: $seriesId, seasonNumber: $seasonNumber, episodeIds: $episodeIds, quality: { quality: { id: $qualityId }, revision: $qualityRev }, languages: [{ id: $langId }], releaseGroup: $releaseGroup, downloadId: $downloadId }], importMode: "auto" }')
        else
            IMPORT_BODY=$(jq -n \
                --arg     path         "$FILE_PATH" \
                --argjson movieId      "$SERIES_ID" \
                --argjson qualityId    "$QUALITY_ID" \
                --argjson qualityRev   "$QUALITY_REV" \
                --argjson langId       "$LANG_ID" \
                --arg     releaseGroup "$RELEASE_GROUP" \
                --arg     downloadId   "$DOWNLOAD_ID" \
                '{ name: "ManualImport", files: [{ path: $path, movieId: $movieId, quality: { quality: { id: $qualityId }, revision: $qualityRev }, languages: [{ id: $langId }], releaseGroup: $releaseGroup, downloadId: $downloadId }], importMode: "auto" }')
        fi

        log "Отправляем импорт..."
        RESP=$(curl -s -m "$CURL_TIMEOUT" -w "\n%{http_code}" -X POST \
            -H "X-Api-Key: $API_KEY" \
            -H "Content-Type: application/json" \
            -d "$IMPORT_BODY" \
            "$API_URL/command")

        HTTP_CODE=$(echo "$RESP" | tail -1)
        RESP_BODY=$(echo "$RESP" | sed '$d')

        if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
            log "OK (HTTP $HTTP_CODE): $FILE_NAME"
            SUCCESS=$((SUCCESS + 1))
            CMD_ID_DONE=$(echo "$RESP_BODY" | jq -r '.id // empty' 2>/dev/null)
            [ -n "$CMD_ID_DONE" ] && CMD_IDS="$CMD_IDS $CMD_ID_DONE"
        else
            log "ОШИБКА (HTTP $HTTP_CODE): $RESP_BODY"
            FAIL=$((FAIL + 1))
        fi
    done < "$TMPFILE"

    rm -f "$TMPFILE"

    # POST /command возвращается сразу, импорт идёт в фоне. Дожидаемся команд:
    # иначе чистка очереди может удалить торрент с ещё не импортированными файлами.
    for C in $CMD_IDS; do
        S=""
        for _ in $(seq 1 60); do
            S=$(curl -s -m 10 -H "X-Api-Key: $API_KEY" "$API_URL/command/$C" | jq -r '.status // empty')
            case "$S" in
                completed) break ;;
                failed|aborted)
                    log "  команда импорта $C завершилась статусом $S"
                    FAIL=$((FAIL + 1)); break ;;
            esac
            sleep 2
        done
        # Не дождались за 2 минуты — импорт ещё идёт: чистка запрещена.
        case "$S" in
            completed|failed|aborted) ;;
            *)
                log "  команда импорта $C не завершилась за 2 мин (статус: ${S:-?}) — чистку очереди пропускаем"
                FAIL=$((FAIL + 1)) ;;
        esac
    done

    # Часть файлов уже была импортирована и ни один не упал -> снимаем записи этого
    # хэша из очереди и из клиента (фантомы). Только ПОСЛЕ обработки всех файлов.
    if [ "$SKIPPED" -gt 0 ] && [ "$FAIL" -eq 0 ]; then
        HASH_UPPER=$(echo "$HASH" | tr 'a-z' 'A-Z')
        QUEUE_JSON=$(curl -s -m "$CURL_TIMEOUT" -H "X-Api-Key: $API_KEY" \
            "$API_URL/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true&includeEpisode=true")
        TO_DELETE=$(echo "$QUEUE_JSON" | jq -r --arg h "$HASH_UPPER" \
            '.records[] | select((.downloadId // "" | ascii_upcase) == $h) | .id' 2>/dev/null)
        if [ -n "$TO_DELETE" ]; then
            for qid in $TO_DELETE; do
                QCODE=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
                    -H "X-Api-Key: $API_KEY" \
                    "$API_URL/queue/$qid?removeFromClient=true&blocklist=false&changeCategory=false")
                log "  чистка очереди: DELETE /queue/$qid -> HTTP $QCODE"
            done
            curl -s -m 5 -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
                -d '{"name":"RefreshMonitoredDownloads"}' "$API_URL/command" > /dev/null
        else
            log "  чистка очереди: записей этого хэша нет."
        fi
    fi

    log "=== Готово. Успешно: $SUCCESS, ошибок: $FAIL, пропущено (уже импортированы): $SKIPPED ==="
    if [ "$FAIL" -gt 0 ]; then
        exit 1
    fi
    exit 0
fi

log "Событие «$EVENT_TYPE» не обрабатывается."
exit 0
