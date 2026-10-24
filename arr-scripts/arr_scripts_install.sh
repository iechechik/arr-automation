#!/bin/bash
# =============================================================================
# arr_scripts_install.sh — приведение проекта к «пакетному» виду (идемпотентно)
# =============================================================================
# Что делает (можно запускать сколько угодно раз):
#   1. создаёт /opt/docker-compose/arr-scripts и кладёт туда рабочие скрипты;
#   2. переключает монтирование в docker-compose.yaml: common-scripts -> arr-scripts
#      (с бэкапом compose в /root/backups/compose/), при изменении пересоздаёт
#      контейнеры sonarr и radarr;
#   3. проверяет уведомление CustomScript:
#        Sonarr — On Grab + On Manual Interaction Required;
#        Radarr — только On Manual Interaction Required;
#      отсутствующее создаёт, существующее правит;
#   4. ставит в Sonarr клиенту qBittorrent «Initial State = Stop»;
#   5. ставит systemd-таймер автодокачки (enable --now).
#
# Запуск: sudo bash /opt/docker-compose/common-scripts/arr_scripts_install.sh
# =============================================================================
set -euo pipefail

BASE=/opt/docker-compose
SRC="$BASE/common-scripts"
DST="$BASE/arr-scripts"
COMPOSE="$BASE/docker-compose.yaml"
SCRIPT_PATH="/config/scripts/arr_events.sh"
SONARR_API="http://127.0.0.1:9095/api/v3"
RADARR_API="http://127.0.0.1:9094/api/v3"
UNIT_DIR=/etc/systemd/system

say() { echo "[install] $*"; }
die() { echo "[install] ОШИБКА: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запускать от root: sudo bash $0"

# ---------------------------------------------------------------- 1. каталог
say "каталог $DST"
mkdir -p "$DST/logs"

for f in arr_events.sh sonarr_tracking_series.sh sonarr_tracking_series.service \
         sonarr_tracking_series.timer arr_scripts_install.sh README.md; do
    [ -f "$SRC/$f" ] || die "нет файла $SRC/$f"
done

install -m 755 "$SRC/arr_events.sh"                "$DST/arr_events.sh"
install -m 755 "$SRC/sonarr_tracking_series.sh"    "$DST/sonarr_tracking_series.sh"
install -m 644 "$SRC/sonarr_tracking_series.service" "$DST/sonarr_tracking_series.service"
install -m 644 "$SRC/sonarr_tracking_series.timer"   "$DST/sonarr_tracking_series.timer"
install -m 644 "$SRC/README.md"                    "$DST/README.md"
install -m 755 "$SRC/arr_scripts_install.sh"        "$DST/arr_scripts_install.sh"

for f in arr_events.sh sonarr_tracking_series.sh; do
    bash -n "$DST/$f" || die "синтаксис $f"
done
say "скрипты на месте, синтаксис ок"

# ---------------------------------------------------------------- 2. compose
CHANGED=0
if grep -q "${BASE}/common-scripts:/config/scripts" "$COMPOSE"; then
    mkdir -p /root/backups/compose
    BAK="/root/backups/compose/docker-compose.yaml.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$COMPOSE" "$BAK"
    say "бэкап compose: $BAK"
    sed -i "s#${BASE}/common-scripts:/config/scripts#${DST}:/config/scripts#g" "$COMPOSE"
    CHANGED=1
    say "монтирование переключено на $DST"
else
    say "монтирование уже указывает на $DST (или отсутствует) — пропуск"
fi

if [ "$CHANGED" = "1" ]; then
    say "пересоздаю контейнеры sonarr и radarr"
    ( cd "$BASE" && docker compose up -d sonarr radarr )
fi

# ---------------------------------------------------------------- 3. ключи API
KEY_SONARR=$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$BASE/configs/sonarr/config.xml" | head -1)
KEY_RADARR=$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$BASE/configs/radarr/config.xml" | head -1)
[ -n "$KEY_SONARR" ] || die "не прочитал ключ Sonarr"
[ -n "$KEY_RADARR" ] || die "не прочитал ключ Radarr"

wait_api() {  # $1 api, $2 key
    local i
    for i in $(seq 1 90); do
        if curl -s -m 5 -H "X-Api-Key: $2" "$1/system/status" | grep -q '"appName"'; then
            return 0
        fi
        sleep 2
    done
    return 1
}
say "жду готовности API"
wait_api "$SONARR_API" "$KEY_SONARR" || die "Sonarr API не отвечает"
wait_api "$RADARR_API" "$KEY_RADARR" || die "Radarr API не отвечает"

# ---------------------------------------------------------------- 4. уведомления
# ensure_notification <api> <key> <имя> <onGrab: true|false>
ensure_notification() {
    local api="$1" key="$2" name="$3" grab="$4"
    local id payload resp code

    id=$(curl -s -m 15 -H "X-Api-Key: $key" "$api/notification" \
        | jq -r --arg n "$name" '.[] | select(.name==$n and .implementation=="CustomScript") | .id' | head -1)

    if [ -n "$id" ]; then
        payload=$(curl -s -m 15 -H "X-Api-Key: $key" "$api/notification/$id" | jq \
            --argjson grab "$grab" --arg path "$SCRIPT_PATH" '
            .onGrab = $grab
            | .onManualInteractionRequired = true
            | .fields = ((.fields // []) | map(if .name == "path" then .value = $path else . end))
            | if ([.fields[].name] | index("arguments")) == null
              then .fields += [{"name":"arguments","value":""}] else . end')
        code=$(curl -s -o /tmp/notif.out -w '%{http_code}' -X PUT -H "X-Api-Key: $key" \
            -H "Content-Type: application/json" -d "$payload" "$api/notification/$id")
        [ "$code" = "202" ] || [ "$code" = "200" ] || die "не смог обновить уведомление «$name» (HTTP $code): $(head -c 300 /tmp/notif.out)"
        say "уведомление «$name» обновлено (onGrab=$grab, onManual=true)"
    else
        payload=$(jq -n --arg name "$name" --arg path "$SCRIPT_PATH" --argjson grab "$grab" '{
            name: $name,
            implementation: "CustomScript",
            configContract: "CustomScriptSettings",
            onGrab: $grab,
            onManualInteractionRequired: true,
            fields: [
                {name: "path", value: $path},
                {name: "arguments", value: ""}
            ]
        }')
        code=$(curl -s -o /tmp/notif.out -w '%{http_code}' -X POST -H "X-Api-Key: $key" \
            -H "Content-Type: application/json" -d "$payload" "$api/notification")
        [ "$code" = "201" ] || [ "$code" = "200" ] || die "не смог создать уведомление «$name» (HTTP $code): $(head -c 300 /tmp/notif.out)"
        say "уведомление «$name» создано (onGrab=$grab, onManual=true)"
    fi
}

ensure_notification "$SONARR_API" "$KEY_SONARR" "events:grab,manual" true
ensure_notification "$RADARR_API" "$KEY_RADARR" "events:manual_import" false

# ---------------------------------------------------------------- 5. qBittorrent: Initial State = Stop
QB_ID=$(curl -s -m 15 -H "X-Api-Key: $KEY_SONARR" "$SONARR_API/downloadclient" \
    | jq -r '.[] | select(.implementation=="QBittorrent") | .id' | head -1)
if [ -z "$QB_ID" ]; then
    say "ВНИМАНИЕ: клиент qBittorrent в Sonarr не найден — Initial State не выставлен"
else
    CUR=$(curl -s -m 15 -H "X-Api-Key: $KEY_SONARR" "$SONARR_API/downloadclient/$QB_ID" \
        | jq -r '(.fields[] | select(.name=="initialState") | .value) // empty')
    if [ "$CUR" = "2" ]; then
        say "qBittorrent: Initial State уже Stop"
    else
        PAYLOAD=$(curl -s -m 15 -H "X-Api-Key: $KEY_SONARR" "$SONARR_API/downloadclient/$QB_ID" \
            | jq '(.fields) |= map(if .name == "initialState" then .value = 2 else . end)')
        CODE=$(curl -s -o /tmp/qb.out -w '%{http_code}' -X PUT -H "X-Api-Key: $KEY_SONARR" \
            -H "Content-Type: application/json" -d "$PAYLOAD" "$SONARR_API/downloadclient/$QB_ID")
        [ "$CODE" = "202" ] || [ "$CODE" = "200" ] || die "не смог выставить Initial State (HTTP $CODE): $(head -c 300 /tmp/qb.out)"
        say "qBittorrent: Initial State -> Stop (было $CUR)"
    fi
fi

# ---------------------------------------------------------------- 6. systemd
say "ставлю таймер автодокачки"
install -m 644 "$DST/sonarr_tracking_series.service" "$UNIT_DIR/sonarr_tracking_series.service"
install -m 644 "$DST/sonarr_tracking_series.timer"   "$UNIT_DIR/sonarr_tracking_series.timer"
systemctl daemon-reload
systemctl enable --now sonarr_tracking_series.timer >/dev/null
say "таймер включён"

# ---------------------------------------------------------------- 7. итог
echo
say "готово. Состояние:"
systemctl list-timers sonarr_tracking_series.timer --no-pager || true
echo
say "файлы:"
ls -1 "$DST" "$DST/logs" 2>/dev/null | sed 's/^/    /'
echo
say "что осталось на тебе:"
echo "    - старый каталог $SRC больше не смонтирован, можно удалить;"
echo "    - блок qbit-proxy в $COMPOSE можно вычистить (сервис под профилем manual)."
