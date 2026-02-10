# autoJP/n8n

Единый режим работы: **Master -> n8n workflows -> скрипты**, без прямых вызовов скриптов из Master.

## Архитектура (single source of truth)

Пайплайн для каждого Product Type всегда выполняется как:

`WF_A -> WF_B -> WF_C -> WF_D`

- `WF_Master_Orchestrator` запускает `master_orchestrator.py`.
- `master_orchestrator.py` вызывает **только n8n workflow execution API** для `WF_A/WF_B/WF_C/WF_D`.
- Каждый `WF_*` уже внутри себя может вызвать Python-скрипт (`executeCommand`), но это деталь реализации workflow, не Master.

## Входы workflow (стандартизировано)

Для `WF_A/WF_B/WF_C/WF_D` вход единообразный:
- `product_type_id` (обязательный)
- `product_id` (опциональный)
- `run_id` (опциональный)
- `trace_id` (опциональный)

Для `WF_Master_Orchestrator`:
- `product_type_ids` (обязательный, CSV)
- `run_id` (опциональный)
- `trace_id` (опциональный)

При отсутствии обязательного поля workflow возвращает контролируемый `invalid input`-результат, а не падает в середине.

## Единый контракт результата

Все workflow и вызываемые из них скрипты возвращают/нормализуют контракт:
- `ok` (boolean)
- `stage`
- `product_type_id`
- `product_id`
- `status` (`success|skipped|already_running|no_changes|error|timeout`)
- `metrics` (object)
- `errors` (array)
- `warnings` (array)
- `timestamps` (`started_at`, `finished_at`)

Master принимает решения **только** по этому контракту.

## State machine в DefectDojo

State хранится в `description` Product Type строкой:

`autojp_state:{...json...}`

Используемые ключи:
- `current_stage`
- `last_success_stage`
- `last_run_at`
- `last_error`
- `retry_count`
- `input_hash`

Алгоритм Master на каждый PT:
1. Читает state из Dojo.
2. Выбирает допустимый следующий stage.
3. Запускает только этот stage (через n8n workflow).
4. Обновляет state в Dojo по контракту результата.
5. Ошибка одного PT не останавливает обработку остальных PT.

## Идемпотентность

- **WF_C** (`acunetix_sync_pt.py`): upsert targets; при отсутствии изменений возвращает `status=no_changes` или `skipped`.
- **WF_D** (`acunetix_scan_pt.py`): scan-guard; если есть активный scan, возвращает `already_running`/`skipped` без запуска нового.
- Повторный запуск Master на неизменных данных не должен создавать новые targets и не должен запускать новый scan.

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

