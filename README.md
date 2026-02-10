# autoJP/n8n

Набор воспроизводимых n8n workflow (JSON) + Python CLI-утилит для пайплайна:
subdomains → targets → nmap → Acunetix.

## Состав репозитория

### Workflows (n8n exports)
- `WF_A_Subdomains_PT.json` — получение/нормализация сабдоменов для Product Type.
- `WF_C_Targets_For_PT.json` — сбор/дедупликация targets для дальнейшего сканирования.
- `WF_B_Nmap_Product.json` — обработка Nmap-результатов и подготовка данных для сканеров.

### Python утилиты
- `enum_subs_auto.py` — enumeration сабдоменов (assetfinder/sublist3r), JSON-результат.
- `process_nmap_ips_for_pt.py` — разбор Nmap XML, создание IP:port products и обновление description PT в DefectDojo.
- `acunetix_sync_pt.py` — синхронизация targets из DefectDojo PT в Acunetix group.
- `acunetix_set_group_scan_speed.py` — установка `scan_speed` для targets в Acunetix group.

## Порядок запуска (базовый)
1. Импортировать workflows: `WF_A` → `WF_C` → `WF_B`.
2. Настроить credentials/ENV в n8n (см. `.env.example`).
3. Запустить `WF_A` (сабдомены), затем `WF_C` (targets), затем `WF_B` (Nmap processing).
4. При необходимости синхронизировать в Acunetix через Python-скрипты.

## Переменные окружения
См. `.env.example` — только шаблоны, без секретов.

## Унифицированный интерфейс Python CLI (P0)
Общее:
- JSON в `stdout`.
- `--output <file>` — опциональная запись того же JSON в файл.
- `--timeout` — таймаут сети/источников.
- Предсказуемые exit codes: `0` (ok), `1` (runtime/API), `2` (invalid args/input).

### 1) enum_subs_auto.py
```bash
python3 enum_subs_auto.py --domain example.com --timeout 120 --output /tmp/subs.json
```

### 2) process_nmap_ips_for_pt.py
```bash
python3 process_nmap_ips_for_pt.py \
  --base-url "$DOJO_BASE_URL" \
  --token "$DOJO_API_TOKEN" \
  --product-type-id 123 \
  --input /tmp \
  --timeout 30 \
  --output /tmp/nmap_result.json
```

### 3) acunetix_sync_pt.py
```bash
python3 acunetix_sync_pt.py \
  --base-url "$DOJO_BASE_URL" \
  --token "$DOJO_API_TOKEN" \
  --product-type-id 123 \
  --acu-base-url "$ACU_BASE_URL" \
  --acu-token "$ACU_API_TOKEN" \
  --timeout 30 \
  --output /tmp/acu_sync.json
```

### 4) acunetix_set_group_scan_speed.py
```bash
python3 acunetix_set_group_scan_speed.py \
  --base-url "$ACU_BASE_URL" \
  --token "$ACU_API_TOKEN" \
  --group-id <group_uuid> \
  --scan-speed sequential \
  --timeout 30 \
  --output /tmp/scan_speed.json
```

## Sanity checks, добавленные в P0
- Валидация обязательных токенов/URL.
- Проверка корректности `--timeout`.
- Проверка входного каталога Nmap XML (`--input`) в `process_nmap_ips_for_pt.py`.
- Защита от пустых списков целей (возврат структурированного JSON, без падения).

## Что было / что стало / как проверить
- Было: разные параметры CLI (`--api-token`, `--acu-api-token`, и т.д.), частично неунифицированный вывод.
- Стало: единый стиль (`--base-url`, `--token`, `--input`, `--output`, `--timeout`) + JSON stdout во всех утилитах.
- Проверка: `python3 <script> --help` и dry-run/invalid-input кейсы (см. ниже в отчёте).

## TODO
- Добавить smoke-тесты парсеров (P2).
- Добавить `pyproject.toml` с `black`/`ruff` и CI lint/check (P1/P2).
