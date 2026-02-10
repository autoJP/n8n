# autoJP/n8n

Набор n8n workflow + Python CLI для пайплайна PT: `WF_A -> WF_B -> WF_C -> WF_D`,
с healthcheck (`WF_E`) и мастер-оркестратором (`WF_Master_Orchestrator`).

## Что исправлено
- Убраны захардкоженные секреты/URL/IP из workflow exports: только `$env.*` и credentials.
- Убран хардкод `product_type_id`: PT передаётся только через вход workflow.
- Приведены переменные к единому стандарту: `DOJO_*`, `ACUNETIX_*`.
- `WF_E` проверяет Dojo только с авторизацией (401/403 отдельно от network timeout).
- Добавлена идемпотентность:
  - `WF_C` (`acunetix_sync_pt.py`) делает upsert и возвращает `skipped/no_changes`.
  - `WF_D` (`acunetix_scan_pt.py`) не запускает новый скан, если уже есть активный.
- Master переведён в watcher/state-machine (`master_orchestrator.py` + `WF_Master_Orchestrator`).

## Workflows
- `WF_A_Subdomains_PT.json`
- `WF_B_Nmap_Product.json`
- `WF_C_Targets_For_PT.json`
- `WF_D_Scan_For_PT.json`
- `WF_E_HealthCheck.json`
- `WF_Master_Orchestrator.json`

## Обязательные env/credentials
См. `.env.example`.

Обязательно:
- `DOJO_BASE_URL`
- `DOJO_API_TOKEN`
- `ACUNETIX_BASE_URL`
- `ACUNETIX_API_TOKEN`
- `ACUNETIX_SCAN_PROFILE_ID`

## Единый контракт результата
Все ключевые шаги возвращают:
- `ok`
- `stage`
- `product_type_id`
- `status` (`success|skipped|error|already_running|timeout`)
- `metrics`
- `errors[]`
- `warnings[]`
- `timestamps`

## Где хранится state
State хранится в `description` соответствующего `Product Type` в DefectDojo, строкой:

`autojp_state:{...json...}`

Ключи state:
- `WF_A`, `WF_C`, `WF_D` — результат последнего шага (`success|error`)
- `last_run`
- `last_error`
- `retry_count`
- `failed_step`

## Проверка идемпотентности
1. Запустить `WF_C` дважды для одного PT с неизменными данными.
   - Второй запуск: `status=skipped`, причина `no_changes`.
2. Запустить `WF_D` дважды для одного PT.
   - Если первый скан активен: второй запуск `status=already_running`.
3. Запустить `WF_Master_Orchestrator` дважды.
   - Второй запуск не должен повторно выполнять уже успешные шаги без изменения состояния.

## Версионные зависимости
- Endpoints Acunetix (`/api/v1/targets/add`, `/api/v1/scans`, `/api/v1/target_groups/...`) зависят от версии Acunetix.
- Формат полей Product Type в Dojo зависит от версии Dojo API v2.
- При несовместимости меняйте только URL/fields в:
  - `acunetix_sync_pt.py`
  - `acunetix_scan_pt.py`
  - `master_orchestrator.py`
