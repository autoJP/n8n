#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import sys
import traceback
from typing import Any, Dict, List, Optional

import requests
import urllib3

# Самоподписанный TLS на Acunetix – живём с verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# -------------------- helpers --------------------


def make_session(verify: bool) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    return s


# -------------------- Dojo --------------------


def dojo_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def dojo_get_product_type(
    s: requests.Session,
    base_url: str,
    token: str,
    pt_id: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/product_types/{pt_id}/"
    r = s.get(url, headers=dojo_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def dojo_get_products_for_pt(
    s: requests.Session,
    base_url: str,
    token: str,
    pt_id: int,
) -> List[Dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/products/?prod_type={pt_id}&limit=1000"
    r = s.get(url, headers=dojo_headers(token), timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("results", [])


def normalize_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return False


def build_targets_from_products(products: List[Dict[str, Any]]) -> List[str]:
    """
    Берём только internet_accessible=true, строим URL для Acunetix.
    • если уже http/https – оставляем
    • если IP:port – делаем http://IP:port
    • если домен – https://домен (без дубликатов www.)
    """
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
        else:
            # IP:port?
            if any(ch.isdigit() for ch in lower) and ":" in lower and " " not in lower:
                url = f"http://{name}"
            else:
                bare = lower
                if bare.startswith("www."):
                    bare = bare[4:]
                url = f"https://{bare}"

        if url in seen:
            continue
        seen.add(url)
        urls.append(url)

    return urls


# -------------------- Acunetix --------------------


def acu_headers(token: str) -> Dict[str, str]:
    return {
        "X-Auth": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


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


def acu_create_group(
    s: requests.Session,
    base_url: str,
    token: str,
    name: str,
    desc: str,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/target_groups"
    payload = {"name": name, "description": desc}
    r = s.post(url, headers=acu_headers(token), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def acu_targets_add(
    s: requests.Session,
    base_url: str,
    token: str,
    group_id: str,
    pt_name: str,
    urls: List[str],
) -> Dict[str, Any]:
    """
    POST /api/v1/targets/add
    body:
    {
      "targets": [
        {"addressValue": "...", "address": "...", "description": "<pt_name>", "web_asset_id": ""}
      ],
      "groups": ["<group_id>"]
    }
    """
    url = f"{base_url.rstrip('/')}/api/v1/targets/add"
    targets = [
        {
            "addressValue": u,
            "address": u,
            "description": pt_name,
            "web_asset_id": "",
        }
        for u in urls
    ]
    payload = {"targets": targets, "groups": [group_id]}
    r = s.post(url, headers=acu_headers(token), json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}
    return {"status": r.status_code, "response": body}


# -------------------- main --------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dojo-base-url", required=True)
    ap.add_argument("--dojo-api-token", required=True)
    ap.add_argument("--product-type-id", "--pt-id", dest="pt_id", type=int, required=True)
    ap.add_argument("--acu-base-url", required=True)
    ap.add_argument("--acu-api-token", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    debug: Dict[str, Any] = {
        "pt_id": args.pt_id,
        "dojo_base_url": args.dojo_base_url,
        "acu_base_url": args.acu_base_url,
        "dry_run": bool(args.dry_run),
    }

    try:
        # ---- Dojo ----
        dojo_s = make_session(verify=True)
        pt = dojo_get_product_type(dojo_s, args.dojo_base_url, args.dojo_api_token, args.pt_id)
        pt_name = pt.get("name") or f"PT-{args.pt_id}"
        debug["pt_name"] = pt_name

        products = dojo_get_products_for_pt(dojo_s, args.dojo_base_url, args.dojo_api_token, args.pt_id)
        debug["dojo_products_total"] = len(products)

        urls = build_targets_from_products(products)
        debug["targets_prepared"] = urls
        debug["targets_prepared_count"] = len(urls)

        if not urls:
            print(json.dumps({
                "ok": False,
                "reason": "no_internet_accessible_targets",
                "debug": debug,
            }, ensure_ascii=False))
            return

        # ---- Acunetix ----
        acu_s = make_session(verify=False)

        groups = acu_list_groups(acu_s, args.acu_base_url, args.acu_api_token)
        g = acu_find_group_by_name(groups, pt_name)

        if g:
            group_id = g.get("group_id")
            debug["acu_group_existing"] = g
        else:
            if args.dry_run:
                group_id = "dry-run-group-id"
                debug["acu_group_created"] = "dry_run_only"
            else:
                created = acu_create_group(
                    acu_s,
                    args.acu_base_url,
                    args.acu_api_token,
                    pt_name,
                    f"Dojo PT #{pt.get('id')} ({pt_name})",
                )
                group_id = created.get("group_id")
                debug["acu_group_created"] = created

        debug["acu_group_id"] = group_id

        if args.dry_run:
            debug["acu_add_payload_preview"] = {
                "targets": [
                    {
                        "addressValue": u,
                        "address": u,
                        "description": pt_name,
                        "web_asset_id": "",
                    }
                    for u in urls
                ],
                "groups": [group_id],
            }
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "debug": debug,
            }, ensure_ascii=False))
            return

        add_result = acu_targets_add(
            acu_s,
            args.acu_base_url,
            args.acu_api_token,
            group_id,
            pt_name,
            urls,
        )
        debug["acu_add_result"] = add_result

        if add_result["status"] not in (200, 201):
            raise RuntimeError(f"/api/v1/targets/add returned {add_result['status']}: {add_result['response']}")

        print(json.dumps({
            "ok": True,
            "product_type_id": args.pt_id,
            "product_type_name": pt_name,
            "group_id": group_id,
            "targets_count": len(urls),
            "debug": debug,
        }, ensure_ascii=False))

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

