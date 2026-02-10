#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EXIT_RUNTIME = 1
EXIT_INPUT = 2
EXIT_EXTERNAL_API = 3
STAGE = "WF_C.acunetix_sync_pt"


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_result(pt_id: int, started_at: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "stage": STAGE,
        "product_type_id": pt_id,
        "timestamps": {"start": started_at, "end": None},
        "metrics": {
            "targets_total": 0,
            "targets_created": 0,
            "targets_skipped": 0,
            "group_created": 0,
            "group_updated": 0,
        },
        "errors": [],
        "warnings": [],
    }


def finalize(result: Dict[str, Any], output: Optional[str]) -> None:
    result["timestamps"]["end"] = ts()
    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))


def make_session(verify: bool) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    return s


def dojo_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Token {token}", "Accept": "application/json", "Content-Type": "application/json"}


def acu_headers(token: str) -> Dict[str, str]:
    return {"X-Auth": token, "Accept": "application/json", "Content-Type": "application/json"}


def api_call(method: str, session: requests.Session, url: str, timeout: int, retries: int = 2, **kwargs: Any) -> requests.Response:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = session.request(method, url, timeout=timeout, **kwargs)
            if r.status_code in (401, 403):
                raise PermissionError(f"auth_error:{r.status_code}")
            if r.status_code == 429 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return r
        except (requests.RequestException, PermissionError) as e:
            last = e
            if attempt >= retries:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"request_failed:{method}:{url}:{last}")


def dojo_get_product_type(s: requests.Session, base_url: str, token: str, pt_id: int, timeout: int) -> Dict[str, Any]:
    r = api_call("GET", s, f"{base_url.rstrip('/')}/product_types/{pt_id}/", headers=dojo_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def dojo_get_products_for_pt(s: requests.Session, base_url: str, token: str, pt_id: int, timeout: int) -> List[Dict[str, Any]]:
    r = api_call("GET", s, f"{base_url.rstrip('/')}/products/?prod_type={pt_id}&limit=1000", headers=dojo_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("results", [])


def normalize_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return False


def build_targets_from_products(products: List[Dict[str, Any]]) -> List[str]:
    seen, urls = set(), []
    for prod in products:
        if not normalize_bool(prod.get("internet_accessible")):
            continue
        name = (prod.get("name") or "").strip()
        if not name:
            continue
        lower = name.lower()
        if lower.startswith(("http://", "https://")):
            url = name
        elif any(ch.isdigit() for ch in lower) and ":" in lower and " " not in lower:
            url = f"http://{name}"
        else:
            bare = lower[4:] if lower.startswith("www.") else lower
            url = f"https://{bare}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def acu_list_groups(s: requests.Session, base_url: str, token: str, timeout: int) -> List[Dict[str, Any]]:
    r = api_call("GET", s, f"{base_url.rstrip('/')}/api/v1/target_groups?limit=100", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("groups", [])


def acu_create_group(s: requests.Session, base_url: str, token: str, name: str, desc: str, timeout: int) -> Dict[str, Any]:
    r = api_call("POST", s, f"{base_url.rstrip('/')}/api/v1/target_groups", headers=acu_headers(token), json={"name": name, "description": desc}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def acu_get_group_target_ids(s: requests.Session, base_url: str, token: str, group_id: str, timeout: int) -> List[str]:
    r = api_call("GET", s, f"{base_url.rstrip('/')}/api/v1/target_groups/{group_id}/targets", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("target_id_list", [])


def acu_get_target(s: requests.Session, base_url: str, token: str, target_id: str, timeout: int) -> Dict[str, Any]:
    r = api_call("GET", s, f"{base_url.rstrip('/')}/api/v1/targets/{target_id}", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def acu_add_targets(s: requests.Session, base_url: str, token: str, group_id: str, urls: List[str], pt_name: str, timeout: int) -> Tuple[int, Dict[str, Any]]:
    payload = {"targets": [{"address": u, "description": pt_name, "criticality": 10} for u in urls], "groups": [group_id]}
    r = api_call("POST", s, f"{base_url.rstrip('/')}/api/v1/targets/add", headers=acu_headers(token), json=payload, timeout=timeout)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
    return r.status_code, body


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("DOJO_BASE_URL"))
    ap.add_argument("--token", default=os.environ.get("DOJO_API_TOKEN"))
    ap.add_argument("--product-type-id", type=int, required=True)
    ap.add_argument("--acu-base-url", default=os.environ.get("ACU_BASE_URL"))
    ap.add_argument("--acu-token", default=os.environ.get("ACU_API_TOKEN"))
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    started = ts()
    result = build_result(args.product_type_id, started)

    missing = [k for k, v in {"base-url": args.base_url, "token": args.token, "acu-base-url": args.acu_base_url, "acu-token": args.acu_token}.items() if not v]
    if missing or args.timeout <= 0:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "invalid_input", "message": f"missing={missing} timeout={args.timeout}"})
        finalize(result, args.output)
        return EXIT_INPUT

    try:
        dojo_s = make_session(True)
        acu_s = make_session(False)
        pt = dojo_get_product_type(dojo_s, args.base_url, args.token, args.product_type_id, args.timeout)
        pt_name = pt.get("name") or f"PT-{args.product_type_id}"
        products = dojo_get_products_for_pt(dojo_s, args.base_url, args.token, args.product_type_id, args.timeout)
        desired_urls = build_targets_from_products(products)
        result["metrics"]["targets_total"] = len(desired_urls)

        if not desired_urls:
            result["status"] = "skipped"
            result["warnings"].append({"code": "no_targets", "message": "No internet_accessible products found"})
            finalize(result, args.output)
            return 0

        groups = acu_list_groups(acu_s, args.acu_base_url, args.acu_token, args.timeout)
        group = next((g for g in groups if g.get("name") == pt_name), None)
        if not group:
            if args.dry_run:
                group_id = "dry-run-group"
            else:
                created = acu_create_group(acu_s, args.acu_base_url, args.acu_token, pt_name, f"Dojo PT #{args.product_type_id}", args.timeout)
                group_id = created.get("group_id")
                result["metrics"]["group_created"] = 1
        else:
            group_id = group.get("group_id")

        if not group_id:
            raise RuntimeError("group_id_empty")

        existing_urls = set()
        if not args.dry_run:
            for tid in acu_get_group_target_ids(acu_s, args.acu_base_url, args.acu_token, group_id, args.timeout):
                try:
                    t = acu_get_target(acu_s, args.acu_base_url, args.acu_token, tid, args.timeout)
                    addr = t.get("address") or t.get("address_value")
                    if addr:
                        existing_urls.add(addr)
                except Exception as e:
                    result["warnings"].append({"code": "target_lookup_failed", "target_id": tid, "message": str(e)})

        to_create = [u for u in desired_urls if u not in existing_urls]
        result["metrics"]["targets_skipped"] = len(desired_urls) - len(to_create)

        if not to_create:
            result["status"] = "skipped"
            finalize(result, args.output)
            return 0

        if args.dry_run:
            result["metrics"]["targets_created"] = len(to_create)
            finalize(result, args.output)
            return 0

        status_code, body = acu_add_targets(acu_s, args.acu_base_url, args.acu_token, group_id, to_create, pt_name, args.timeout)
        if status_code not in (200, 201):
            raise RuntimeError(f"targets_add_failed:{status_code}:{body}")

        result["metrics"]["targets_created"] = len(to_create)
        finalize(result, args.output)
        return 0

    except PermissionError as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "auth_error", "message": str(e)})
        finalize(result, args.output)
        return EXIT_EXTERNAL_API
    except requests.RequestException as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "external_api", "message": str(e)})
        finalize(result, args.output)
        return EXIT_EXTERNAL_API
    except Exception as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "runtime_error", "message": str(e), "trace": traceback.format_exc(limit=2)})
        finalize(result, args.output)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
