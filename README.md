# autoJP/n8n perimeter automation

Набор workflow/скриптов для автоматизации пайплайна:

1. сбор поверхности (`WF_A_Subdomains_PT`),
2. импорт Nmap в продукты (`WF_B_Nmap_Product`),
3. подготовка целей для Acunetix (`WF_C_Targets_For_PT`),
4. запуск сканов + импорт результатов в DefectDojo (`WF_D_AcunetixScan_PT`),
5. health-check (`WF_E_HealthCheck`),
6. оркестрация (`WF_Master_Orchestrator`).

> Все токены и URL выносятся в ENV/Credentials n8n. Секреты в JSON-export не хранятся.

## Быстрый старт

1. Заполните `.env` по образцу `.env.example`.
2. Убедитесь, что Python-скрипты доступны в рабочей директории n8n (или скорректируйте `Execute Command`).
3. Импортируйте JSON-файлы workflow в n8n.
4. Запустите `WF_E_HealthCheck`.
5. Запустите `WF_Master_Orchestrator` вручную или по расписанию.

## Переменные окружения

См. `.env.example`:

- `DOJO_BASE_URL`, `DOJO_API_TOKEN`
- `ACUNETIX_BASE_URL`, `ACUNETIX_API_TOKEN`
- `ACUNETIX_SCAN_PROFILE_ID`
- `ACUNETIX_POLL_INTERVAL`, `ACUNETIX_SCAN_TIMEOUT_SECONDS`
- `N8N_HEALTHCHECK_URL`
- `DEFAULT_PRODUCT_TYPE_ID`

## Workflow A/B (существующие)

- `WF_A_Subdomains_PT.json` — инвентаризация сабдоменов и создание продуктов.
- `WF_B_Nmap_Product.json` — запуск Nmap и импорт результатов в DefectDojo.

## Workflow C: `WF_C_Targets_For_PT`

**Назначение:** построение/нормализация `host:port` целей из endpoints продуктов PT, создание недостающих internet_accessible продуктов, синхронизация с Acunetix.

**Входы**

- `product_type_id` (input или `DEFAULT_PRODUCT_TYPE_ID`)
- `dry_run` (опционально)
- ENV: `DOJO_BASE_URL`, `DOJO_API_TOKEN`, `ACUNETIX_BASE_URL`, `ACUNETIX_API_TOKEN`

**Шаги**

1. Забирает PT и все продукты PT из DefectDojo.
2. Для каждого продукта забирает endpoints (`host`/`port`).
3. Нормализует в `host:port`, удаляет дубликаты.
4. Создает отсутствующие продукты (`name=host:port`, `internet_accessible=true`).
5. Собирает все internet_accessible продукты PT в URL-цели (HTTP/HTTPS эвристика по порту).
6. Идемпотентно синхронизирует цели в Acunetix target group (по имени PT).
7. Возвращает JSON-отчет.

**Успешный результат (пример)**

```json
{
  "ok": true,
  "product_type_id": 6,
  "products_created": 3,
  "targets_total": 25,
  "group_id": "..."
}
```

**Ошибочный результат (пример)**

```json
{
  "ok": false,
  "error": "401 Client Error: Unauthorized"
}
```

## Workflow D: `WF_D_AcunetixScan_PT`

**Назначение:** запуск сканов Acunetix для internet_accessible целей PT, polling завершения, экспорт результата, импорт в DefectDojo.

**Входы**

- `product_type_id`
- `poll_interval`, `timeout_seconds`
- `dry_run`
- ENV: `DOJO_BASE_URL`, `DOJO_API_TOKEN`, `ACUNETIX_BASE_URL`, `ACUNETIX_API_TOKEN`, `ACUNETIX_SCAN_PROFILE_ID`

**Шаги**

1. Собирает internet_accessible продукты PT.
2. Маппит продукт в target Acunetix по URL.
3. Стартует scan per target.
4. Polling до `completed/failed/timeout`.
5. Пытается скачать отчет и импортировать в DefectDojo (`import-scan`, scan type `Acunetix Scan`).
6. Возвращает список сканов и статусы.

**Успешный результат (пример)**

```json
{
  "ok": true,
  "failed_count": 0,
  "scans": [
    {"product_id": 101, "scan_id": "...", "status": "completed"}
  ]
}
```

**Ошибочный результат (пример)**

```json
{
  "ok": false,
  "failed_count": 2,
  "scans": [
    {"product_id": 101, "status": "timeout"},
    {"product_id": 102, "status": "failed"}
  ]
}
```

## Workflow E: `WF_E_HealthCheck`

**Назначение:** проверка доступности DefectDojo API, Acunetix API и n8n health endpoint.

**Входы**

- ENV: `DOJO_BASE_URL`, `DOJO_API_TOKEN`, `ACUNETIX_BASE_URL`, `ACUNETIX_API_TOKEN`, `N8N_HEALTHCHECK_URL`

**Шаги**

1. Выполняет HTTP-check каждого сервиса.
2. Агрегирует статусы.
3. При любом падении кидает явную ошибку `Health check failed for: ...`.

## Workflow Master: `WF_Master_Orchestrator`

**Назначение:** последовательный запуск пайплайна.

**Порядок:**

`WF_E_HealthCheck -> WF_A_Subdomains_PT -> WF_B_Nmap_Product -> WF_C_Targets_For_PT -> WF_D_AcunetixScan_PT`

Триггеры:

- расписание (каждые 6 часов),
- ручной запуск.

## CLI-скрипты

### `sync_targets_for_pt.py`

```bash
python3 ./sync_targets_for_pt.py --help
```

- Exit code `0`: успех
- Exit code `1`: ошибка/неуспех

### `acunetix_scan_import_pt.py`

```bash
python3 ./acunetix_scan_import_pt.py --help
```

- Exit code `0`: все сканы завершены без failed/timeout
- Exit code `2`: есть failed/timeout
- Exit code `1`: runtime ошибка

## Caveats / TODO

- TODO: endpoint и формат выгрузки отчета Acunetix может отличаться между версиями. При несовместимости скорректируйте `download_report()` в `acunetix_scan_import_pt.py`.
- TODO: если в DefectDojo требуется строгое привязывание импорта к engagement/test, дополнительно зафиксируйте политику создания engagement (текущая логика использует `engagement_name` в `import-scan`).
- TODO: в `WF_Master_Orchestrator` `workflowId` может потребовать обновления на реальные ID после импорта в конкретный n8n instance.
