#!/bin/bash
# media-import.sh — ручной прогон: импорт залипших в Sonarr и Radarr + пересканирование Jellyfin.
#
# Запускать на хосте (нужен доступ к docker), от root:
#   /opt/docker-compose/common-scripts/media-import.sh
#
# Токен Jellyfin: подставьте свой (или экспортируйте JELLYFIN_TOKEN в окружение).
JELLYFIN_TOKEN="${JELLYFIN_TOKEN:-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx}"
JELLYFIN_URL="${JELLYFIN_URL:-http://misc.local:9096}"
CURL_TIMEOUT=30

RC=0

# 1. Импорт в Sonarr
docker exec sonarr /config/scripts/arr-batch-import.sh sonarr || RC=1

# 2. Импорт в Radarr
docker exec radarr /config/scripts/arr-batch-import.sh radarr || RC=1

# 3. Пересканирование Jellyfin
echo "Запускаем пересканирование Jellyfin..."
curl -s -m "$CURL_TIMEOUT" -o /dev/null -w "Jellyfin: HTTP %{http_code}\n" \
    -X POST "$JELLYFIN_URL/Library/Refresh" \
    -H "X-Emby-Token: $JELLYFIN_TOKEN" || RC=1

exit $RC
