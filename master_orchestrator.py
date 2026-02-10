#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

STATE_PREFIX = "autojp_state:"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cmd(cmd: List[str]) -> Dict[str, Any]:
    cp = subprocess.run(cmd, text=True, capture_output=True)
    body = {}
    try:
        body = json.loads(cp.stdout.strip() or "{}")
    except Exception:
        body = {"ok": False, "status": "error", "errors": [{"code": "invalid_json", "details": cp.stdout}]}
    return {"exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr, "body": body}


def get_state(pt: Dict[str, Any]) -> Dict[str, Any]:
    desc = pt.get("description") or ""
    for line in desc.splitlines():
        if line.startswith(STATE_PREFIX):
            raw = line[len(STATE_PREFIX) :].strip()
            try:
                return json.loads(raw)
            except Exception:
                return {}
    return {}


def set_state(base_url: str, token: str, pt_id: int, state: Dict[str, Any], timeout: int) -> None:
    h = {"Authorization": f"Token {token}", "Content-Type": "application/json", "Accept": "application/json"}
    r = requests.get(f"{base_url.rstrip('/')}/product_types/{pt_id}/", headers=h, timeout=timeout)
    r.raise_for_status()
    pt = r.json()
    desc = pt.get("description") or ""
    lines = [l for l in desc.splitlines() if not l.startswith(STATE_PREFIX)]
    lines.append(f"{STATE_PREFIX}{json.dumps(state, ensure_ascii=False, separators=(',', ':'))}")
    patch = {"description": "\n".join(lines).strip()}
    p = requests.patch(f"{base_url.rstrip('/')}/product_types/{pt_id}/", headers=h, json=patch, timeout=timeout)
    p.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("DOJO_BASE_URL"))
    ap.add_argument("--token", default=os.environ.get("DOJO_API_TOKEN"))
    ap.add_argument("--product-type-ids", required=True, help="Comma-separated PT IDs")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    result = {
        "ok": True,
        "stage": "WF_MASTER",
        "product_type_id": None,
        "status": "success",
        "metrics": {"processed": 0},
        "errors": [],
        "warnings": [],
        "timestamps": {"started_at": now_iso(), "finished_at": now_iso()},
        "items": [],
    }

    ids = [int(x.strip()) for x in args.product_type_ids.split(",") if x.strip()]
    h = {"Authorization": f"Token {args.token}", "Content-Type": "application/json", "Accept": "application/json"}

    for pt_id in ids:
        item: Dict[str, Any] = {"product_type_id": pt_id, "status": "skipped", "steps": []}
        try:
            pt = requests.get(f"{args.base_url.rstrip('/')}/product_types/{pt_id}/", headers=h, timeout=args.timeout).json()
            state = get_state(pt)
            retry = int(state.get("retry_count", 0))
            failed_step = state.get("failed_step")

            if retry >= 3 and failed_step:
                item["status"] = "skipped"
                item["steps"].append({"stage": failed_step, "status": "retry_limit_reached"})
                result["warnings"].append(f"pt_{pt_id}_retry_limit_reached")
                result["items"].append(item)
                continue

            pipeline = [
                ("WF_A", ["python3", "enum_subs_auto.py", "--domain", pt.get("name", "")]),
                ("WF_C", ["python3", "acunetix_sync_pt.py", "--product-type-id", str(pt_id)]),
                ("WF_D", ["python3", "acunetix_scan_pt.py", "--product-type-id", str(pt_id)]),
            ]

            started = False
            for stage, cmd in pipeline:
                if not started and state.get(stage) == "success":
                    continue
                started = True
                step = run_cmd(cmd)
                body = step["body"]
                status = body.get("status", "error")
                item["steps"].append({"stage": stage, "status": status})
                state[stage] = "success" if body.get("ok") else "error"
                state["last_run"] = now_iso()
                state["last_error"] = body.get("errors", [])
                if body.get("ok") and status in {"success", "skipped", "already_running"}:
                    state["retry_count"] = 0
                    state.pop("failed_step", None)
                else:
                    state["retry_count"] = retry + 1
                    state["failed_step"] = stage
                    set_state(args.base_url, args.token, pt_id, state, args.timeout)
                    item["status"] = "error"
                    break
                set_state(args.base_url, args.token, pt_id, state, args.timeout)
            else:
                item["status"] = "success"
        except Exception as exc:
            item["status"] = "error"
            item["steps"].append({"stage": "WF_MASTER", "status": "error", "details": str(exc)})
            result["ok"] = False

        result["items"].append(item)

    result["metrics"]["processed"] = len(result["items"])
    result["timestamps"]["finished_at"] = now_iso()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
