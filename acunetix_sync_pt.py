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


def log(level: str, message: str) -> None:
    print(f"{level}: {message}", file=sys.stderr)


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


def build_targets_from_products(products: List[Dict[str, Any]]) -> List[str]:
    seen = set()
    urls: List[str] = []
    for prod in products:
        if not normalize_bool(prod.get("internet_accessible")):
            continue
        name = (prod.get("name") or "").strip()
        if not name:
            continue
        lower = name.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
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


def acu_targets_add(s: requests.Session, base_url: str, token: str, group_id: str, pt_name: str, urls: List[str], timeout: int) -> Dict[str, Any]:
    targets = [{"addressValue": u, "address": u, "description": pt_name, "web_asset_id": ""} for u in urls]
    payload = {"targets": targets, "groups": [group_id]}
    r = s.post(f"{base_url.rstrip('/')}/api/v1/targets/add", headers=acu_headers(token), json=payload, timeout=timeout)
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
    ap.add_argument("--acu-base-url", default=os.environ.get("ACU_BASE_URL"))
    ap.add_argument("--acu-token", "--acu-api-token", dest="acu_token", default=os.environ.get("ACU_API_TOKEN"))
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--output", help="Optional path to write result JSON")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    missing = []
    for key, val in {"base_url": args.base_url, "token": args.token, "acu_base_url": args.acu_base_url, "acu_token": args.acu_token}.items():
        if not val:
            missing.append(key)
    if missing:
        log("ERROR", f"Missing required args/env: {', '.join(missing)}")
        print(json.dumps({"ok": False, "error": "missing_required", "missing": missing}, ensure_ascii=False))
        return 2
    if args.timeout <= 0:
        print(json.dumps({"ok": False, "error": "invalid_timeout"}, ensure_ascii=False))
        return 2

    debug: Dict[str, Any] = {
        "pt_id": args.pt_id,
        "dojo_base_url": args.base_url,
        "acu_base_url": args.acu_base_url,
        "dry_run": bool(args.dry_run),
        "timeout": args.timeout,
    }

    try:
        dojo_s = make_session(verify=True)
        pt = dojo_get_product_type(dojo_s, args.base_url, args.token, args.pt_id, args.timeout)
        pt_name = pt.get("name") or f"PT-{args.pt_id}"
        debug["pt_name"] = pt_name

        products = dojo_get_products_for_pt(dojo_s, args.base_url, args.token, args.pt_id, args.timeout)
        urls = build_targets_from_products(products)
        debug["dojo_products_total"] = len(products)
        debug["targets_prepared_count"] = len(urls)

        if not urls:
            result = {"ok": False, "reason": "no_internet_accessible_targets", "debug": debug}
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
            print(json.dumps(result, ensure_ascii=False))
            return 1

        acu_s = make_session(verify=False)
        groups = acu_list_groups(acu_s, args.acu_base_url, args.acu_token, args.timeout)
        g = acu_find_group_by_name(groups, pt_name)
        if g:
            group_id = g.get("group_id")
            debug["acu_group_existing"] = g
        elif args.dry_run:
            group_id = "dry-run-group-id"
            debug["acu_group_created"] = "dry_run_only"
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
            debug["acu_group_created"] = created

        debug["acu_group_id"] = group_id

        if args.dry_run:
            result = {"ok": True, "dry_run": True, "product_type_id": args.pt_id, "product_type_name": pt_name, "group_id": group_id, "targets_count": len(urls), "debug": debug}
        else:
            add_result = acu_targets_add(acu_s, args.acu_base_url, args.acu_token, group_id, pt_name, urls, args.timeout)
            debug["acu_add_result"] = add_result
            if add_result["status"] not in (200, 201):
                raise RuntimeError(f"/api/v1/targets/add returned {add_result['status']}: {add_result['response']}")
            result = {"ok": True, "product_type_id": args.pt_id, "product_type_name": pt_name, "group_id": group_id, "targets_count": len(urls), "debug": debug}

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    except Exception as e:
        debug["exception"] = str(e)
        debug["traceback"] = traceback.format_exc()
        err = {"ok": False, "error": "unexpected_error", "details": str(e), "debug": debug}
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(err, f, ensure_ascii=False, indent=2)
        print(json.dumps(err, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
