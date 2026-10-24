# arr-scripts

Скрипты автоматизации Sonarr / Radarr. Каталог целиком монтируется в оба
контейнера как `/config/scripts`.

## Состав

| Файл | Что делает |
|---|---|
| `arr_events.sh` | Реакция на события Sonarr/Radarr. **Grab**: закрывает залипшие записи очереди, переименовывает торрент под фактически запрошенные серии (`S6E5`, `S6E5E6`), ставит `filePrio=0` лишним файлам, запускает торрент; если запустить не удалось — снимает захват из очереди. **Manual Interaction Required**: импортирует файлы вручную, а при «already imported» сносит фантомные записи этого хэша. |
| `sonarr_tracking_series.sh` | Автодокачка: `wanted/missing` → пары «сериал + сезон» → `SeasonSearch` по каждой, последовательно, не больше `MAX_GRABS` захватов за прогон. |
| `sonarr_tracking_series.service` | systemd unit (oneshot), запускает скрипт внутри контейнера Sonarr. |
| `sonarr_tracking_series.timer` | Каждые 10 минут **после завершения** предыдущего прогона (`OnUnitInactiveSec`). |
| `arr_scripts_install.sh` | Идемпотентная установка: раскладывает файлы, переключает монтирование, правит уведомления и Initial State, включает таймер. |
| `logs/` | Логи и журнал захватов (создаётся автоматически). |

## Логи

```
logs/arr_events.log                  события Grab / Manual
logs/grabs.tsv                       журнал захватов: дата, хэш, сезон/серии
logs/sonarr_tracking_series.log      работа автодокачки
```

Ротация у обоих скриптов своя: при размере больше 1 МиБ файл уезжает в `*.1`
(одна архивная копия). Замок автодокачки живёт в `/tmp` контейнера.

## Переменные

`arr_events.sh`: `LOG_FILE`, `GRAB_HISTORY`, `FILES_WAIT` (по умолчанию 30 с),
`QBIT_URL` / `QBIT_API_KEY` (или `QBIT_USER` + `QBIT_PASS`).

`sonarr_tracking_series.sh`: `DRY=1` — только показать цели; `MAX_GRABS` (1),
`SEARCH_TIMEOUT` (300 с), `LOG_FILE`, `API_URL`, `API_KEY`.

## Установка / проверка

```bash
sudo bash /opt/docker-compose/arr-scripts/arr_scripts_install.sh   # установить или починить
systemctl list-timers sonarr_tracking_series.timer --no-pager
sudo systemctl start sonarr_tracking_series.service           # прогон сейчас
sudo docker exec sonarr env DRY=1 /config/scripts/sonarr_tracking_series.sh
tail -f /opt/docker-compose/arr-scripts/logs/sonarr_tracking_series.log
```

## Как это связано с prowlarr-proxy

Сезонный поиск Sonarr отдаёт пак целиком (`S6E1-6`), а в библиотеке часть серий
уже есть. `prowlarr-proxy` переписывает заголовок пака под недостающие серии
(`S6E5E6`) и пересчитывает размер, поэтому Sonarr берёт пак и ждёт импорт только
нужных серий. `arr_events.sh` доводит это до клиента: переименовывает торрент
(по имени Sonarr решает, сколько серий ждать) и выключает лишние файлы.

## Удаление

```bash
sudo systemctl disable --now sonarr_tracking_series.timer
sudo rm /etc/systemd/system/sonarr_tracking_series.{service,timer}
sudo systemctl daemon-reload
```
