#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
process_nmap_ips_for_pt.py

Process Nmap XML for a *single* DefectDojo Product Type:
 - list all products in that product_type
 - collect web IP:port targets from /tmp/nmap_<product_id>.xml (or --xml-dir)
 - skip ports in EXCLUDE_PORTS (default 80,443)
 - create new Product entries only for IP:port targets that do not already exist in that product_type
 - set internet_accessible = true for created products
 - update product_type.description with unique list of Acunetix targets:
     - main domain (canonical, without www), one line
     - each internet_accessible non-IP product (canonical host without www)
     - each IP:port target as <proto>://IP:port, <product_type.name>

Suitable for use from n8n via Execute Command with JSON summary on stdout.
"""

import os
import sys
import json
import argparse
from typing import List, Tuple, Set, Dict
import requests
import xml.etree.ElementTree as ET
from ipaddress import ip_address

API_TIMEOUT = 30


# ---------- CLI / ENV ----------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Process Nmap XML for a single DefectDojo product_type (IP-only products) and update product_type description"
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("DOJO_BASE_URL", "http://localhost:8080/api/v2"),
        help="DefectDojo API base URL, e.g. http://host:8080/api/v2",
    )
    p.add_argument(
        "--api-token",
        default=os.environ.get("DOJO_API_TOKEN"),
        help="DefectDojo API token (or set DOJO_API_TOKEN env)",
    )
    p.add_argument(
        "--product-type-id",
        "--pt-id",
        dest="product_type_id",
        type=int,
        required=True,
        help="ID of product_type to process (required)",
    )
    p.add_argument(
        "--xml-dir",
        default=os.environ.get("NMAP_XML_DIR", "/tmp"),
        help="Directory containing nmap_<product_id>.xml files (default /tmp)",
    )
    p.add_argument(
        "--exclude-ports",
        default=os.environ.get("EXCLUDE_PORTS", "80,443"),
        help="Comma-separated ports to exclude for IP:port products (default 80,443)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write to the API; only simulate actions",
    )
    return p


parser = build_arg_parser()
args = parser.parse_args()

DOJO_BASE = args.base_url.rstrip("/")
API_TOKEN = args.api_token
XML_DIR = args.xml_dir
EXCLUDE_PORTS: Set[int] = {int(p.strip()) for p in args.exclude_ports.split(",") if p.strip().isdigit()}
DRY_RUN = args.dry_run
PT_ID = args.product_type_id

if not API_TOKEN:
    print("ERROR: DOJO_API_TOKEN not set and --api-token not provided", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "Authorization": f"Token {API_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------- HTTP helpers ----------

def safe_get(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, headers=HEADERS, params=params, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def safe_post(url: str, payload: dict) -> dict:
    if DRY_RUN:
        return {"_dry_run": True, "url": url, "payload": payload}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def safe_patch(url: str, payload: dict) -> dict:
    if DRY_RUN:
        return {"_dry_run": True, "url": url, "payload": payload}
    r = requests.patch(url, headers=HEADERS, json=payload, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def api_get(path: str, params: dict | None = None) -> dict:
    return safe_get(DOJO_BASE + path, params=params)


def api_post(path: str, payload: dict) -> dict:
    return safe_post(DOJO_BASE + path, payload)


def api_patch(path: str, payload: dict) -> dict:
    return safe_patch(DOJO_BASE + path, payload)


# ---------- Helpers ----------

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
    """
    Parse nmap xml and return list of (ip, port, proto) for open web-like services.
    Skip ports in exclude_ports.
    """
    if not os.path.exists(filename):
        return []
    try:
        tree = ET.parse(filename)
    except ET.ParseError:
        return []
    root = tree.getroot()
    result: List[Tuple[str, int, str]] = []

    for host in root.findall("host"):
        ip_addr = None
        for addr_el in host.findall("address"):
            addr = addr_el.get("addr")
            if addr:
                ip_addr = addr
                break
        if not ip_addr:
            continue

        ports_el = host.find("ports")
        if ports_el is None:
            continue

        for port_el in ports_el.findall("port"):
            state = port_el.find("state")
            if state is None or state.get("state") != "open":
                continue
            try:
                portnum = int(port_el.get("portid", "0"))
            except ValueError:
                continue
            if portnum in exclude_ports:
                continue
            svc = port_el.find("service")
            svc_name = (svc.get("name") if svc is not None else "") or ""
            svc_tunnel = (svc.get("tunnel") if svc is not None else "") or ""
            # heuristic: web-like service if name has http, or tunnel=ssl, or known web-ish ports
            if "http" in svc_name.lower() or "ssl" in svc_tunnel.lower() or portnum in {8000, 8008, 8080, 8443, 8888, 9000, 9443}:
                proto = "https" if ("ssl" in svc_tunnel.lower() or portnum in (8443, 9443)) else "http"
                result.append((ip_addr, portnum, proto))

    return result


# ---------- Core processing for single product_type ----------

def process_single_product_type(pt_id: int) -> dict:
    summary: Dict[str, object] = {
        "product_type_id": pt_id,
        "xml_dir": XML_DIR,
        "exclude_ports": sorted(list(EXCLUDE_PORTS)),
        "dry_run": DRY_RUN,
        "created_ip_products": [],
        "updated_description": False,
    }

    # 1) Load product_type
    pt = api_get(f"/product_types/{pt_id}/")
    pt_name = pt.get("name", f"pt_{pt_id}")
    summary["product_type_name"] = pt_name

    # 2) Load all products in this product_type
    products: List[dict] = []
    limit = 100
    offset = 0
    while True:
        params = {"prod_type": pt_id, "limit": limit, "offset": offset}
        data = api_get("/products/", params=params)
        results = data.get("results", [])
        if not results:
            break
        products.extend(results)
        if not data.get("next"):
            break
        offset += limit

    summary["products_count"] = len(products)

    # 3) Build initial sets
    existing_product_names: Set[str] = set()
    domain_hosts: Set[str] = set()  # canonical (without www) hosts for domain targets

    for p in products:
        pname = p.get("name", "")
        existing_product_names.add(pname)

        if p.get("internet_accessible"):
            host_part = pname.split(":", 1)[0]
            if host_part and not looks_like_ip(host_part):
                canonical = strip_www(host_part)
                domain_hosts.add(canonical)

    # 4) Collect IP:port candidates from all XMLs of products in this PT
    candidates: Dict[str, str] = {}  # prod_name -> proto
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        xml_path = os.path.join(XML_DIR, f"nmap_{pid}.xml")
        if not os.path.exists(xml_path):
            continue
        targets = parse_nmap_xml_for_ips(xml_path, EXCLUDE_PORTS)
        for ip_addr, portnum, proto in targets:
            prod_name = f"{ip_addr}:{portnum}"
            candidates[prod_name] = proto  # last one wins

    # 5) Create missing products and collect IP target lines
    created_products: List[str] = []
    ip_lines_set: Set[str] = set()

    for prod_name, proto in sorted(candidates.items()):
        ip_lines_set.add(f"{proto}://{prod_name}, {pt_name}")
        if prod_name in existing_product_names:
            continue
        payload = {
            "name": prod_name,
            "prod_type": pt_id,
            "description": f"Auto-created from nmap XML in {XML_DIR}",
            "internet_accessible": True,
        }
        try:
            created = api_post("/products/", payload)
            created_name = created.get("name", prod_name)
            created_products.append(created_name)
            existing_product_names.add(created_name)
        except requests.HTTPError as e:
            created_products.append(f"{prod_name} (error: {e})")

    summary["created_ip_products"] = created_products
    summary["created_ip_products_count"] = len(created_products)

    # 6) Build description lines: main domain + domains (canonical, without www) + IP:ports
    lines: List[str] = []

    # main PT name: if not IP, add only https:// form, canonicalized for host, but label = pt_name
    if pt_name and not looks_like_ip(pt_name.split(":", 1)[0]):
        host_part = pt_name.split(":", 1)[0]
        canonical_pt = strip_www(host_part)
        lines.append(f"https://{canonical_pt}, {pt_name}")

    # products with internet_accessible and non-IP names (canonical hosts, no www)
    for host in sorted(domain_hosts):
        lines.append(f"https://{host}, {pt_name}")

    # IP:port lines
    for line in sorted(ip_lines_set):
        lines.append(line)

    # dedupe preserving order
    seen: Set[str] = set()
    unique_lines: List[str] = []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            unique_lines.append(line)

    if unique_lines:
        description_text = "Acunetix targets:\n" + "\n".join(unique_lines)
    else:
        description_text = ""

    # 7) Patch product_type.description
    try:
        api_patch(f"/product_types/{pt_id}/", {"description": description_text})
        summary["updated_description"] = True
    except requests.HTTPError as e:
        summary["updated_description"] = False
        summary["error"] = f"failed_update_description: {e}"

    return summary


def main() -> None:
    try:
        result = process_single_product_type(PT_ID)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

