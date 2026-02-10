#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STAGE = "WF_D"
ACTIVE_SCAN_STATUSES = {"queued", "processing", "starting", "scheduled"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def result_contract(pt_id: int, status: str, ok: bool = True) -> Dict[str, Any]:
    ts = now_iso()
    return {
        "ok": ok,
        "stage": STAGE,
        "product_type_id": pt_id,
        "status": status,
        "metrics": {},
        "errors": [],
        "warnings": [],
        "timestamps": {"started_at": ts, "finished_at": ts},
    }


def headers_dojo(token: str) -> Dict[str, str]:
    return {"Authorization": f"Token {token}", "Accept": "application/json", "Content-Type": "application/json"}


def headers_acu(token: str) -> Dict[str, str]:
    return {"X-Auth": token, "Accept": "application/json", "Content-Type": "application/json"}


def get_json(session: requests.Session, url: str, headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    r = session.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def post_json(session: requests.Session, url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    r = session.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", dest="dojo_base_url", default=os.environ.get("DOJO_BASE_URL"))
    p.add_argument("--token", dest="dojo_token", default=os.environ.get("DOJO_API_TOKEN"))
    p.add_argument("--product-type-id", dest="product_type_id", type=int, required=True)
    p.add_argument("--acu-base-url", default=os.environ.get("ACUNETIX_BASE_URL"))
    p.add_argument("--acu-token", dest="acu_token", default=os.environ.get("ACUNETIX_API_TOKEN"))
    p.add_argument("--scan-profile-id", default=os.environ.get("ACUNETIX_SCAN_PROFILE_ID", "11111111-1111-1111-1111-111111111111"))
    p.add_argument("--incremental", action="store_true")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out = result_contract(args.product_type_id, "error", ok=False)
    out["timestamps"]["started_at"] = now_iso()

    if not all([args.dojo_base_url, args.dojo_token, args.acu_base_url, args.acu_token]):
        out["errors"].append({"code": "missing_required"})
        print(json.dumps(out, ensure_ascii=False))
        return 2

    try:
        s = requests.Session()
        s.verify = False

        pt = get_json(
            s,
            f"{args.dojo_base_url.rstrip('/')}/product_types/{args.product_type_id}/",
            headers_dojo(args.dojo_token),
            args.timeout,
        )
        pt_name = pt.get("name") or f"PT-{args.product_type_id}"

        groups = get_json(s, f"{args.acu_base_url.rstrip('/')}/api/v1/target_groups?limit=100", headers_acu(args.acu_token), args.timeout).get("groups", [])
        group = next((g for g in groups if g.get("name") == pt_name), None)
        if not group:
            out["ok"] = True
            out["status"] = "skipped"
            out["warnings"].append("group_not_found")
            out["timestamps"]["finished_at"] = now_iso()
            print(json.dumps(out, ensure_ascii=False))
            return 0

        group_id = group.get("group_id")
        target_ids = get_json(s, f"{args.acu_base_url.rstrip('/')}/api/v1/target_groups/{group_id}/targets", headers_acu(args.acu_token), args.timeout).get("target_id_list", [])
        if not target_ids:
            out["ok"] = True
            out["status"] = "skipped"
            out["warnings"].append("no_targets")
            out["metrics"].update({"group_id": group_id})
            out["timestamps"]["finished_at"] = now_iso()
            print(json.dumps(out, ensure_ascii=False))
            return 0

        scans = get_json(s, f"{args.acu_base_url.rstrip('/')}/api/v1/scans?c=100", headers_acu(args.acu_token), args.timeout).get("scans", [])
        target_set: Set[str] = set(target_ids)
        active = []
        for scan in scans:
            status = (scan.get("current_session", {}) or {}).get("status") or scan.get("status")
            tid = scan.get("target_id") or (scan.get("target", {}) or {}).get("target_id")
            if tid in target_set and str(status).lower() in ACTIVE_SCAN_STATUSES:
                active.append({"scan_id": scan.get("scan_id"), "target_id": tid, "status": status})

        out["metrics"].update({"group_id": group_id, "targets_count": len(target_ids), "active_scans_count": len(active)})
        if active:
            out["ok"] = True
            out["status"] = "already_running"
            out["warnings"].append("scan_guard_triggered")
            out["metrics"]["active_scans"] = active
            out["timestamps"]["finished_at"] = now_iso()
            print(json.dumps(out, ensure_ascii=False))
            return 0

        if args.dry_run:
            out["ok"] = True
            out["status"] = "success"
            out["metrics"]["dry_run"] = True
            out["timestamps"]["finished_at"] = now_iso()
            print(json.dumps(out, ensure_ascii=False))
            return 0

        started = []
        for tid in target_ids:
            payload = {
                "target_id": tid,
                "profile_id": args.scan_profile_id,
                "incremental": bool(args.incremental),
                "schedule": {"disable": False, "start_date": None, "time_sensitive": False},
            }
            created = post_json(s, f"{args.acu_base_url.rstrip('/')}/api/v1/scans", headers_acu(args.acu_token), payload, args.timeout)
            started.append({"target_id": tid, "scan_id": created.get("scan_id")})

        out["ok"] = True
        out["status"] = "success"
        out["metrics"]["started_scans_count"] = len(started)
        out["metrics"]["started_scans"] = started
        out["timestamps"]["finished_at"] = now_iso()
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except Exception as exc:
        out["ok"] = False
        out["status"] = "error"
        out["errors"].append({"code": "unexpected_error", "details": str(exc)})
        out["timestamps"]["finished_at"] = now_iso()
        print(json.dumps(out, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
