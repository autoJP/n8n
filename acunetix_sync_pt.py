#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STAGE = "WF_C"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_result(product_type_id: int, status: str, ok: bool = True) -> Dict[str, Any]:
    ts = now_iso()
    return {
        "ok": ok,
        "stage": STAGE,
        "product_type_id": product_type_id,
        "status": status,
        "metrics": {},
        "errors": [],
        "warnings": [],
        "timestamps": {"started_at": ts, "finished_at": ts},
    }


def make_session(verify: bool) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    return s


def dojo_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def dojo_get_product_type(s: requests.Session, base_url: str, token: str, pt_id: int, timeout: int) -> Dict[str, Any]:
    r = s.get(f"{base_url.rstrip('/')}/product_types/{pt_id}/", headers=dojo_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def dojo_get_products_for_pt(s: requests.Session, base_url: str, token: str, pt_id: int, timeout: int) -> List[Dict[str, Any]]:
    r = s.get(f"{base_url.rstrip('/')}/products/?prod_type={pt_id}&limit=1000", headers=dojo_headers(token), timeout=timeout)
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


def normalize_target_url(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return ""
    if n.startswith("http://") or n.startswith("https://"):
        return n
    if any(ch.isdigit() for ch in n) and ":" in n and " " not in n:
        return f"http://{n}"
    bare = n[4:] if n.startswith("www.") else n
    return f"https://{bare}"


def build_targets_from_products(products: List[Dict[str, Any]]) -> List[str]:
    seen: Set[str] = set()
    urls: List[str] = []
    for prod in products:
        if not normalize_bool(prod.get("internet_accessible")):
            continue
        url = normalize_target_url(prod.get("name") or "")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def acu_headers(token: str) -> Dict[str, str]:
    return {
        "X-Auth": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def acu_list_groups(s: requests.Session, base_url: str, token: str, timeout: int) -> List[Dict[str, Any]]:
    r = s.get(f"{base_url.rstrip('/')}/api/v1/target_groups?limit=100", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("groups", [])


def acu_find_group_by_name(groups: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for g in groups:
        if g.get("name") == name:
            return g
    return None


def acu_create_group(s: requests.Session, base_url: str, token: str, name: str, desc: str, timeout: int) -> Dict[str, Any]:
    payload = {"name": name, "description": desc}
    r = s.post(f"{base_url.rstrip('/')}/api/v1/target_groups", headers=acu_headers(token), json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def acu_group_target_ids(s: requests.Session, base_url: str, token: str, group_id: str, timeout: int) -> List[str]:
    r = s.get(f"{base_url.rstrip('/')}/api/v1/target_groups/{group_id}/targets", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("target_id_list", [])


def acu_get_target(s: requests.Session, base_url: str, token: str, target_id: str, timeout: int) -> Dict[str, Any]:
    r = s.get(f"{base_url.rstrip('/')}/api/v1/targets/{target_id}", headers=acu_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()


def acu_targets_add(s: requests.Session, base_url: str, token: str, group_id: str, pt_name: str, urls: List[str], timeout: int) -> Dict[str, Any]:
    targets = [{"addressValue": u, "address": u, "description": pt_name, "web_asset_id": ""} for u in urls]
    payload = {"targets": targets, "groups": [group_id]}
    r = s.post(f"{base_url.rstrip('/')}/api/v1/targets/add", headers=acu_headers(token), json=payload, timeout=timeout)
    body: Dict[str, Any]
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}
    return {"status": r.status_code, "response": body}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", "--dojo-base-url", dest="base_url", default=os.environ.get("DOJO_BASE_URL"))
    ap.add_argument("--token", "--dojo-api-token", dest="token", default=os.environ.get("DOJO_API_TOKEN"))
    ap.add_argument("--product-type-id", "--pt-id", dest="pt_id", type=int, required=True)
    ap.add_argument("--acu-base-url", default=os.environ.get("ACUNETIX_BASE_URL"))
    ap.add_argument("--acu-token", "--acu-api-token", dest="acu_token", default=os.environ.get("ACUNETIX_API_TOKEN"))
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--output", help="Optional path to write result JSON")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    result = make_result(args.pt_id, "error", ok=False)
    result["timestamps"]["started_at"] = now_iso()

    missing = []
    for key, val in {"base_url": args.base_url, "token": args.token, "acu_base_url": args.acu_base_url, "acu_token": args.acu_token}.items():
        if not val:
            missing.append(key)
    if missing:
        result["errors"].append({"code": "missing_required", "details": missing})
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    if args.timeout <= 0:
        result["errors"].append({"code": "invalid_timeout"})
        print(json.dumps(result, ensure_ascii=False))
        return 2

    try:
        dojo_s = make_session(verify=True)
        pt = dojo_get_product_type(dojo_s, args.base_url, args.token, args.pt_id, args.timeout)
        pt_name = pt.get("name") or f"PT-{args.pt_id}"

        products = dojo_get_products_for_pt(dojo_s, args.base_url, args.token, args.pt_id, args.timeout)
        urls = build_targets_from_products(products)
        result["metrics"].update({
            "dojo_products_total": len(products),
            "targets_prepared_count": len(urls),
        })

        if not urls:
            result["ok"] = True
            result["status"] = "skipped"
            result["warnings"].append("no_internet_accessible_targets")
            result["timestamps"]["finished_at"] = now_iso()
            print(json.dumps(result, ensure_ascii=False))
            return 0

        acu_s = make_session(verify=False)
        groups = acu_list_groups(acu_s, args.acu_base_url, args.acu_token, args.timeout)
        g = acu_find_group_by_name(groups, pt_name)

        group_created = False
        if g:
            group_id = g.get("group_id")
        elif args.dry_run:
            group_id = "dry-run-group-id"
            group_created = True
        else:
            created = acu_create_group(
                acu_s,
                args.acu_base_url,
                args.acu_token,
                pt_name,
                f"Dojo PT #{pt.get('id')} ({pt_name})",
                args.timeout,
            )
            group_id = created.get("group_id")
            group_created = True

        if not group_id:
            raise RuntimeError("group_id_resolution_failed")

        desired = set(urls)
        existing: Set[str] = set()
        target_ids = [] if args.dry_run else acu_group_target_ids(acu_s, args.acu_base_url, args.acu_token, group_id, args.timeout)
        for tid in target_ids:
            t = acu_get_target(acu_s, args.acu_base_url, args.acu_token, tid, args.timeout)
            addr = t.get("address") or t.get("target") or ""
            norm = normalize_target_url(addr)
            if norm:
                existing.add(norm)

        to_add = sorted(desired - existing)
        skipped_existing = sorted(desired & existing)

        result["metrics"].update({
            "group_id": group_id,
            "group_created": group_created,
            "targets_existing_count": len(existing),
            "targets_skipped_count": len(skipped_existing),
            "targets_to_add_count": len(to_add),
        })

        if args.dry_run:
            result["status"] = "success"
            result["metrics"]["dry_run"] = True
        elif not to_add:
            result["status"] = "skipped"
            result["metrics"]["reason"] = "no_changes"
        else:
            add_result = acu_targets_add(acu_s, args.acu_base_url, args.acu_token, group_id, pt_name, to_add, args.timeout)
            if add_result["status"] not in (200, 201):
                raise RuntimeError(f"targets_add_failed: {add_result['status']}")
            result["status"] = "success"
            result["metrics"]["targets_added_count"] = len(to_add)

        result["ok"] = True
        result["timestamps"]["finished_at"] = now_iso()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    except Exception as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "unexpected_error", "details": str(e), "traceback": traceback.format_exc()})
        result["timestamps"]["finished_at"] = now_iso()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
