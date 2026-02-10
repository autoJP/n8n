#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

STATE_PREFIX = "autojp_state:"
STAGES = ["WF_A", "WF_B", "WF_C", "WF_D"]
FINAL_STAGE = STAGES[-1]
SUCCESS_STATUSES = {"success", "skipped", "no_changes", "already_running", "skipped_recent"}
TRANSIENT_KEYWORDS = {"timeout", "tempor", "rate", "429", "5xx", "503", "502", "504", "connection", "unavailable"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_result() -> Dict[str, Any]:
    ts = now_iso()
    return {
        "ok": False,
        "stage": "WF_MASTER",
        "product_type_id": None,
        "product_id": None,
        "status": "error",
        "metrics": {},
        "errors": [{"code": "invalid_workflow_response"}],
        "warnings": [],
        "timestamps": {"started_at": ts, "finished_at": ts},
    }


def read_state_from_description(description: str) -> Dict[str, Any]:
    for line in (description or "").splitlines():
        if line.startswith(STATE_PREFIX):
            try:
                return json.loads(line[len(STATE_PREFIX) :].strip())
            except Exception:
                return {}
    return {}


def write_state_to_description(description: str, state: Dict[str, Any]) -> str:
    lines = [line for line in (description or "").splitlines() if not line.startswith(STATE_PREFIX)]
    lines.append(f"{STATE_PREFIX}{json.dumps(state, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines).strip()


def dojo_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_product_type(base_url: str, token: str, pt_id: int, timeout: int) -> Dict[str, Any]:
    r = requests.get(f"{base_url.rstrip('/')}/product_types/{pt_id}/", headers=dojo_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def patch_product_type_state(base_url: str, token: str, pt_id: int, state: Dict[str, Any], timeout: int) -> None:
    pt = get_product_type(base_url, token, pt_id, timeout)
    payload = {"description": write_state_to_description(pt.get("description") or "", state)}
    p = requests.patch(
        f"{base_url.rstrip('/')}/product_types/{pt_id}/",
        headers=dojo_headers(token),
        json=payload,
        timeout=timeout,
    )
    p.raise_for_status()


def normalize_workflow_result(raw: Any, fallback_stage: str, pt_id: int) -> Dict[str, Any]:
    if isinstance(raw, dict):
        candidate = raw
        if isinstance(raw.get("data"), list) and raw.get("data"):
            first = raw["data"][0]
            if isinstance(first, dict) and isinstance(first.get("json"), dict):
                candidate = first["json"]
        if isinstance(raw.get("json"), dict):
            candidate = raw["json"]
        if isinstance(candidate, dict) and "ok" in candidate and "status" in candidate:
            candidate.setdefault("stage", fallback_stage)
            candidate.setdefault("product_type_id", pt_id)
            candidate.setdefault("metrics", {})
            candidate.setdefault("errors", [])
            candidate.setdefault("warnings", [])
            candidate.setdefault("timestamps", {"started_at": now_iso(), "finished_at": now_iso()})
            candidate.setdefault("product_id", None)
            return candidate
    out = default_result()
    out["stage"] = fallback_stage
    out["product_type_id"] = pt_id
    return out


def build_n8n_headers(api_key: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-N8N-API-KEY"] = api_key
    return headers


def execute_workflow(
    n8n_base_url: str,
    n8n_api_key: str,
    workflow_id: str,
    payload: Dict[str, Any],
    timeout: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    # n8n API: POST /api/v1/workflows/{id}/execute
    url = f"{n8n_base_url.rstrip('/')}/api/v1/workflows/{workflow_id}/execute"
    req = {"workflowData": None, "input": [payload], "waitTillCompletion": True}
    r = requests.post(url, headers=build_n8n_headers(n8n_api_key), json=req, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    normalized = normalize_workflow_result(body, payload.get("stage") or "WF_UNKNOWN", int(payload["product_type_id"]))
    return normalized, body


def summarize_raw_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    data_len = len(raw.get("data", [])) if isinstance(raw.get("data"), list) else 0
    return {
        "has_data": data_len > 0,
        "data_items": data_len,
        "execution_id": raw.get("id") or raw.get("executionId"),
        "status": raw.get("status"),
    }


def is_transient_exception(exc: Exception) -> bool:
    transient_types = (
        requests.Timeout,
        requests.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )
    return isinstance(exc, transient_types)


def is_transient_result(step_result: Dict[str, Any]) -> bool:
    if (step_result.get("status") or "").lower() == "timeout":
        return True
    for err in step_result.get("errors", []):
        text = json.dumps(err, ensure_ascii=False).lower()
        if "401" in text or "403" in text or "invalid" in text or "validation" in text:
            return False
        if any(keyword in text for keyword in TRANSIENT_KEYWORDS):
            return True
    return False


def next_stage(state: Dict[str, Any]) -> Optional[str]:
    current = state.get("current_stage")
    if not current:
        return STAGES[0]
    if current not in STAGES:
        return STAGES[0]
    idx = STAGES.index(current)
    if idx >= len(STAGES) - 1:
        return None
    return STAGES[idx + 1]


def input_hash_for_pt(pt: Dict[str, Any]) -> str:
    marker = {
        "name": pt.get("name"),
        "updated": pt.get("updated") or pt.get("updated_at") or "",
    }
    return hashlib.sha256(json.dumps(marker, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("DOJO_BASE_URL"))
    ap.add_argument("--token", default=os.environ.get("DOJO_API_TOKEN"))
    ap.add_argument("--product-type-ids", required=True, help="Comma-separated PT IDs")
    ap.add_argument("--n8n-base-url", default=os.environ.get("N8N_BASE_URL", "http://localhost:5678"))
    ap.add_argument("--n8n-api-key", default=os.environ.get("N8N_API_KEY", ""))
    ap.add_argument("--wf-a-id", default=os.environ.get("N8N_WF_A_ID"))
    ap.add_argument("--wf-b-id", default=os.environ.get("N8N_WF_B_ID"))
    ap.add_argument("--wf-c-id", default=os.environ.get("N8N_WF_C_ID"))
    ap.add_argument("--wf-d-id", default=os.environ.get("N8N_WF_D_ID"))
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--max-retries", type=int, default=int(os.environ.get("MASTER_MAX_RETRIES", "2")))
    ap.add_argument("--retry-backoff-seconds", type=float, default=float(os.environ.get("MASTER_RETRY_BACKOFF_SECONDS", "2")))
    args = ap.parse_args()

    stage_to_id = {
        "WF_A": args.wf_a_id,
        "WF_B": args.wf_b_id,
        "WF_C": args.wf_c_id,
        "WF_D": args.wf_d_id,
    }

    started = now_iso()
    result = {
        "ok": True,
        "stage": "WF_MASTER",
        "product_type_id": None,
        "product_id": None,
        "status": "success",
        "metrics": {"processed": 0, "failed": 0},
        "errors": [],
        "warnings": [],
        "timestamps": {"started_at": started, "finished_at": started},
        "items": [],
    }

    missing_wf_ids = [k for k, v in stage_to_id.items() if not v]
    if missing_wf_ids:
        err = {"code": "missing_workflow_ids", "details": missing_wf_ids}
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append(err)
        result["timestamps"]["finished_at"] = now_iso()
        print(json.dumps(result, ensure_ascii=False))
        return 1

    ids = [int(x.strip()) for x in args.product_type_ids.split(",") if x.strip()]

    for pt_id in ids:
        item: Dict[str, Any] = {"product_type_id": pt_id, "status": "skipped", "steps": []}
        try:
            pt = get_product_type(args.base_url, args.token, pt_id, args.timeout)
            state = read_state_from_description(pt.get("description") or "")
            state.setdefault("current_stage", None)
            state.setdefault("last_success_stage", None)
            state.setdefault("last_run_at", None)
            state.setdefault("last_error", None)
            state.setdefault("retry_count", 0)
            state.setdefault("pipeline_status", "idle")
            current_input_hash = input_hash_for_pt(pt)
            previous_success_hash = state.get("last_success_input_hash")

            if (
                previous_success_hash
                and current_input_hash == previous_success_hash
                and state.get("last_success_stage") == FINAL_STAGE
                and not state.get("last_error")
            ):
                item["status"] = "skipped_no_change"
                state["input_hash"] = current_input_hash
                state["pipeline_status"] = "skipped_no_change"
                state["last_run_at"] = now_iso()
                patch_product_type_state(args.base_url, args.token, pt_id, state, args.timeout)
                result["items"].append(item)
                continue

            if current_input_hash != state.get("input_hash"):
                state["current_stage"] = None
                state["last_success_stage"] = None
                state["last_error"] = None
                state["retry_count"] = 0

            if state.get("last_error") and current_input_hash == state.get("input_hash") and int(state.get("retry_count", 0)) >= args.max_retries:
                item["status"] = "failed"
                state["pipeline_status"] = "failed"
                state["last_run_at"] = now_iso()
                patch_product_type_state(args.base_url, args.token, pt_id, state, args.timeout)
                result["items"].append(item)
                continue

            state["input_hash"] = current_input_hash
            state["pipeline_status"] = "running"

            # Run until pipeline completes or an error occurs.
            while True:
                stage = state.get("current_stage")
                if not stage:
                    stage = STAGES[0]
                wf_id = stage_to_id.get(stage)
                if not wf_id:
                    item["status"] = "error"
                    item["steps"].append({"stage": stage, "status": "error", "error": "workflow_id_not_set"})
                    state["last_error"] = {"code": "workflow_id_not_set", "stage": stage}
                    break

                payload = {
                    "product_type_id": pt_id,
                    "run_id": f"pt-{pt_id}-{int(datetime.now(timezone.utc).timestamp())}",
                    "trace_id": f"master-{pt_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    "stage": stage,
                }
                attempt = 0
                transient_failure = False
                while True:
                    raw: Dict[str, Any] = {}
                    try:
                        step_result, raw = execute_workflow(
                            args.n8n_base_url,
                            args.n8n_api_key,
                            wf_id,
                            payload,
                            args.timeout,
                        )
                    except Exception as exc:
                        if is_transient_exception(exc) and attempt < args.max_retries:
                            delay = args.retry_backoff_seconds * (2**attempt)
                            attempt += 1
                            state["retry_count"] = attempt
                            time.sleep(delay)
                            continue
                        raise

                    transient_failure = is_transient_result(step_result)
                    if not (step_result.get("ok") and step_result.get("status") in SUCCESS_STATUSES) and transient_failure and attempt < args.max_retries:
                        delay = args.retry_backoff_seconds * (2**attempt)
                        attempt += 1
                        state["retry_count"] = attempt
                        time.sleep(delay)
                        continue
                    break

                step_status = step_result.get("status", "error")
                step_ok = bool(step_result.get("ok")) and step_status in SUCCESS_STATUSES
                item["steps"].append(
                    {
                        "stage": stage,
                        "status": step_status,
                        "attempts": attempt + 1,
                        "summary": summarize_raw_response(raw),
                        "metrics": step_result.get("metrics", {}),
                    }
                )
                state["last_run_at"] = now_iso()

                if step_ok:
                    state["last_success_stage"] = stage
                    state["last_error"] = None
                    state["retry_count"] = 0
                    nxt = next_stage({"current_stage": stage})
                    if nxt is None:
                        item["status"] = "success"
                        state["current_stage"] = None
                        state["last_success_input_hash"] = current_input_hash
                        state["pipeline_status"] = "success"
                        break
                    state["current_stage"] = nxt
                    patch_product_type_state(args.base_url, args.token, pt_id, state, args.timeout)
                    continue

                state["last_error"] = {
                    "stage": stage,
                    "status": step_status,
                    "errors": step_result.get("errors", []),
                }
                state["retry_count"] = attempt + 1
                state["current_stage"] = stage
                state["pipeline_status"] = "failed" if (not transient_failure or state["retry_count"] >= args.max_retries) else "error"
                item["status"] = "failed" if state["pipeline_status"] == "failed" else "error"
                break

            patch_product_type_state(args.base_url, args.token, pt_id, state, args.timeout)
        except Exception as exc:
            item["status"] = "error"
            item["steps"].append({"stage": "WF_MASTER", "status": "error", "details": str(exc)})

        if item["status"] in {"error", "failed"}:
            result["ok"] = False
            result["metrics"]["failed"] += 1
        result["items"].append(item)

    result["metrics"]["processed"] = len(result["items"])
    result["status"] = "success" if result["ok"] else "error"
    result["timestamps"]["finished_at"] = now_iso()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
