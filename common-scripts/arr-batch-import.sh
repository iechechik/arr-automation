#!/bin/bash
# arr-batch-import.sh
# Ручной прогон: находит все залипшие загрузки в Sonarr и/или Radarr
# и импортирует каждую через arr-force-import.sh
#
# Использование (изнутри контейнера, ключ и порт берутся из него самого):
#   docker exec sonarr /config/scripts/arr-batch-import.sh sonarr
#   docker exec radarr /config/scripts/arr-batch-import.sh radarr
# Без аргумента target определяется по hostname контейнера.

LOG_FILE="/config/scripts/log"
FORCE_IMPORT_SCRIPT="/config/scripts/arr-force-import.sh"
CURL_TIMEOUT=30

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Определяем какие приложения обрабатывать
# ---------------------------------------------------------------------------
TARGET="${1:-auto}"

if [ "$TARGET" = "auto" ]; then
    HOSTNAME=$(hostname)
    if echo "$HOSTNAME" | grep -qi "sonarr"; then
        TARGET="sonarr"
    elif echo "$HOSTNAME" | grep -qi "radarr"; then
        TARGET="radarr"
    else
        TARGET="both"
    fi
    log "Автоопределение: target=$TARGET (hostname=$HOSTNAME)"
fi

log "========================================================"
log "=== Старт arr-batch-import.sh  target=$TARGET"
log "========================================================"

if [ ! -f "$FORCE_IMPORT_SCRIPT" ]; then
    log "ОШИБКА: скрипт $FORCE_IMPORT_SCRIPT не найден"
    exit 1
fi

# ---------------------------------------------------------------------------
# Функция: получить API-ключ
# ---------------------------------------------------------------------------
get_api_key() {
    xmlstarlet sel -t -v "//ApiKey" /config/config.xml 2>/dev/null
}

# ---------------------------------------------------------------------------
# Функция: обработать одно приложение
# ---------------------------------------------------------------------------
process_app() {
    local app_type="$1"
    local port="$2"

    local api_url="http://localhost:$port/api/v3"
    local api_key
    api_key=$(get_api_key)

    if [ -z "$api_key" ]; then
        log "[$app_type] ОШИБКА: не удалось получить API-ключ"
        return 1
    fi

    local status
    status=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w "%{http_code}" \
        -H "X-Api-Key: $api_key" \
        "$api_url/system/status")
    if [ "$status" != "200" ]; then
        log "[$app_type] ОШИБКА: API недоступен (HTTP $status) — пропускаем"
        return 1
    fi

    log "[$app_type] API доступен, ищем залипшие загрузки..."

    local queue_json
    queue_json=$(curl -s -m "$CURL_TIMEOUT" \
        -H "X-Api-Key: $api_key" \
        "$api_url/queue?pageSize=200&includeUnknownSeriesItems=true")

    local stuck_json
    stuck_json=$(echo "$queue_json" | jq '[
        .records[] |
        select(
            .trackedDownloadStatus == "warning" or
            .status == "warning" or
            (.statusMessages[]?.messages[]? | test("Unable to parse|Invalid season|Manual"))
        ) |
        {
            title: .title,
            downloadId: .downloadId,
            seriesId: (.seriesId // .movieId),
            entityTitle: (.series.title // .movie.title // .title)
        }
    ] | unique_by(.downloadId)')

    local total
    total=$(echo "$stuck_json" | jq '. | length')
    log "[$app_type] Найдено залипших: $total"

    if [ "$total" -eq 0 ]; then
        log "[$app_type] Нечего импортировать — всё чисто"
        return 0
    fi

    log "[$app_type] Список залипших:"
    echo "$stuck_json" | jq -r '.[] | "  - \(.entityTitle) | downloadId=\(.downloadId)"' | \
        while IFS= read -r line; do log "[$app_type] $line"; done

    local processed=0
    local failed=0

    local tmpfile
    tmpfile=$(mktemp)
    echo "$stuck_json" | jq -c '.[]' > "$tmpfile"

    while IFS= read -r item; do
        local download_id entity_title series_id
        download_id=$(echo "$item"  | jq -r '.downloadId')
        entity_title=$(echo "$item" | jq -r '.entityTitle')
        series_id=$(echo "$item"    | jq -r '.seriesId')

        log "[$app_type] ---"
        log "[$app_type] Обрабатываем: «$entity_title»"
        log "[$app_type]   downloadId=$download_id  id=$series_id"

        if [ -z "$download_id" ] || [ "$download_id" = "null" ]; then
            log "[$app_type]   ПРОПУСК: нет downloadId"
            failed=$((failed + 1))
            continue
        fi

        # Передаём переменные через env — гарантированно попадают в дочерний процесс
        if [ "$app_type" = "sonarr" ]; then
            env \
                sonarr_eventtype="ManualInteractionRequired" \
                sonarr_series_id="$series_id" \
                sonarr_series_title="$entity_title" \
                sonarr_download_id="$download_id" \
                bash "$FORCE_IMPORT_SCRIPT"
        else
            env \
                radarr_eventtype="ManualInteractionRequired" \
                radarr_movie_id="$series_id" \
                radarr_movie_title="$entity_title" \
                radarr_download_id="$download_id" \
                bash "$FORCE_IMPORT_SCRIPT"
        fi

        local exit_code=$?
        if [ $exit_code -eq 0 ]; then
            processed=$((processed + 1))
        else
            log "[$app_type]   ОШИБКА: скрипт завершился с кодом $exit_code"
            failed=$((failed + 1))
        fi

    done < "$tmpfile"
    rm -f "$tmpfile"

    log "[$app_type] Итого: обработано=$processed ошибок=$failed"

    [ "$failed" -eq 0 ] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Запускаем для нужных приложений
# ---------------------------------------------------------------------------
RC=0
case "$TARGET" in
    sonarr)
        process_app "sonarr" 8989 || RC=1
        ;;
    radarr)
        process_app "radarr" 7878 || RC=1
        ;;
    both)
        # ВНИМАНИЕ: изнутри контейнера both работать не может — API-ключ читается из
        # /config/config.xml того контейнера, где запущен скрипт, и порты 7878/8989
        # берутся из его же localhost. Запускайте по одному или с хоста.
        log "ВНИМАНИЕ: target=both имеет смысл только при запуске на хосте"
        process_app "sonarr" 8989 || RC=1
        process_app "radarr" 7878 || RC=1
        ;;
    *)
        log "ОШИБКА: неизвестный target «$TARGET». Используй: sonarr | radarr | both"
        exit 1
        ;;
esac

log "========================================================"
log "=== arr-batch-import.sh завершён (код $RC)"
log "========================================================"
exit $RC
