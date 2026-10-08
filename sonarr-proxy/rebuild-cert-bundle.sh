#!/bin/bash

# Пути к файлам
CONF_DIR="/opt/docker-compose/configs/sonarr-proxy"
BUNDLE="$CONF_DIR/ca-bundle.crt"
MY_CERT="$CONF_DIR/cert.pem"

echo "Начинаю сборку бандла..."

# 1. Берем чистый эталонный бандл из любого работающего контейнера (например, radarr)
if docker exec radarr test -f /etc/ssl/certs/ca-certificates.crt; then
    docker exec radarr cat /etc/ssl/certs/ca-certificates.crt > "$BUNDLE"
    echo "[OK] Системный бандл скопирован из Radarr."
else
    echo "[ERR] Не удалось найти бандл в Radarr!"
    exit 1
fi

# 2. Добавляем твой сертификат в конец бандла
if [ -f "$MY_CERT" ]; then
    echo "" >> "$BUNDLE"
    cat "$MY_CERT" >> "$BUNDLE"
    echo "" >> "$BUNDLE"
    echo "[OK] Ваш сертификат cert.pem добавлен в бандл."
else
    echo "[ERR] Файл cert.pem не найден в $CONF_DIR!"
    exit 1
fi

# 3. Проверка размера (чтобы не был пустышкой)
LINES=$(wc -l < "$BUNDLE")
echo "Итоговый бандл: $LINES строк."

echo "Готово. Теперь перезапусти контейнеры: docker compose restart sonarr prowlarr"
