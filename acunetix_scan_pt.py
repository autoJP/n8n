#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, sys, time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EXIT_RUNTIME=1; EXIT_INPUT=2; EXIT_EXTERNAL_API=3
STAGE='WF_D_AcunetixScan_PT'

def now(): return datetime.now(timezone.utc)
def iso(dt): return dt.isoformat()

def res_base(pt:int):
    return {"ok":True,"status":"ok","stage":STAGE,"product_type_id":pt,
            "timestamps":{"start":iso(now()),"end":None},
            "metrics":{"scans_started":0,"scans_skipped":0,"findings_imported":0},
            "errors":[],"warnings":[]}

def done(r,o=None):
    r['timestamps']['end']=iso(now())
    if o:
        with open(o,'w',encoding='utf-8') as f: json.dump(r,f,ensure_ascii=False,indent=2)
    print(json.dumps(r,ensure_ascii=False))

def hdr(t): return {"X-Auth":t,"Accept":"application/json","Content-Type":"application/json"}

def call(s,m,u,timeout,**kw):
    for i in range(3):
        r=s.request(m,u,timeout=timeout,**kw)
        if r.status_code in (401,403): raise PermissionError(f'auth_error:{r.status_code}')
        if (r.status_code==429 or r.status_code>=500) and i<2: time.sleep(2**i); continue
        return r
    return r

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--base-url', default=os.environ.get('ACU_BASE_URL'))
    ap.add_argument('--token', default=os.environ.get('ACU_API_TOKEN'))
    ap.add_argument('--group-id', required=True)
    ap.add_argument('--product-type-id', type=int, required=True)
    ap.add_argument('--timeout', type=int, default=30)
    ap.add_argument('--poll-timeout', type=int, default=1800)
    ap.add_argument('--poll-interval', type=int, default=20)
    ap.add_argument('--recent-minutes', type=int, default=30)
    ap.add_argument('--output')
    args=ap.parse_args()

    r=res_base(args.product_type_id)
    if not args.base_url or not args.token or args.timeout<=0:
        r['ok']=False; r['status']='error'; r['errors'].append({'code':'invalid_input'}); done(r,args.output); return EXIT_INPUT
    s=requests.Session(); s.verify=False
    try:
        scans=call(s,'GET',f"{args.base_url.rstrip('/')}/api/v1/scans?l=100",headers=hdr(args.token),timeout=args.timeout)
        scans.raise_for_status()
        items=scans.json().get('scans',[])
        now_dt=now(); recent_cut=now_dt-timedelta(minutes=args.recent_minutes)
        for sc in items:
            if sc.get('target',{}).get('group_id')!=args.group_id:
                continue
            status=(sc.get('current_session',{}) or {}).get('status','').lower()
            started=(sc.get('current_session',{}) or {}).get('start_date')
            started_dt=None
            if started:
                try: started_dt=datetime.fromisoformat(started.replace('Z','+00:00'))
                except Exception: started_dt=None
            if status in {'processing','queued','starting'} or (started_dt and started_dt>=recent_cut):
                r['status']='already_running' if status in {'processing','queued','starting'} else 'skipped'
                r['metrics']['scans_skipped']=1
                done(r,args.output); return 0

        payload={"target_id":args.group_id,"profile_id":"11111111-1111-1111-1111-111111111111","schedule":{"disable":False,"start_date":None,"time_sensitive":False}}
        start=call(s,'POST',f"{args.base_url.rstrip('/')}/api/v1/scans",headers=hdr(args.token),json=payload,timeout=args.timeout)
        if start.status_code not in (200,201):
            raise RuntimeError(f'scan_start_failed:{start.status_code}:{start.text}')
        sid=start.json().get('scan_id')
        r['metrics']['scans_started']=1

        deadline=time.time()+args.poll_timeout
        wait=args.poll_interval
        while time.time()<deadline:
            st=call(s,'GET',f"{args.base_url.rstrip('/')}/api/v1/scans/{sid}",headers=hdr(args.token),timeout=args.timeout)
            st.raise_for_status()
            status=((st.json().get('current_session') or {}).get('status') or '').lower()
            if status in {'completed','aborted','failed'}:
                if status!='completed':
                    r['status']='error'; r['ok']=False; r['errors'].append({'code':'scan_failed','message':status}); done(r,args.output); return EXIT_RUNTIME
                done(r,args.output); return 0
            time.sleep(wait); wait=min(wait*2,120)

        r['status']='timeout'; r['warnings'].append({'code':'poll_timeout'})
        done(r,args.output); return 0
    except PermissionError as e:
        r['ok']=False; r['status']='error'; r['errors'].append({'code':'auth_error','message':str(e)}); done(r,args.output); return EXIT_EXTERNAL_API
    except requests.RequestException as e:
        r['ok']=False; r['status']='error'; r['errors'].append({'code':'external_api','message':str(e)}); done(r,args.output); return EXIT_EXTERNAL_API
    except Exception as e:
        r['ok']=False; r['status']='error'; r['errors'].append({'code':'runtime_error','message':str(e)}); done(r,args.output); return EXIT_RUNTIME

if __name__=='__main__':
    sys.exit(main())
