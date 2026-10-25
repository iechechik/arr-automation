#!/bin/bash
# =============================================================================
# sonarr_tracking_series.sh — автодокачка пропущенных серий Sonarr
# =============================================================================
#
# Что делает:
#   1. Берёт у Sonarr список пропущенных серий (/wanted/missing: отслеживаемые,
#      уже вышедшие, без файла на диске).
#   2. Группирует их по «сериал + сезон».
#   3. Пропускает сериалы, которые уже есть в очереди (чтобы не захватить дважды).
#   4. По каждой оставшейся паре запускает SeasonSearch — сезонный поиск.
#      Сужение пака до недостающих серий делает prowlarr-proxy, поэтому один
#      торрент может закрыть сразу несколько серий (например S1E3E4).
#   5. Останавливается после первого же захвата (MAX_GRABS), чтобы не сыпать
#      пачку торрентов в qBittorrent за один прогон.
#
# Запуск: внутри контейнера Sonarr —
#   docker exec sonarr /config/scripts/sonarr_tracking_series.sh
#
# Переменные окружения:
#   DRY=1            — только показать, что искали бы, ничего не запускать
#   MAX_GRABS=N      — сколько захватов допустимо за прогон (по умолчанию 1)
#   SEARCH_TIMEOUT=N — сколько секунд ждать завершения одной команды поиска (300)
#   LOG_FILE=...     — путь лога (по умолчанию /config/scripts/sonarr_tracking_series.log)
#   API_URL, API_KEY — если нужно переопределить (по умолчанию localhost:8989 и config.xml)
# =============================================================================

LOG_FILE=${LOG_FILE:-/config/scripts/logs/sonarr_tracking_series.log}
LOG_MAX_BYTES=1048576
API_URL=${API_URL:-http://localhost:8989/api/v3}
API_KEY=${API_KEY:-}
DRY=${DRY:-0}
MAX_GRABS=${MAX_GRABS:-1}
STATE_FILE=${STATE_FILE:-/config/scripts/state/tracking_state.tsv}
REPEAT_HOURS=${REPEAT_HOURS:-6}   # не искать ту же пару раньше, чем через N часов (если поиск был пустым)
SEARCH_TIMEOUT=${SEARCH_TIMEOUT:-300}

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] [tracking] $*" | tee -a "$LOG_FILE"
}

# --- каталог логов и ротация ------------------------------------------------
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null

if [ -f "$LOG_FILE" ]; then
    SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt "$LOG_MAX_BYTES" ]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1"
    fi
fi

# --- ключ -------------------------------------------------------------------
if [ -z "$API_KEY" ] && [ -f /config/config.xml ]; then
    API_KEY=$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml | head -1)
fi
if [ -z "$API_KEY" ]; then
    log "ОШИБКА: не удалось получить API-ключ Sonarr"
    exit 1
fi

# --- замок: ручной запуск не должен пересечься с таймером -------------------
exec 9>"/tmp/sonarr_tracking_series.lock" || exit 0
if ! flock -n 9; then
    log "Уже выполняется другой запуск — выходим."
    exit 0
fi

api() {
    curl -s -m 60 -H "X-Api-Key: $API_KEY" "$@"
}

# История захватов обновляется сразу, а очередь — только на следующем опросе
# клиента (~40 c). Поэтому «взяли или нет» смотрим по истории, очередь — запасной признак.
# Сравнивать надо ID записи, а не хэш: при докачке того же пака хэш не меняется,
# и сравнение по хэшу даёт ложное «ничего не взято».
last_grab_id() {   # $1 seriesId
    api "$API_URL/history?seriesId=$1&eventType=1&pageSize=1&sortKey=date&sortDirection=descending" \
        | jq -r '.records[0].id // empty'
}

last_grab_hash() {   # $1 seriesId — только для лога
    api "$API_URL/history?seriesId=$1&eventType=1&pageSize=1&sortKey=date&sortDirection=descending" \
        | jq -r '.records[0].downloadId // empty'
}

state_get() {   # $1 ключ seriesId:season -> epoch последнего пустого поиска
    [ -f "$STATE_FILE" ] || return 0
    awk -F'\t' -v k="$1" '$1==k && $3=="empty" {print $2}' "$STATE_FILE" 2>/dev/null | tail -1
}

state_set() {   # $1 ключ, $2 result (empty|grab)
    mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
    local now; now=$(date +%s)
    [ -f "$STATE_FILE" ] && grep -v "^$1$(printf '\t')" "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null
    printf '%s\t%s\t%s\n' "$1" "$now" "$2" >> "${STATE_FILE}.tmp"
    mv -f "${STATE_FILE}.tmp" "$STATE_FILE"
}

log "=== Старт${DRY:+ (DRY=$DRY)} ==="

# --- 1. что пропущено -------------------------------------------------------
MISSING=$(api "$API_URL/wanted/missing?page=1&pageSize=2000&includeSeries=true&monitored=true")
if [ -z "$MISSING" ]; then
    log "Не удалось получить /wanted/missing — выходим."
    exit 1
fi

TOTAL=$(echo "$MISSING" | jq -r '.totalRecords // 0')
TARGETS=$(echo "$MISSING" | jq -r '.records[]? | "\(.seriesId)\t\(.seasonNumber)\t\(.series.title)"' | sort -u)
if [ -z "$TARGETS" ]; then
    log "Пропущенных серий нет."
    exit 0
fi
log "Пропущено серий: $TOTAL; пар «сериал + сезон»: $(echo "$TARGETS" | wc -l)"

# --- 2. кто уже в очереди ---------------------------------------------------
QUEUE=$(api "$API_URL/queue?page=1&pageSize=1000&includeSeries=true")
# Залипшие записи в imported/ignored/failed не должны навсегда блокировать сериал.
IN_QUEUE=$(echo "$QUEUE" | jq -r '
    .records[]?
    | select((.trackedDownloadState // "") | IN("downloading","importing","importPending","importBlocked","failedPending"))
    | .seriesId' | sort -u)

# --- 3. последовательный поиск ---------------------------------------------
SEARCHED=0
GRABS=0
while IFS=$'\t' read -r SERIES_ID SEASON TITLE; do
    [ -z "$SERIES_ID" ] && continue

    if echo "$IN_QUEUE" | grep -qx "$SERIES_ID"; then
        log "  «$TITLE» S$SEASON — сериал уже в очереди, пропуск."
        continue
    fi

    STATE_KEY="$SERIES_ID:$SEASON"
    LAST_EMPTY=$(state_get "$STATE_KEY")
    if [ -n "$LAST_EMPTY" ]; then
        AGE=$(( $(date +%s) - LAST_EMPTY ))
        if [ "$AGE" -lt $(( REPEAT_HOURS * 3600 )) ]; then
            log "  «$TITLE» S$SEASON — искали $((AGE / 60)) мин назад впустую, пропуск (REPEAT_HOURS=$REPEAT_HOURS)."
            continue
        fi
    fi

    if [ "$DRY" = "1" ]; then
        log "  [DRY] «$TITLE» S$SEASON — искал бы (seriesId=$SERIES_ID)."
        continue
    fi

    PREV_ID=$(last_grab_id "$SERIES_ID")
    log "  «$TITLE» S$SEASON — SeasonSearch…"
    CMD=$(api -X POST -H "Content-Type: application/json" \
        -d "{\"name\":\"SeasonSearch\",\"seriesId\":$SERIES_ID,\"seasonNumber\":$SEASON}" \
        "$API_URL/command")
    CMD_ID=$(echo "$CMD" | jq -r '.id // empty')
    if [ -z "$CMD_ID" ]; then
        log "    команда не создалась: $(echo "$CMD" | head -c 200)"
        continue
    fi

    WAITED=0
    STATUS=""
    while [ "$WAITED" -lt "$SEARCH_TIMEOUT" ]; do
        sleep 5
        WAITED=$((WAITED + 5))
        STATUS=$(api "$API_URL/command/$CMD_ID" | jq -r '.status // empty')
        case "$STATUS" in
            completed|failed|aborted) break ;;
        esac
    done
    SEARCHED=$((SEARCHED + 1))

    if [ "$STATUS" != "completed" ]; then
        log "    поиск не завершился штатно (статус: ${STATUS:-таймаут})"
    fi

    HIT=""
    NEW_ID=$(last_grab_id "$SERIES_ID")
    if [ -n "$NEW_ID" ] && [ "$NEW_ID" != "$PREV_ID" ]; then
        HIT=$(last_grab_hash "$SERIES_ID")
    fi
    if [ -z "$HIT" ]; then
        QUEUE=$(api "$API_URL/queue?page=1&pageSize=1000&includeSeries=true")
        HIT=$(echo "$QUEUE" | jq -r '.records[]?
            | select(.seriesId == '"$SERIES_ID"')
            | select((.trackedDownloadState // "") | IN("downloading","importing","importPending","importBlocked","failedPending"))
            | .downloadId' | head -1)
    fi
    if [ -n "$HIT" ]; then
        GRABS=$((GRABS + 1))
        state_set "$STATE_KEY" grab
        log "    захват есть (hash ${HIT:0:12}), захватов за прогон: $GRABS"
        if [ "$GRABS" -ge "$MAX_GRABS" ]; then
            log "    достигнут лимит MAX_GRABS=$MAX_GRABS — останавливаемся."
            break
        fi
    else
        state_set "$STATE_KEY" empty
        log "    ничего не взято (повторно не раньше чем через ${REPEAT_HOURS} ч)."
    fi
done <<< "$TARGETS"

log "=== Готово: поисков $SEARCHED, захватов $GRABS ==="
