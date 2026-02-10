#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
process_nmap_ips_for_pt.py

Process Nmap XML for a single DefectDojo Product Type.
Outputs a stable JSON object to stdout and optionally to --output.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Dict, List, Set, Tuple

import requests

STAGE = "WF_B"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def contract(product_type_id: int, status: str, ok: bool = True) -> dict:
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


# ---------- logging ----------

def log(level: str, message: str) -> None:
    print(f"{level}: {message}", file=sys.stderr)


# ---------- CLI ----------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Process Nmap XML for a DefectDojo product_type and update products/description"
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("DOJO_BASE_URL"),
        help="DefectDojo API base URL",
    )
    p.add_argument(
        "--token",
        "--api-token",
        dest="token",
        default=os.environ.get("DOJO_API_TOKEN"),
        help="DefectDojo API token",
    )
    p.add_argument("--product-type-id", "--pt-id", dest="product_type_id", type=int, required=True)
    p.add_argument(
        "--input",
        "--xml-dir",
        dest="xml_dir",
        default=os.environ.get("NMAP_XML_DIR", "/tmp"),
        help="Directory containing nmap_<product_id>.xml",
    )
    p.add_argument("--exclude-ports", default=os.environ.get("EXCLUDE_PORTS", "80,443"))
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    p.add_argument("--output", help="Optional path to write result JSON")
    p.add_argument("--dry-run", action="store_true")
    return p


# ---------- helpers ----------

def looks_like_ip(s: str) -> bool:
    try:
        ip_address(s)
        return True
    except Exception:
        return False


def strip_www(host: str) -> str:
    host = (host or "").strip()
    if host.lower().startswith("www."):
        return host[4:]
    return host


def parse_nmap_xml_for_ips(filename: str, exclude_ports: Set[int]) -> List[Tuple[str, int, str]]:
    if not os.path.exists(filename):
        return []
    try:
        tree = ET.parse(filename)
    except ET.ParseError:
        return []

    root = tree.getroot()
    result: List[Tuple[str, int, str]] = []
    for host in root.findall("host"):
        status_el = host.find("status")
        if status_el is not None and status_el.get("state") != "up":
            continue

        addr = host.find("address")
        if addr is None:
            continue
        ip_addr = addr.get("addr", "")
        if not ip_addr or not looks_like_ip(ip_addr):
            continue

        ports = host.find("ports")
        if ports is None:
            continue

        for p in ports.findall("port"):
            state = p.find("state")
            if state is None or state.get("state") != "open":
                continue
            try:
                portnum = int(p.get("portid", "0"))
            except Exception:
                continue
            if portnum <= 0 or portnum in exclude_ports:
                continue

            svc = p.find("service")
            svc_name = (svc.get("name", "") if svc is not None else "").lower()
            svc_tunnel = (svc.get("tunnel", "") if svc is not None else "").lower()

            if svc_name in {"http", "https", "ssl/http", "http-alt", "https-alt"} or portnum in {
                81,
                3000,
                5000,
                5601,
                8000,
                8080,
                8081,
                8443,
                8888,
                9000,
                9443,
            }:
                proto = "https" if ("ssl" in svc_tunnel or portnum in (8443, 9443)) else "http"
                result.append((ip_addr, portnum, proto))

    return result


# ---------- API client ----------

class DojoClient:
    def __init__(self, base_url: str, token: str, timeout: int, dry_run: bool):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(self.base + path, headers=self.headers, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict) -> dict:
        if self.dry_run:
            return {"_dry_run": True, "path": path, "payload": payload}
        r = requests.post(self.base + path, headers=self.headers, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def patch(self, path: str, payload: dict) -> dict:
        if self.dry_run:
            return {"_dry_run": True, "path": path, "payload": payload}
        r = requests.patch(self.base + path, headers=self.headers, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ---------- core ----------

def process_single_product_type(client: DojoClient, pt_id: int, xml_dir: str, exclude_ports: Set[int]) -> dict:
    summary: Dict[str, object] = contract(pt_id, "success", ok=True)
    summary["metrics"] = {
        "xml_dir": xml_dir,
        "exclude_ports": sorted(list(exclude_ports)),
        "dry_run": client.dry_run,
        "created_ip_products": [],
        "updated_description": False,
    }

    pt = client.get(f"/product_types/{pt_id}/")
    pt_name = pt.get("name", f"pt_{pt_id}")
    summary["metrics"]["product_type_name"] = pt_name

    products: List[dict] = []
    limit = 100
    offset = 0
    while True:
        data = client.get("/products/", params={"prod_type": pt_id, "limit": limit, "offset": offset})
        results = data.get("results", [])
        if not results:
            break
        products.extend(results)
        if not data.get("next"):
            break
        offset += limit
    summary["metrics"]["products_count"] = len(products)

    existing_product_names: Set[str] = set()
    domain_hosts: Set[str] = set()
    for p in products:
        pname = p.get("name", "")
        existing_product_names.add(pname)
        if p.get("internet_accessible"):
            host_part = pname.split(":", 1)[0]
            if host_part and not looks_like_ip(host_part):
                domain_hosts.add(strip_www(host_part))

    candidates: Dict[str, str] = {}
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        xml_path = os.path.join(xml_dir, f"nmap_{pid}.xml")
        for ip_addr, portnum, proto in parse_nmap_xml_for_ips(xml_path, exclude_ports):
            candidates[f"{ip_addr}:{portnum}"] = proto

    created_products: List[str] = []
    ip_lines_set: Set[str] = set()
    for prod_name, proto in sorted(candidates.items()):
        ip_lines_set.add(f"{proto}://{prod_name}, {pt_name}")
        if prod_name in existing_product_names:
            continue
        payload = {
            "name": prod_name,
            "prod_type": pt_id,
            "description": f"Auto-created from nmap XML in {xml_dir}",
            "internet_accessible": True,
        }
        try:
            created = client.post("/products/", payload)
            created_name = created.get("name", prod_name)
            created_products.append(created_name)
            existing_product_names.add(created_name)
        except requests.HTTPError as e:
            created_products.append(f"{prod_name} (error: {e})")

    summary["metrics"]["created_ip_products"] = created_products
    summary["metrics"]["created_ip_products_count"] = len(created_products)

    lines: List[str] = []
    if pt_name and not looks_like_ip(pt_name.split(":", 1)[0]):
        lines.append(f"https://{strip_www(pt_name.split(':', 1)[0])}, {pt_name}")
    for host in sorted(domain_hosts):
        lines.append(f"https://{host}, {pt_name}")
    for line in sorted(ip_lines_set):
        lines.append(line)

    seen: Set[str] = set()
    unique_lines: List[str] = []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            unique_lines.append(line)

    description_text = "Acunetix targets:\n" + "\n".join(unique_lines) if unique_lines else ""
    try:
        client.patch(f"/product_types/{pt_id}/", {"description": description_text})
        summary["metrics"]["updated_description"] = True
    except requests.HTTPError as e:
        summary["ok"] = False
        summary["status"] = "error"
        summary["metrics"]["updated_description"] = False
        summary["errors"].append({"code": "failed_update_description", "details": str(e)})

    if len(created_products) == 0:
        summary["status"] = "skipped"

    summary["timestamps"]["finished_at"] = now_iso()

    return summary


def main() -> int:
    args = build_arg_parser().parse_args()

    if not args.base_url or not args.token:
        log("ERROR", "API token is required (--token or DOJO_API_TOKEN)")
        print(json.dumps(contract(args.product_type_id, "error", ok=False) | {"errors": [{"code": "missing_required"}]}, ensure_ascii=False))
        return 2
    if args.timeout <= 0:
        log("ERROR", "--timeout must be > 0")
        print(json.dumps(contract(args.product_type_id, "error", ok=False) | {"errors": [{"code": "invalid_timeout"}]}, ensure_ascii=False))
        return 2
    if not os.path.isdir(args.xml_dir):
        log("ERROR", f"Input directory does not exist: {args.xml_dir}")
        c = contract(args.product_type_id, "error", ok=False)
        c["errors"].append({"code": "invalid_input_dir", "input": args.xml_dir})
        print(json.dumps(c, ensure_ascii=False))
        return 2

    exclude_ports: Set[int] = {int(p.strip()) for p in args.exclude_ports.split(",") if p.strip().isdigit()}

    try:
        client = DojoClient(args.base_url, args.token, args.timeout, args.dry_run)
        result = process_single_product_type(client, args.product_type_id, args.xml_dir, exclude_ports)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as e:
        err = contract(args.product_type_id, "error", ok=False)
        err["errors"].append({"code": "unexpected_error", "details": str(e)})
        print(json.dumps(err, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
