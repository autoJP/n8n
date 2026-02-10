#!/usr/bin/env python3
"""Synchronize DefectDojo PT endpoints to internet-accessible products and Acunetix targets."""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Set

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP_PORTS = {80, 81, 3000, 5000, 7001, 8000, 8008, 8080, 8081, 8888, 9000}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build host:port products for a Product Type and sync targets to Acunetix")
    p.add_argument("--dojo-base-url", required=True)
    p.add_argument("--dojo-api-token", required=True)
    p.add_argument("--product-type-id", "--pt-id", dest="pt_id", required=True, type=int)
    p.add_argument("--acu-base-url", required=True)
    p.add_argument("--acu-api-token", required=True)
    p.add_argument("--verify-tls", action="store_true", help="Enable TLS verification for Acunetix")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def dojo_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Token {token}", "Accept": "application/json", "Content-Type": "application/json"}


def acu_headers(token: str) -> Dict[str, str]:
    return {"X-Auth": token, "Accept": "application/json", "Content-Type": "application/json"}


def get_paginated(session: requests.Session, url: str, headers: Dict[str, str], key: str = "results") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    next_url: Optional[str] = url
    while next_url:
        resp = session.get(next_url, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        out.extend(data.get(key, []))
        next_url = data.get("next")
    return out


def normalize_host_port(host: str, port: Any) -> Optional[str]:
    if not host:
        return None
    host = host.strip().lower()
    if not host:
        return None
    try:
        p = int(port)
    except Exception:
        return None
    if p <= 0 or p > 65535:
        return None
    return f"{host}:{p}"


def target_from_name(name: str) -> Optional[str]:
    value = (name or "").strip().lower()
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if ":" in value:
        host, port = value.rsplit(":", 1)
        if not port.isdigit():
            return f"https://{value}"
        proto = "http" if int(port) in HTTP_PORTS else "https"
        return f"{proto}://{host}:{port}"
    return f"https://{value}"


def get_or_create_group(s: requests.Session, base: str, token: str, group_name: str, dry_run: bool) -> str:
    r = s.get(f"{base.rstrip('/')}/api/v1/target_groups?limit=100", headers=acu_headers(token), timeout=30)
    r.raise_for_status()
    groups = r.json().get("groups", [])
    for g in groups:
        if g.get("name") == group_name:
            return g.get("group_id", "")
    if dry_run:
        return "dry-run-group-id"
    payload = {"name": group_name, "description": f"Synced from DefectDojo product type '{group_name}'"}
    c = s.post(f"{base.rstrip('/')}/api/v1/target_groups", headers=acu_headers(token), json=payload, timeout=30)
    c.raise_for_status()
    return c.json().get("group_id", "")


def sync_targets(s: requests.Session, base: str, token: str, group_id: str, targets: List[str], pt_name: str, dry_run: bool) -> Dict[str, Any]:
    payload = {
        "targets": [{"address": t, "addressValue": t, "description": pt_name, "criticality": 10, "web_asset_id": ""} for t in targets],
        "groups": [group_id],
    }
    if dry_run:
        return {"status": 200, "response": {"dry_run": True, "payload_preview": payload}}
    r = s.post(f"{base.rstrip('/')}/api/v1/targets/add", headers=acu_headers(token), json=payload, timeout=120)
    body: Any
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    return {"status": r.status_code, "response": body}


def main() -> int:
    args = parse_args()
    dojo = requests.Session()
    acu = requests.Session()
    acu.verify = bool(args.verify_tls)

    report: Dict[str, Any] = {"ok": False, "product_type_id": args.pt_id, "dry_run": args.dry_run}

    try:
        pt = dojo.get(f"{args.dojo_base_url.rstrip('/')}/product_types/{args.pt_id}/", headers=dojo_headers(args.dojo_api_token), timeout=30)
        pt.raise_for_status()
        pt_data = pt.json()
        pt_name = pt_data.get("name") or f"PT-{args.pt_id}"

        products = get_paginated(dojo, f"{args.dojo_base_url.rstrip('/')}/products/?prod_type={args.pt_id}&limit=200", dojo_headers(args.dojo_api_token))
        by_name = {str(p.get('name', '')).lower(): p for p in products}

        candidates: Set[str] = set()
        for product in products:
            pid = product.get("id")
            if not pid:
                continue
            endpoints = get_paginated(dojo, f"{args.dojo_base_url.rstrip('/')}/endpoints/?product={pid}&limit=200", dojo_headers(args.dojo_api_token))
            for ep in endpoints:
                norm = normalize_host_port(str(ep.get("host") or ""), ep.get("port"))
                if norm:
                    candidates.add(norm)

        created: List[str] = []
        for hp in sorted(candidates):
            if hp in by_name:
                continue
            if args.dry_run:
                created.append(hp)
                continue
            payload = {
                "name": hp,
                "description": f"Auto-created from endpoints for PT #{args.pt_id}",
                "prod_type": args.pt_id,
                "internet_accessible": True,
            }
            c = dojo.post(f"{args.dojo_base_url.rstrip('/')}/products/", headers=dojo_headers(args.dojo_api_token), json=payload, timeout=30)
            c.raise_for_status()
            created.append(hp)

        products_after = get_paginated(dojo, f"{args.dojo_base_url.rstrip('/')}/products/?prod_type={args.pt_id}&limit=200", dojo_headers(args.dojo_api_token))
        targets: List[str] = []
        seen: Set[str] = set()
        for p in products_after:
            if not p.get("internet_accessible"):
                continue
            t = target_from_name(str(p.get("name") or ""))
            if t and t not in seen:
                seen.add(t)
                targets.append(t)

        group_id = get_or_create_group(acu, args.acu_base_url, args.acu_api_token, pt_name, args.dry_run)
        sync_result = sync_targets(acu, args.acu_base_url, args.acu_api_token, group_id, targets, pt_name, args.dry_run)

        report.update(
            {
                "ok": sync_result.get("status") in (200, 201),
                "product_type_name": pt_name,
                "products_total_before": len(products),
                "host_port_candidates": len(candidates),
                "products_created": len(created),
                "created_products": created,
                "targets_total": len(targets),
                "group_id": group_id,
                "acunetix_sync": sync_result,
            }
        )
    except Exception as exc:
        report.update({"error": str(exc)})
        print(json.dumps(report, ensure_ascii=False))
        return 1

    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
