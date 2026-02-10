#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any, Dict, List, Set, Tuple

import requests

EXIT_RUNTIME = 1
EXIT_INPUT = 2
EXIT_EXTERNAL_API = 3
STAGE = "WF_C.process_nmap_ips_for_pt"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def result_base(pt_id: int, started: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "stage": STAGE,
        "product_type_id": pt_id,
        "timestamps": {"start": started, "end": None},
        "metrics": {
            "products_total": 0,
            "targets_total": 0,
            "targets_created": 0,
            "targets_skipped": 0,
            "description_updated": 0,
        },
        "errors": [],
        "warnings": [],
    }


def write_result(res: Dict[str, Any], output: str | None) -> None:
    res["timestamps"]["end"] = now()
    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False))


def looks_like_ip(s: str) -> bool:
    try:
        ip_address(s)
        return True
    except Exception:
        return False


def strip_www(host: str) -> str:
    return host[4:] if host.lower().startswith("www.") else host


def parse_nmap_xml_for_ips(filename: str, exclude_ports: Set[int]) -> List[Tuple[str, int, str]]:
    if not os.path.exists(filename):
        return []
    try:
        root = ET.parse(filename).getroot()
    except ET.ParseError:
        return []

    result: List[Tuple[str, int, str]] = []
    for host in root.findall("host"):
        status = host.find("status")
        if status is not None and status.get("state") != "up":
            continue
        addr = host.find("address")
        ip_addr = addr.get("addr", "") if addr is not None else ""
        if not ip_addr or not looks_like_ip(ip_addr):
            continue
        for p in host.findall("ports/port"):
            state = p.find("state")
            if state is None or state.get("state") != "open":
                continue
            try:
                port = int(p.get("portid", "0"))
            except Exception:
                continue
            if port <= 0 or port in exclude_ports:
                continue
            svc = p.find("service")
            svc_name = (svc.get("name", "") if svc is not None else "").lower()
            svc_tunnel = (svc.get("tunnel", "") if svc is not None else "").lower()
            httpish = svc_name in {"http", "https", "ssl/http", "http-alt", "https-alt"} or port in {81, 3000, 5000, 5601, 8000, 8080, 8081, 8443, 8888, 9000, 9443}
            if httpish:
                proto = "https" if ("ssl" in svc_tunnel or port in {8443, 9443}) else "http"
                result.append((ip_addr, port, proto))
    return result


class Dojo:
    def __init__(self, base_url: str, token: str, timeout: int, dry_run: bool):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self.headers = {"Authorization": f"Token {token}", "Accept": "application/json", "Content-Type": "application/json"}

    def _req(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        for i in range(3):
            r = requests.request(method, self.base + path, headers=self.headers, timeout=self.timeout, **kwargs)
            if r.status_code == 429 and i < 2:
                time.sleep(2 ** i)
                continue
            if r.status_code >= 500 and i < 2:
                time.sleep(2 ** i)
                continue
            if r.status_code in (401, 403):
                raise PermissionError(f"auth_error:{r.status_code}")
            return r
        return r

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self._req("GET", path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict) -> dict:
        if self.dry_run:
            return {"_dry_run": True}
        r = self._req("POST", path, json=payload)
        r.raise_for_status()
        return r.json()

    def patch(self, path: str, payload: dict) -> dict:
        if self.dry_run:
            return {"_dry_run": True}
        r = self._req("PATCH", path, json=payload)
        r.raise_for_status()
        return r.json()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.environ.get("DOJO_BASE_URL", "http://localhost:8080/api/v2"))
    p.add_argument("--token", default=os.environ.get("DOJO_API_TOKEN"))
    p.add_argument("--product-type-id", type=int, required=True)
    p.add_argument("--input", default=os.environ.get("NMAP_XML_DIR", "/tmp"))
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--exclude-ports", default=os.environ.get("EXCLUDE_PORTS", "80,443"))
    p.add_argument("--output")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    started = now()
    result = result_base(args.product_type_id, started)

    if not args.token or args.timeout <= 0 or not os.path.isdir(args.input):
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "invalid_input", "message": "token/timeout/input invalid"})
        write_result(result, args.output)
        return EXIT_INPUT

    exclude_ports: Set[int] = {int(p.strip()) for p in args.exclude_ports.split(",") if p.strip().isdigit()}

    try:
        dojo = Dojo(args.base_url, args.token, args.timeout, args.dry_run)
        pt = dojo.get(f"/product_types/{args.product_type_id}/")
        pt_name = pt.get("name", f"pt_{args.product_type_id}")

        products: List[dict] = []
        offset, limit = 0, 100
        while True:
            data = dojo.get("/products/", params={"prod_type": args.product_type_id, "limit": limit, "offset": offset})
            batch = data.get("results", [])
            if not batch:
                break
            products.extend(batch)
            if not data.get("next"):
                break
            offset += limit

        result["metrics"]["products_total"] = len(products)
        existing_names: Set[str] = {p.get("name", "") for p in products}

        domain_hosts: Set[str] = set()
        candidates: Dict[str, str] = {}
        for p in products:
            pname = p.get("name", "")
            if p.get("internet_accessible"):
                host = pname.split(":", 1)[0]
                if host and not looks_like_ip(host):
                    domain_hosts.add(strip_www(host))
            pid = p.get("id")
            if not pid:
                continue
            for ip, port, proto in parse_nmap_xml_for_ips(os.path.join(args.input, f"nmap_{pid}.xml"), exclude_ports):
                candidates[f"{ip}:{port}"] = proto

        result["metrics"]["targets_total"] = len(candidates)
        for name in sorted(candidates):
            if name in existing_names:
                result["metrics"]["targets_skipped"] += 1
                continue
            payload = {"name": name, "prod_type": args.product_type_id, "description": f"Auto-created from nmap XML in {args.input}", "internet_accessible": True}
            dojo.post("/products/", payload)
            existing_names.add(name)
            result["metrics"]["targets_created"] += 1

        lines: List[str] = [f"https://{strip_www(pt_name.split(':', 1)[0])}, {pt_name}"] if pt_name and not looks_like_ip(pt_name.split(":", 1)[0]) else []
        lines.extend([f"https://{h}, {pt_name}" for h in sorted(domain_hosts)])
        lines.extend([f"{proto}://{prod}, {pt_name}" for prod, proto in sorted(candidates.items())])
        deduped = list(dict.fromkeys([l for l in lines if l]))
        description = "Acunetix targets:\n" + "\n".join(deduped) if deduped else ""

        if (pt.get("description") or "") != description:
            dojo.patch(f"/product_types/{args.product_type_id}/", {"description": description})
            result["metrics"]["description_updated"] = 1
        else:
            result["status"] = "skipped" if result["metrics"]["targets_created"] == 0 else "ok"

        write_result(result, args.output)
        return 0

    except PermissionError as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "auth_error", "message": str(e)})
        write_result(result, args.output)
        return EXIT_EXTERNAL_API
    except requests.RequestException as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "external_api", "message": str(e)})
        write_result(result, args.output)
        return EXIT_EXTERNAL_API
    except Exception as e:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append({"code": "runtime_error", "message": str(e)})
        write_result(result, args.output)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
