#!/bin/bash

CONF_DIR="/opt/docker-compose/configs/radarr-proxy"
BUNDLE="$CONF_DIR/ca-bundle.crt"
MY_CERT="$CONF_DIR/cert.pem"
MY_KEY="$CONF_DIR/key.pem"

echo "--- Начинаю подготовку для Radarr-Proxy ---"

# 1. Генерация ключей с поддержкой нескольких доменов (SAN)
echo "[..] Генерирую "всеядный" сертификат..."
openssl req -x509 -newkey rsa:4096 -keyout "$MY_KEY" -out "$MY_CERT" -sha256 -days 3650 -nodes \
  -subj "/C=RU/ST=MSK/L=MSK/O=Proxy/CN=api.radarr.video" \
  -addext "subjectAltName = DNS:api.radarr.video, DNS:radarr.servarr.com"
chmod 644 "$MY_CERT" "$MY_KEY"

# 2. Сборка бандла
if docker exec radarr test -f /etc/ssl/certs/ca-certificates.crt; then
    docker exec radarr cat /etc/ssl/certs/ca-certificates.crt > "$BUNDLE"
    echo "" >> "$BUNDLE"
    cat "$MY_CERT" >> "$BUNDLE"
    echo "[OK] ca-bundle.crt собран."
else
    echo "[ERR] Контейнер radarr не запущен! Не могу забрать эталонный бандл."
    exit 1
fi

echo "--- Готово! ---"
