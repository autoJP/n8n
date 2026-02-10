# autoJP/n8n

Единый режим работы: **Master -> n8n workflows -> скрипты**, без прямых вызовов скриптов из Master.

## Архитектура (single source of truth)

Пайплайн для каждого Product Type всегда выполняется как:

`WF_A -> WF_B -> WF_C -> WF_D`

- `WF_Master_Orchestrator` запускает `master_orchestrator.py`.
- `master_orchestrator.py` вызывает **только n8n workflow execution API** для `WF_A/WF_B/WF_C/WF_D`.
- Каждый `WF_*` уже внутри себя может вызвать Python-скрипт (`executeCommand`), но это деталь реализации workflow, не Master.
- Контракт результатов (`ok/stage/product_type_id/status/metrics/errors/warnings/timestamps`) — единственный источник решений Master.

## Контракт входа и выхода

### Входы workflow (стандартизировано)

Для `WF_A/WF_B/WF_C/WF_D` вход единообразный:
- `product_type_id` (обязательный)
- `product_id` (опциональный)
- `run_id` (опциональный)
- `trace_id` (опциональный)
- `stage` (внутренний маркер Master)

Для `WF_Master_Orchestrator`:
- `product_type_ids` (обязательный, CSV)
- `run_id` (опциональный)
- `trace_id` (опциональный)

### Единый контракт результата

Все workflow и вызываемые из них скрипты возвращают/нормализуют контракт:
- `ok` (boolean)
- `stage`
- `product_type_id`
- `product_id`
- `status` (`success|skipped|already_running|skipped_recent|no_changes|error|timeout`)
- `metrics` (object)
- `errors` (array)
- `warnings` (array)
- `timestamps` (`started_at`, `finished_at`)

Master **не анализирует сырые логи n8n как источник управления**. В результатах Master хранится только краткий `summary` ответа n8n (без большого `raw`).

## State machine в DefectDojo

State хранится в `description` Product Type строкой:

`autojp_state:{...json...}`

Формат state:
- `current_stage`
- `last_success_stage`
- `last_success_input_hash`
- `last_run_at`
- `last_error`
- `retry_count`
- `input_hash`
- `pipeline_status` (`running|success|error|failed|skipped_no_change|idle`)

### Семантика выполнения Master по PT

1. Master вычисляет `input_hash` PT.
2. Если `input_hash == last_success_input_hash` и `last_success_stage == WF_D` и `last_error == null`:
   - PT получает `status=skipped_no_change`.
   - `WF_A/WF_B/WF_C/WF_D` **не запускаются**.
3. Если вход изменился (`input_hash` другой):
   - stage/state сбрасывается в начало,
   - цепочка снова проходит `WF_A -> WF_D`.
4. Если есть `last_error` и `retry_count` уже исчерпан:
   - PT помечается `failed` до ручного вмешательства.
5. Ошибка одного PT не останавливает обработку остальных PT.

## Retry policy

- Retry применяется только к транзиентным сбоям:
  - network timeout/connection errors,
  - `429/5xx` и похожие временные ошибки из контракта стадии.
- Retry **не применяется** к `401/403`, invalid/validation input.
- Параметры Master:
  - `--max-retries` (по умолчанию `2`),
  - `--retry-backoff-seconds` (по умолчанию `2`, экспоненциальный backoff).
- После исчерпания retry PT получает `pipeline_status=failed`.

## Идемпотентность стадий

- **WF_A/WF_B**: формируют и возвращают данные по контракту; Master использует только контрактные статусы/метрики.
- **WF_C (`acunetix_sync_pt.py`)**:
  - upsert targets,
  - при отсутствии новых targets возвращает `status=no_changes`.
- **WF_D (`acunetix_scan_pt.py`)**:
  - scan-guard для активных сканов (`already_running`),
  - защита по свежести: `skipped_recent` если скан по целям был недавно.

## Идентификаторы Acunetix

Единая семантика:
- `group_id` — идентификатор target group (уровень PT).
- `target_id` — идентификатор конкретной цели в Acunetix.
- `scan_id` — идентификатор запуска скана.

`WF_C` отвечает за `group_id` и синхронизацию `target_id`; `WF_D` использует `group_id -> target_id -> scan_id` как явную последовательность.

## Как вручную проверить идемпотентность

1. Запустить Master для PT (первый прогон) и дождаться `success/no_changes/already_running/skipped_recent`.
2. Сразу запустить Master второй раз с тем же PT и неизменным входом.
3. Проверить, что:
   - результат PT: `skipped_no_change`,
   - стадии не запускались,
   - в Acunetix не создано новых targets,
   - новые scans не стартовали.

## Workflows

- `WF_A_Subdomains_PT.json`
- `WF_B_Nmap_Product.json`
- `WF_C_Targets_For_PT.json`
- `WF_D_Scan_For_PT.json`
- `WF_E_HealthCheck.json`
- `WF_Master_Orchestrator.json`

## Обязательные env

См. `.env.example`.

Dojo/Acunetix:
- `DOJO_BASE_URL`
- `DOJO_API_TOKEN`
- `ACUNETIX_BASE_URL`
- `ACUNETIX_API_TOKEN`
- `ACUNETIX_SCAN_PROFILE_ID`
- `ACUNETIX_RECENT_SCAN_WINDOW_MINUTES`
- `NMAP_XML_DIR`

N8N execution API:
- `N8N_BASE_URL`
- `N8N_API_KEY`
- `N8N_WF_A_ID`
- `N8N_WF_B_ID`
- `N8N_WF_C_ID`
- `N8N_WF_D_ID`

## Healthcheck

`WF_E_HealthCheck` делает авторизованный Dojo-check и различает:
- auth ошибки (`401/403`) -> `dojo_auth_failed`
- network/connectivity проблемы -> `dojo_unreachable`

## Безопасность n8n

- Ограничивайте права редактирования workflow (RBAC/минимально необходимые роли).
- Держите n8n на актуальной версии безопасности.
- Помните, что `executeCommand`-узлы способны запускать команды ОС — это зона повышенного риска и требует контроля доступа/аудита.
