#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
enum_subs_auto.py — subdomain enum using only assetfinder and sublist3r.
Outputs:
 - plain lines to stdout by default (one host per line)
 - --json-output -> compact JSON to stdout (useful for n8n)
Writes artifacts:
 - <out_dir>/<domain>.subs.txt
 - <out_dir>/<domain>.subs.json
Options:
 - --resolve  -> keep only resolvable hosts (A/AAAA)
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import List, Set, Tuple

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}\.?$")

def norm_domain(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0].split(":")[0]
    return s.rstrip(".")

def is_valid_domain(name: str) -> bool:
    return bool(DOMAIN_RE.match(name.strip()))

def is_sub_of(sub: str, root: str) -> bool:
    sub = norm_domain(sub)
    root = norm_domain(root)
    return sub == root or sub.endswith("." + root)

def run_cmd(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=timeout, check=False, text=True)
        return res.returncode, res.stdout or "", res.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"TimeoutExpired: {e}"
    except Exception as e:
        return 1, "", f"Exception: {e}"

def resolve_host(host: str, timeout: float = 2.0) -> Tuple[bool, List[str]]:
    host = norm_domain(host)
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    ips = []
    try:
        infos = socket.getaddrinfo(host, None)
        for fam, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if ip not in ips:
                ips.append(ip)
        return (len(ips) > 0, ips)
    except Exception:
        return (False, [])
    finally:
        socket.setdefaulttimeout(old)

def from_assetfinder(domain: str, timeout: int) -> Set[str]:
    subs = set()
    exe = shutil.which("assetfinder")
    if not exe:
        return subs
    rc, out, err = run_cmd([exe, "--subs-only", domain], timeout=timeout)
    if rc == 0 and out:
        for line in out.splitlines():
            s = norm_domain(line)
            if is_valid_domain(s) and is_sub_of(s, domain):
                subs.add(s)
    return subs

def from_sublist3r(domain: str, timeout: int) -> Set[str]:
    """
    Запускает Sublist3r внешним процессом и игнорирует stderr полностью.
    Пытается найти бинарь 'sublist3r' в PATH, иначе использует 'python3 -m sublist3r'.
    Возвращает множество найденных поддоменов (нормализованных).
    Ошибки/traceback-ы дочерних процессов НЕ логируются и НЕ выводятся.
    """
    subs = set()

    # 1) попытка найти консольный скрипт
    bin_path = shutil.which("sublist3r")
    if bin_path:
        rc, out, err = run_cmd([bin_path, "-d", domain, "-n", "-t", "50"], timeout=timeout)
    else:
        # 2) попробовать python3 -m sublist3r
        python = shutil.which("python3") or shutil.which("python")
        if not python:
            return subs
        rc, out, err = run_cmd([python, "-m", "sublist3r", "-d", domain, "-n", "-t", "50"], timeout=timeout)

    # Парсим stdout; stderr полностью игнорируем (не печатаем, не логируем)
    if rc == 0 and out:
        for line in out.splitlines():
            s = norm_domain(line)
            if is_valid_domain(s) and is_sub_of(s, domain):
                subs.add(s)

    # В случае ошибок просто возвращаем то, что успели собрать (или пустой set)
    return subs


def write_text(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)

def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True, help="Root domain")
    p.add_argument("--sources", default="assetfinder,sublist3r",
                   help="Comma list: assetfinder,sublist3r (default both)")
    p.add_argument("--per-source-timeout", type=int, default=120,
                   help="Timeout per external source (seconds)")
    p.add_argument("--resolve", action="store_true", help="Keep only resolvable hosts")
    p.add_argument("--out-dir", default="./out", help="Artifacts directory")
    p.add_argument("--json-output", action="store_true", help="Print JSON to stdout")
    args = p.parse_args()

    domain = norm_domain(args.domain)
    if not is_valid_domain(domain):
        print(f"[!] Invalid domain: {args.domain}", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    start = time.time()

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    collected = set()

    if "assetfinder" in sources:
        collected |= from_assetfinder(domain, timeout=args.per_source_timeout)
    if "sublist3r" in sources:
        collected |= from_sublist3r(domain, timeout=args.per_source_timeout)

    # normalize & filter
    cleaned = set()
    for s in collected:
        s2 = norm_domain(s)
        if is_valid_domain(s2) and is_sub_of(s2, domain):
            cleaned.add(s2)

    resolved_map = {}
    if args.resolve and cleaned:
        def worker(h):
            ok, ips = resolve_host(h, timeout=2.0)
            return (h, ips if ok else [])
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
            futs = [ex.submit(worker, h) for h in cleaned]
            for fu in concurrent.futures.as_completed(futs):
                try:
                    h, ips = fu.result()
                    if ips:
                        resolved_map[h] = ips
                except Exception:
                    pass
        cleaned = set(resolved_map.keys())

    subs_sorted = sorted(cleaned)

    txt_path = os.path.join(args.out_dir, f"{domain}.subs.txt")
    json_path = os.path.join(args.out_dir, f"{domain}.subs.json")
    summary_path = os.path.join(args.out_dir, "summary.json")

    write_text(txt_path, "\n".join(subs_sorted) + ("\n" if subs_sorted else ""))
    out_obj = {
        "domain": domain,
        "count": len(subs_sorted),
        "subs": subs_sorted if not args.resolve else [
            {"host": s, "ips": resolved_map.get(s, [])} for s in subs_sorted
        ],
        "sources": {"assetfinder": "assetfinder" in sources, "sublist3r": "sublist3r" in sources},
        "artifacts": {"txt": os.path.abspath(txt_path), "json": os.path.abspath(json_path)}
    }
    write_json(json_path, out_obj)

    # update summary
    try:
        summary = {}
        if os.path.isfile(summary_path):
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        summary[domain] = {"count": len(subs_sorted), "ts": int(time.time())}
        write_json(summary_path, summary)
    except Exception:
        pass

    # stdout
    if args.json_output:
        print(json.dumps(out_obj, ensure_ascii=False))
    else:
        if args.resolve:
            for s in subs_sorted:
                print(s + " " + ",".join(resolved_map.get(s, [])))
        else:
            for s in subs_sorted:
                print(s)

    elapsed = time.time() - start
    print(f"[i] {domain}: found {len(subs_sorted)} subs in {elapsed:.1f}s", file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())

