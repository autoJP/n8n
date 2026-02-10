#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Устанавливает скорость сканирования (scan_speed) для всех целей в Target Group Acunetix.

Варианты использования:

1) Через group_id (предпочтительно, т.к. у нас он уже есть из acunetix_sync_pt.py):

   python3 acunetix_set_group_scan_speed.py \
     --acu-base-url https://192.168.68.103:3443 \
     --acu-api-token <TOKEN> \
     --group-id 46bcc7f4-8baf-4dc1-b4a3-004292b8a855 \
     --scan-speed sequential

2) Через group_name:

   python3 acunetix_set_group_scan_speed.py \
     --acu-base-url https://192.168.68.103:3443 \
     --acu-api-token <TOKEN> \
     --group-name testfire.net \
     --scan-speed sequential

Все параметры можно также брать из переменных окружения:
  ACU_BASE_URL, ACU_API_TOKEN.
"""

import argparse
import json
import sys
import traceback
from typing import Any, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# --------------- helpers ---------------

def make_session(verify: bool = False) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    return s


def acu_headers(token: str) -> Dict[str, str]:
    return {
        "X-Auth": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text[:500]}


# --------------- API calls ---------------

def acu_list_groups(
    s: requests.Session,
    base_url: str,
    token: str,
) -> List[Dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v1/target_groups?limit=100"
    r = s.get(url, headers=acu_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("groups", [])


def acu_find_group_by_name(groups: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for g in groups:
        if g.get("name") == name:
            return g
    return None


def acu_get_group_targets(
    s: requests.Session,
    base_url: str,
    token: str,
    group_id: str,
) -> List[str]:
    """
    GET /api/v1/target_groups/{group_id}/targets
    ответ содержит поле target_id_list.
    """
    url = f"{base_url.rstrip('/')}/api/v1/target_groups/{group_id}/targets"
    r = s.get(url, headers=acu_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("target_id_list", [])


def acu_get_target_configuration(
    s: requests.Session,
    base_url: str,
    token: str,
    target_id: str,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/targets/{target_id}/configuration"
    r = s.get(url, headers=acu_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def acu_set_target_scan_speed(
    s: requests.Session,
    base_url: str,
    token: str,
    target_id: str,
    scan_speed: str,
) -> requests.Response:
    url = f"{base_url.rstrip('/')}/api/v1/targets/{target_id}/configuration"
    payload = {"scan_speed": scan_speed}
    r = s.patch(url, headers=acu_headers(token), json=payload, timeout=30)
    return r


# --------------- main ---------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--acu-base-url", dest="acu_base_url", required=True)
    ap.add_argument("--acu-api-token", dest="acu_api_token", required=True)

    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--group-id", dest="group_id")
    group.add_argument("--group-name", dest="group_name")

    ap.add_argument(
        "--scan-speed",
        dest="scan_speed",
        default="sequential",
        help="scan_speed значение (по умолчанию 'sequential')",
    )
    ap.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="не менять конфигурацию, только показать план",
    )

    args = ap.parse_args()

    debug: Dict[str, Any] = {
        "acu_base_url": args.acu_base_url,
        "group_id_arg": args.group_id,
        "group_name_arg": args.group_name,
        "scan_speed": args.scan_speed,
        "dry_run": bool(args.dry_run),
    }

    try:
        s = make_session(verify=False)

        # 1. Определяем group_id
        group_id = args.group_id
        group_info: Optional[Dict[str, Any]] = None

        if not group_id:
            groups = acu_list_groups(s, args.acu_base_url, args.acu_api_token)
            debug["groups_total"] = len(groups)
            g = acu_find_group_by_name(groups, args.group_name)
            if not g:
                print(json.dumps({
                    "ok": False,
                    "error": "group_not_found",
                    "details": f"Group with name '{args.group_name}' not found",
                    "debug": debug,
                }, ensure_ascii=False))
                sys.exit(1)
            group_id = g.get("group_id")
            group_info = g
        else:
            # можем при желании подтянуть инфу о группе, но это не обязательно
            group_info = {"group_id": group_id}

        if not group_id:
            raise RuntimeError("group_id is empty after resolution")

        debug["group_id"] = group_id
        debug["group_info"] = group_info

        # 2. Получаем все target_id в группе
        target_ids = acu_get_group_targets(s, args.acu_base_url, args.acu_api_token, group_id)
        debug["targets_in_group_count"] = len(target_ids)
        debug["targets_in_group"] = target_ids

        if not target_ids:
            print(json.dumps({
                "ok": True,
                "warning": "no_targets_in_group",
                "group_id": group_id,
                "scan_speed": args.scan_speed,
                "debug": debug,
            }, ensure_ascii=False))
            return

        changed: List[str] = []
        skipped: List[str] = []
        errors: List[Dict[str, Any]] = []

        # 3. Для каждого target — проверяем текущий scan_speed и при необходимости меняем
        for tid in target_ids:
            try:
                cfg = acu_get_target_configuration(s, args.acu_base_url, args.acu_api_token, tid)
                current = cfg.get("scan_speed")
                if current == args.scan_speed:
                    skipped.append(tid)
                    continue

                if args.dry_run:
                    changed.append(tid)
                    continue

                r = acu_set_target_scan_speed(s, args.acu_base_url, args.acu_api_token, tid, args.scan_speed)
                if r.status_code not in (200, 204):
                    errors.append({
                        "target_id": tid,
                        "status": r.status_code,
                        "response": safe_json(r),
                    })
                else:
                    changed.append(tid)
            except Exception as e:
                errors.append({"target_id": tid, "error": str(e)})

        result = {
            "ok": len(errors) == 0,
            "group_id": group_id,
            "scan_speed": args.scan_speed,
            "dry_run": bool(args.dry_run),
            "targets_total": len(target_ids),
            "targets_changed": changed,
            "targets_changed_count": len(changed),
            "targets_skipped": skipped,
            "targets_skipped_count": len(skipped),
            "errors": errors,
            "debug": debug,
        }

        print(json.dumps(result, ensure_ascii=False))

        if errors:
            sys.exit(1)

    except Exception as e:
        debug["exception"] = str(e)
        debug["traceback"] = traceback.format_exc()
        print(json.dumps({
            "ok": False,
            "error": "unexpected_error",
            "details": str(e),
            "debug": debug,
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

