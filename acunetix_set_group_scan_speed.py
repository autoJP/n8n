#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def make_session(verify: bool = False) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    return s


def log(level: str, message: str) -> None:
    print(f"{level}: {message}", file=sys.stderr)


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


def acu_list_groups(s: requests.Session, base_url: str, token: str, timeout: int) -> List[Dict[str, Any]]:
    r = s.get(f"{base_url.rstrip('/')}/api/v1/target_groups?limit=100", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("groups", [])


def acu_find_group_by_name(groups: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for g in groups:
        if g.get("name") == name:
            return g
    return None


def acu_get_group_targets(s: requests.Session, base_url: str, token: str, group_id: str, timeout: int) -> List[str]:
    r = s.get(f"{base_url.rstrip('/')}/api/v1/target_groups/{group_id}/targets", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("target_id_list", [])


def acu_get_target_configuration(s: requests.Session, base_url: str, token: str, target_id: str, timeout: int) -> Dict[str, Any]:
    r = s.get(f"{base_url.rstrip('/')}/api/v1/targets/{target_id}/configuration", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def acu_set_target_scan_speed(
    s: requests.Session, base_url: str, token: str, target_id: str, scan_speed: str, timeout: int
) -> requests.Response:
    payload = {"scan_speed": scan_speed}
    return s.patch(f"{base_url.rstrip('/')}/api/v1/targets/{target_id}/configuration", headers=acu_headers(token), json=payload, timeout=timeout)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", "--acu-base-url", dest="base_url", default=os.environ.get("ACUNETIX_BASE_URL"))
    ap.add_argument("--token", "--acu-api-token", dest="token", default=os.environ.get("ACUNETIX_API_TOKEN"))
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--group-id", dest="group_id")
    group.add_argument("--group-name", dest="group_name")
    ap.add_argument("--scan-speed", dest="scan_speed", default="sequential")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--output", help="Optional path to write result JSON")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    if not args.base_url or not args.token:
        log("ERROR", "base-url/token are required")
        print(json.dumps({"ok": False, "error": "missing_required"}, ensure_ascii=False))
        return 2
    if args.timeout <= 0:
        print(json.dumps({"ok": False, "error": "invalid_timeout"}, ensure_ascii=False))
        return 2

    debug: Dict[str, Any] = {
        "base_url": args.base_url,
        "group_id_arg": args.group_id,
        "group_name_arg": args.group_name,
        "scan_speed": args.scan_speed,
        "dry_run": bool(args.dry_run),
        "timeout": args.timeout,
    }

    try:
        s = make_session(verify=False)
        group_id = args.group_id
        group_info: Optional[Dict[str, Any]] = None

        if not group_id:
            groups = acu_list_groups(s, args.base_url, args.token, args.timeout)
            g = acu_find_group_by_name(groups, args.group_name)
            debug["groups_total"] = len(groups)
            if not g:
                result = {"ok": False, "error": "group_not_found", "details": f"Group with name '{args.group_name}' not found", "debug": debug}
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                print(json.dumps(result, ensure_ascii=False))
                return 1
            group_id = g.get("group_id")
            group_info = g
        else:
            group_info = {"group_id": group_id}

        if not group_id:
            raise RuntimeError("group_id is empty after resolution")

        debug["group_id"] = group_id
        debug["group_info"] = group_info

        target_ids = acu_get_group_targets(s, args.base_url, args.token, group_id, args.timeout)
        debug["targets_in_group_count"] = len(target_ids)

        if not target_ids:
            result = {"ok": True, "warning": "no_targets_in_group", "group_id": group_id, "scan_speed": args.scan_speed, "targets_total": 0, "debug": debug}
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        changed: List[str] = []
        skipped: List[str] = []
        errors: List[Dict[str, Any]] = []

        for tid in target_ids:
            try:
                cfg = acu_get_target_configuration(s, args.base_url, args.token, tid, args.timeout)
                current = cfg.get("scan_speed")
                if current == args.scan_speed:
                    skipped.append(tid)
                    continue
                if args.dry_run:
                    changed.append(tid)
                    continue
                r = acu_set_target_scan_speed(s, args.base_url, args.token, tid, args.scan_speed, args.timeout)
                if r.status_code not in (200, 204):
                    errors.append({"target_id": tid, "status": r.status_code, "response": safe_json(r)})
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

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1

    except Exception as e:
        debug["exception"] = str(e)
        debug["traceback"] = traceback.format_exc()
        result = {"ok": False, "error": "unexpected_error", "details": str(e), "debug": debug}
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
