# autoJP/n8n

Workflow-пайплайн для Dojo→Nmap→Acunetix с идемпотентным оркестратором.

## Workflows
- `WF_C_Targets_For_PT.json` — idempotent подготовка targets и sync в Acunetix (upsert).
- `WF_D_AcunetixScan_PT.json` — запуск scan только если нет активного/ожидающего/недавнего запуска.
- `WF_E_Health.json` — health checks сервисов.
- `WF_Master_DojoWatcher.json` — watcher/state-machine по Product Type, состояние хранится в DefectDojo tag.

## Скрипты (единый контракт)
Все ключевые скрипты возвращают единый JSON-контракт в stdout:

```json
{
  "ok": true,
  "status": "ok|skipped|already_running|timeout|error",
  "stage": "WF_*.*",
  "product_type_id": 123,
  "timestamps": {"start": "...", "end": "..."},
  "metrics": {"targets_created": 0, "targets_total": 0, "scans_started": 0, "scans_skipped": 0, "findings_imported": 0},
  "errors": [],
  "warnings": []
}
```

Exit codes:
- `0` — success/skipped/already_running/timeout (контролируемые исходы)
- `1` — runtime error
- `2` — invalid input/args
- `3` — external API/auth/rate-limit issues

## State machine (Master)
Состояние PT хранится в DefectDojo tag `autojp:state=<base64(json)>`, где JSON содержит:
- `current_stage`
- `last_success_stage`
- `last_run_at`
- `last_error`
- `retry_count`

Master перед этапом читает состояние и пропускает уже завершённые шаги при неизменившемся входе.
При ошибке этапа мастер обновляет `last_error`, увеличивает `retry_count` и ограничивает retry.
Ошибки изолированы на уровне PT (проблемный PT не блокирует другие).

## Порядок запуска
1. Импортировать workflows: `WF_C` → `WF_D` → `WF_E` → `WF_Master`.
2. Заполнить `.env` из `.env.example`.
3. Запустить `WF_Master_DojoWatcher` по расписанию.

## Идемпотентность: как проверить вручную
1. Запустить `WF_C_Targets_For_PT` дважды для одного PT.
   - Первый запуск: `targets_created > 0` (если были новые цели).
   - Второй запуск: `status=skipped`, `targets_created=0`.
2. Запустить `WF_D_AcunetixScan_PT` дважды подряд.
   - Первый запуск: `scans_started=1`.
   - Второй запуск: `status=already_running` или `skipped`, `scans_started=0`, `scans_skipped=1`.
3. Запустить `WF_Master_DojoWatcher` повторно — не должно создаваться новых scans/targets без изменений входа.

## Caveats (версии API)
- Acunetix endpoint-ы `/api/v1/targets/{id}`, `/api/v1/scans`, `/api/v1/target_groups/{id}/targets` могут отличаться между версиями.
- Если формат отличается, менять нужно в `acunetix_sync_pt.py` и `acunetix_scan_pt.py`.
- DefectDojo поле `tags` и PATCH semantics могут отличаться по версиям/настройкам.
