#!/bin/bash
# arr-force-import.sh
# Хук Sonarr/Radarr на событие ManualInteractionRequired: забирает файлы залипшей
# загрузки через manualimport и принудительно импортирует их (importMode=move).
#
# Куда воткнуть: Settings -> Connect -> Custom Script, событие
# «On Manual Interaction Required», путь /config/scripts/arr-force-import.sh
#
# Совместимость: BusyBox sed (Alpine) — без {1,3}, без кириллицы в regex классах.

LOG_FILE="/config/scripts/log"
LOG_MAX_BYTES=1048576          # 1 МиБ: лог общий для sonarr и radarr, не даём ему расти вечно
CURL_TIMEOUT=30

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] $*" | tee -a "$LOG_FILE"
}

# Обрезаем разросшийся лог (одна страховка на запуск, не на каждую строку).
if [ -f "$LOG_FILE" ]; then
    SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt "$LOG_MAX_BYTES" ]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1"
        log "Лог превысил $LOG_MAX_BYTES байт — предыдущий убран в $LOG_FILE.1"
    fi
fi

log "=== Старт arr-force-import.sh ==="

# --- Определяем приложение ---
# ВНИМАНИЕ: SERIES_ID хранит id объекта конкретного приложения — у Radarr это movieId.
if [ -n "$sonarr_eventtype" ]; then
    APP_TYPE="Sonarr"
    EVENT_TYPE="$sonarr_eventtype"
    SERIES_ID="$sonarr_series_id"
    DOWNLOAD_ID="$sonarr_download_id"
    TITLE="$sonarr_series_title"
    PORT=8989
elif [ -n "$radarr_eventtype" ]; then
    APP_TYPE="Radarr"
    EVENT_TYPE="$radarr_eventtype"
    SERIES_ID="$radarr_movie_id"
    DOWNLOAD_ID="$radarr_download_id"
    TITLE="$radarr_movie_title"
    PORT=7878
else
    log "ОШИБКА: не удалось определить тип приложения (нет sonarr_/radarr_ переменных)"
    exit 1
fi

log "Приложение: $APP_TYPE, Событие: $EVENT_TYPE"

# --- Тест ---
if [ "$EVENT_TYPE" = "Test" ]; then
    log "Тестовый запуск — выходим успешно"
    exit 0
fi

# --- Только ManualInteractionRequired ---
if [ "$EVENT_TYPE" != "ManualInteractionRequired" ]; then
    log "Событие «$EVENT_TYPE» — не наше, выходим"
    exit 0
fi

log "Объект: «$TITLE» (id=$SERIES_ID)"
log "Download ID: $DOWNLOAD_ID"

if [ -z "$SERIES_ID" ] || [ -z "$DOWNLOAD_ID" ]; then
    log "ОШИБКА: нет ID объекта или downloadId — выходим"
    exit 1
fi

# --- API ---
API_URL="http://localhost:$PORT/api/v3"
API_KEY=$(xmlstarlet sel -t -v "//ApiKey" /config/config.xml)
if [ -z "$API_KEY" ]; then
    log "ОШИБКА: не удалось получить API-ключ"
    exit 1
fi

# --- Список файлов из manualimport ---
log "Запрашиваем manualimport для downloadId=$DOWNLOAD_ID..."
FILES_JSON=$(curl -s -m "$CURL_TIMEOUT" \
    -H "X-Api-Key: $API_KEY" \
    "$API_URL/manualimport?downloadId=$DOWNLOAD_ID&filterExistingFiles=true")

FILES_COUNT=$(echo "$FILES_JSON" | jq '. | length')
log "Файлов в очереди: $FILES_COUNT"

if [ "$FILES_COUNT" -eq 0 ] || [ "$FILES_COUNT" = "null" ]; then
    log "Нет файлов для импорта (уже импортированы или удалены)"
    exit 0
fi

# --- Для Sonarr загружаем эпизоды ---
if [ "$APP_TYPE" = "Sonarr" ]; then
    log "Загружаем эпизоды сериала $SERIES_ID..."
    EPISODES_JSON=$(curl -s -m "$CURL_TIMEOUT" \
        -H "X-Api-Key: $API_KEY" \
        "$API_URL/episode?seriesId=$SERIES_ID")
    EP_COUNT=$(echo "$EPISODES_JSON" | jq '. | length')
    log "Эпизодов в базе: $EP_COUNT"
fi

# ---------------------------------------------------------------------------
# Функция: номер эпизода из имени файла
# Порядок паттернов от специфичных к общим — ВАЖЕН.
# ---------------------------------------------------------------------------
extract_episode_number() {
    local name="$1"
    local num

    # Паттерн 1: цифры в НАЧАЛЕ строки до точки или пробела
    # "01. Кафе Куба.2023..." → 1
    # "7 серия название"     → 7
    num=$(echo "$name" | sed -nE 's/^([0-9]+)[. ].*/\1/p')
    [ -n "$num" ] && echo "$((10#$num))" && return

    # Паттерн 2: S01E05 / s01e05
    num=$(echo "$name" | sed -nE 's/.*[Ss][0-9]+[Ee]([0-9]+).*/\1/p')
    [ -n "$num" ] && echo "$((10#$num))" && return

    # Паттерн 3: 1x05
    num=$(echo "$name" | sed -nE 's/.*[0-9]+[Xx]([0-9]+).*/\1/p')
    [ -n "$num" ] && echo "$((10#$num))" && return

    # Паттерн 4: "01 серия" / "серия 01"
    # кириллицу ищем через grep, цифры через grep -oE
    if echo "$name" | grep -qi "сери"; then
        num=$(echo "$name" | grep -oE '[0-9]+' | head -1)
        [ -n "$num" ] && echo "$((10#$num))" && return
    fi

    # Паттерн 5: "Название 8.WEB-DLRip" — цифра перед точкой
    num=$(echo "$name" | sed -E 's/.*[^0-9]([0-9]+)\..*/\1/')
    if [ -n "$num" ] && [ "$num" != "$name" ]; then
        echo "$((10#$num))" && return
    fi

    # Паттерн 6: крайний fallback — последняя группа цифр в строке
    num=$(echo "$name" | grep -oE '[0-9]+' | tail -1)
    [ -n "$num" ] && echo "$((10#$num))" && return

    echo ""
}

# ---------------------------------------------------------------------------
# Функция: номер сезона из имени файла (по умолчанию 1)
# ---------------------------------------------------------------------------
extract_season_number() {
    local name="$1"
    local num

    # S02E05
    num=$(echo "$name" | sed -nE 's/.*[Ss]([0-9]+)[Ee][0-9]+.*/\1/p')
    [ -n "$num" ] && echo "$((10#$num))" && return

    # 2x05
    num=$(echo "$name" | sed -nE 's/.*([0-9]+)[Xx][0-9]+.*/\1/p')
    [ -n "$num" ] && echo "$((10#$num))" && return

    echo "1"
}

# ---------------------------------------------------------------------------
# Обрабатываем файлы
# Используем tmpfile чтобы счётчики SUCCESS/FAIL работали (избегаем subshell)
# ---------------------------------------------------------------------------
TMPFILE=$(mktemp)
echo "$FILES_JSON" | jq -c '.[]' > "$TMPFILE"

SUCCESS=0
FAIL=0

while IFS= read -r file_item; do
    FILE_PATH=$(echo "$file_item" | jq -r '.path')
    FILE_NAME=$(echo "$file_item" | jq -r '.name')
    REJECTION=$(echo "$file_item" | jq -r '.rejections[0].reason // "none"')

    log "---"
    log "Файл    : $FILE_NAME"
    log "Путь    : $FILE_PATH"
    log "Отказ   : $REJECTION"

    # Качество и язык берём из того что уже распознал arr
    QUALITY_ID=$(echo "$file_item"    | jq '.quality.quality.id // 1')
    QUALITY_REV=$(echo "$file_item"   | jq '.quality.revision // {"version":1,"real":0,"isRepack":false}')
    LANG_ID=$(echo "$file_item"       | jq '.languages[0].id // 1')
    RELEASE_GROUP=$(echo "$file_item" | jq -r '.releaseGroup // "Unknown"')

    # --- Sonarr: находим episodeId ---
    if [ "$APP_TYPE" = "Sonarr" ]; then
        EP_NUM=$(extract_episode_number "$FILE_NAME")
        SEASON_NUM=$(extract_season_number "$FILE_NAME")

        log "Определено: сезон=$SEASON_NUM эпизод=$EP_NUM"

        if [ -z "$EP_NUM" ]; then
            log "ПРОПУСК: не удалось извлечь номер эпизода из имени «$FILE_NAME»"
            FAIL=$((FAIL + 1))
            continue
        fi

        # head -1: страховка от мульти-эпизодных файлов, иначе в тело уйдёт
        # многострочное значение и запрос сломается
        EPISODE_ID=$(echo "$EPISODES_JSON" | jq \
            --argjson ep "$EP_NUM" \
            --argjson sn "$SEASON_NUM" \
            '.[] | select(.seasonNumber==$sn and .episodeNumber==$ep) | .id' | head -1)

        if [ -z "$EPISODE_ID" ] || [ "$EPISODE_ID" = "null" ]; then
            log "ПРОПУСК: эпизод S${SEASON_NUM}E${EP_NUM} не найден в базе Sonarr"
            FAIL=$((FAIL + 1))
            continue
        fi

        log "Найден episodeId=$EPISODE_ID (S${SEASON_NUM}E${EP_NUM})"

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
            '{
                name: "ManualImport",
                files: [{
                    path:         $path,
                    seriesId:     $seriesId,
                    seasonNumber: $seasonNumber,
                    episodeIds:   $episodeIds,
                    quality:      { quality: { id: $qualityId }, revision: $qualityRev },
                    languages:    [{ id: $langId }],
                    releaseGroup: $releaseGroup,
                    downloadId:   $downloadId
                }],
                importMode: "move"
            }')

    # --- Radarr: episodeIds и seasonNumber не нужны ---
    else
        IMPORT_BODY=$(jq -n \
            --arg     path         "$FILE_PATH" \
            --argjson movieId      "$SERIES_ID" \
            --argjson qualityId    "$QUALITY_ID" \
            --argjson qualityRev   "$QUALITY_REV" \
            --argjson langId       "$LANG_ID" \
            --arg     releaseGroup "$RELEASE_GROUP" \
            --arg     downloadId   "$DOWNLOAD_ID" \
            '{
                name: "ManualImport",
                files: [{
                    path:         $path,
                    movieId:      $movieId,
                    quality:      { quality: { id: $qualityId }, revision: $qualityRev },
                    languages:    [{ id: $langId }],
                    releaseGroup: $releaseGroup,
                    downloadId:   $downloadId
                }],
                importMode: "move"
            }')
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
    else
        log "ОШИБКА (HTTP $HTTP_CODE): $RESP_BODY"
        FAIL=$((FAIL + 1))
    fi

done < "$TMPFILE"

rm -f "$TMPFILE"

log "=== Готово. Успешно: $SUCCESS, ошибок: $FAIL ==="

# Ненулевой код возврата, если хоть что-то не импортировалось: иначе в Sonarr/Radarr
# хук всегда выглядит успешным, а arr-batch-import.sh рапортует «ошибок 0».
[ "$FAIL" -eq 0 ] || exit 1
exit 0
