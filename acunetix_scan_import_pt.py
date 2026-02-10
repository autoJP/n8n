#!/usr/bin/env python3
"""Run Acunetix scans for Product Type targets and import results into DefectDojo."""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP_PORTS = {80, 81, 3000, 5000, 7001, 8000, 8008, 8080, 8081, 8888, 9000}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start Acunetix scans for a PT, poll completion, export report, import into DefectDojo")
    p.add_argument("--dojo-base-url", required=True)
    p.add_argument("--dojo-api-token", required=True)
    p.add_argument("--product-type-id", "--pt-id", dest="pt_id", required=True, type=int)
    p.add_argument("--acu-base-url", required=True)
    p.add_argument("--acu-api-token", required=True)
    p.add_argument("--scan-profile-id", default="11111111-1111-1111-1111-111111111111")
    p.add_argument("--poll-interval", type=int, default=30)
    p.add_argument("--timeout-seconds", type=int, default=3600)
    p.add_argument("--verify-tls", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def dojo_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Token {token}", "Accept": "application/json"}


def acu_headers(token: str) -> Dict[str, str]:
    return {"X-Auth": token, "Accept": "application/json", "Content-Type": "application/json"}


def get_products(session: requests.Session, base: str, token: str, pt_id: int) -> List[Dict[str, Any]]:
    url = f"{base.rstrip('/')}/products/?prod_type={pt_id}&limit=500"
    r = session.get(url, headers=dojo_headers(token), timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])


def build_url(name: str) -> Optional[str]:
    v = (name or "").strip().lower()
    if not v:
        return None
    if v.startswith("http://") or v.startswith("https://"):
        return v
    if ":" in v:
        host, port = v.rsplit(":", 1)
        if port.isdigit() and int(port) in HTTP_PORTS:
            return f"http://{host}:{port}"
        if port.isdigit():
            return f"https://{host}:{port}"
    return f"https://{v}"


def list_acu_targets(session: requests.Session, base: str, token: str) -> List[Dict[str, Any]]:
    r = session.get(f"{base.rstrip('/')}/api/v1/targets?l=200", headers=acu_headers(token), timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("targets", [])


def create_scan(session: requests.Session, base: str, token: str, target_id: str, profile_id: str) -> str:
    payload = {"target_id": target_id, "profile_id": profile_id, "schedule": {"disable": False, "start_date": None, "time_sensitive": False}}
    r = session.post(f"{base.rstrip('/')}/api/v1/scans", headers=acu_headers(token), json=payload, timeout=60)
    r.raise_for_status()
    return r.json().get("scan_id", "")


def wait_scan(session: requests.Session, base: str, token: str, scan_id: str, poll: int, timeout_sec: int) -> Tuple[str, Dict[str, Any]]:
    started = time.time()
    last: Dict[str, Any] = {}
    while True:
        r = session.get(f"{base.rstrip('/')}/api/v1/scans/{scan_id}", headers=acu_headers(token), timeout=30)
        r.raise_for_status()
        last = r.json()
        status = ((last.get("current_session") or {}).get("status") or last.get("status") or "unknown").lower()
        if status in {"completed", "processing", "finished"}:
            return "completed", last
        if status in {"failed", "aborted"}:
            return "failed", last
        if time.time() - started > timeout_sec:
            return "timeout", last
        time.sleep(max(5, poll))


def download_report(session: requests.Session, base: str, token: str, scan_id: str, out_file: Path) -> None:
    # NOTE: endpoint can vary between Acunetix versions.
    r = session.get(f"{base.rstrip('/')}/api/v1/scans/{scan_id}/results", headers=acu_headers(token), timeout=60)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise RuntimeError(f"no results for scan {scan_id}")
    result_id = results[0].get("result_id")
    if not result_id:
        raise RuntimeError("result_id missing")

    rep = session.post(
        f"{base.rstrip('/')}/api/v1/reports",
        headers=acu_headers(token),
        json={"template_id": "11111111-1111-1111-1111-111111111111", "source": {"list_type": "scans", "id_list": [scan_id]}, "report_name": f"scan-{scan_id}"},
        timeout=60,
    )
    rep.raise_for_status()
    download = rep.json().get("download") or rep.json().get("report_id")
    if not download:
        # fallback raw vulnerabilities
        v = session.get(f"{base.rstrip('/')}/api/v1/scans/{scan_id}/results/{result_id}/vulnerabilities", headers=acu_headers(token), timeout=60)
        v.raise_for_status()
        out_file.write_text(json.dumps(v.json(), ensure_ascii=False), encoding="utf-8")
        return

    url = download if str(download).startswith("http") else f"{base.rstrip('/')}{download}"
    d = session.get(url, headers=acu_headers(token), timeout=120)
    d.raise_for_status()
    out_file.write_bytes(d.content)


def import_to_dojo(session: requests.Session, base: str, token: str, product_id: int, report_path: Path) -> Dict[str, Any]:
    files = {"file": (report_path.name, report_path.read_bytes())}
    data = {
        "scan_type": "Acunetix Scan",
        "minimum_severity": "Info",
        "active": "true",
        "verified": "false",
        "close_old_findings": "false",
        "product": str(product_id),
        "engagement_name": f"Acunetix import for product {product_id}",
        "test_title": f"Acunetix automated import {int(time.time())}",
    }
    headers = {"Authorization": f"Token {token}"}
    r = session.post(f"{base.rstrip('/')}/import-scan/", headers=headers, data=data, files=files, timeout=120)
    r.raise_for_status()
    return r.json() if r.text else {"status": r.status_code}


def main() -> int:
    args = parse_args()
    dojo = requests.Session()
    acu = requests.Session()
    acu.verify = bool(args.verify_tls)
    report: Dict[str, Any] = {"ok": False, "product_type_id": args.pt_id, "dry_run": args.dry_run, "scans": []}

    try:
        products = get_products(dojo, args.dojo_base_url, args.dojo_api_token, args.pt_id)
        targets = list_acu_targets(acu, args.acu_base_url, args.acu_api_token)
        target_by_address = {str(t.get("address", "")).lower(): t for t in targets}

        for product in products:
            if not product.get("internet_accessible"):
                continue
            product_id = int(product["id"])
            address = build_url(str(product.get("name") or ""))
            if not address:
                continue
            target = target_by_address.get(address.lower())
            if not target:
                report["scans"].append({"product_id": product_id, "address": address, "status": "skipped", "reason": "target_not_found_in_acunetix"})
                continue
            target_id = target.get("target_id")
            if not target_id:
                report["scans"].append({"product_id": product_id, "address": address, "status": "skipped", "reason": "target_id_missing"})
                continue

            if args.dry_run:
                report["scans"].append({"product_id": product_id, "address": address, "status": "dry_run"})
                continue

            scan_id = create_scan(acu, args.acu_base_url, args.acu_api_token, target_id, args.scan_profile_id)
            state, payload = wait_scan(acu, args.acu_base_url, args.acu_api_token, scan_id, args.poll_interval, args.timeout_seconds)
            item = {"product_id": product_id, "address": address, "scan_id": scan_id, "status": state}

            if state != "completed":
                item["details"] = payload
                report["scans"].append(item)
                continue

            with tempfile.NamedTemporaryFile(prefix="acunetix_", suffix=".json", dir="/tmp", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                download_report(acu, args.acu_base_url, args.acu_api_token, scan_id, tmp_path)
                dojo_import = import_to_dojo(dojo, args.dojo_base_url, args.dojo_api_token, product_id, tmp_path)
                item["dojo_import"] = dojo_import
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
            report["scans"].append(item)

        failures = [s for s in report["scans"] if s.get("status") in {"failed", "timeout"}]
        report["ok"] = len(failures) == 0
        report["failed_count"] = len(failures)
    except Exception as exc:
        report.update({"error": str(exc)})
        print(json.dumps(report, ensure_ascii=False))
        return 1

    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
