#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EXIT_RUNTIME=1
EXIT_INPUT=2
EXIT_EXTERNAL_API=3
STAGE="WF_C.acunetix_set_group_scan_speed"

def now()->str:
    return datetime.now(timezone.utc).isoformat()

def headers(token:str)->Dict[str,str]:
    return {"X-Auth": token, "Accept": "application/json", "Content-Type": "application/json"}

def call(method:str,s:requests.Session,url:str,timeout:int,**kwargs:Any)->requests.Response:
    for i in range(3):
        r=s.request(method,url,timeout=timeout,**kwargs)
        if r.status_code in (401,403):
            raise PermissionError(f"auth_error:{r.status_code}")
        if r.status_code==429 and i<2:
            time.sleep(2**i); continue
        if r.status_code>=500 and i<2:
            time.sleep(2**i); continue
        return r
    return r

def base_result(group_id: str|None)->Dict[str,Any]:
    return {"ok":True,"status":"ok","stage":STAGE,"product_type_id":None,
            "timestamps":{"start":now(),"end":None},
            "metrics":{"targets_total":0,"targets_updated":0,"targets_skipped":0},
            "errors":[],"warnings":[],"group_id":group_id}

def done(res:Dict[str,Any], output:Optional[str]):
    res["timestamps"]["end"]=now()
    if output:
        with open(output,'w',encoding='utf-8') as f: json.dump(res,f,ensure_ascii=False,indent=2)
    print(json.dumps(res,ensure_ascii=False))

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--base-url', default=os.environ.get('ACU_BASE_URL'))
    ap.add_argument('--token', default=os.environ.get('ACU_API_TOKEN'))
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--group-id')
    g.add_argument('--group-name')
    ap.add_argument('--scan-speed', default='sequential')
    ap.add_argument('--product-type-id', type=int)
    ap.add_argument('--input')
    ap.add_argument('--output')
    ap.add_argument('--timeout', type=int, default=30)
    ap.add_argument('--dry-run', action='store_true')
    args=ap.parse_args()

    res=base_result(args.group_id)
    res['product_type_id']=args.product_type_id
    if not args.base_url or not args.token or args.timeout<=0:
        res['ok']=False; res['status']='error'; res['errors'].append({'code':'invalid_input','message':'missing base-url/token or bad timeout'})
        done(res,args.output); return EXIT_INPUT

    s=requests.Session(); s.verify=False
    try:
        group_id=args.group_id
        if not group_id:
            r=call('GET',s,f"{args.base_url.rstrip('/')}/api/v1/target_groups?limit=100",headers=headers(args.token),timeout=args.timeout)
            r.raise_for_status()
            groups=r.json().get('groups',[])
            match=next((x for x in groups if x.get('name')==args.group_name),None)
            if not match:
                res['status']='skipped'; res['warnings'].append({'code':'group_not_found','message':args.group_name}); done(res,args.output); return 0
            group_id=match.get('group_id')
        res['group_id']=group_id

        r=call('GET',s,f"{args.base_url.rstrip('/')}/api/v1/target_groups/{group_id}/targets",headers=headers(args.token),timeout=args.timeout)
        r.raise_for_status()
        tids=r.json().get('target_id_list',[])
        res['metrics']['targets_total']=len(tids)
        if not tids:
            res['status']='skipped'; done(res,args.output); return 0

        for tid in tids:
            cfg=call('GET',s,f"{args.base_url.rstrip('/')}/api/v1/targets/{tid}/configuration",headers=headers(args.token),timeout=args.timeout)
            cfg.raise_for_status()
            current=cfg.json().get('scan_speed')
            if current==args.scan_speed:
                res['metrics']['targets_skipped']+=1
                continue
            if args.dry_run:
                res['metrics']['targets_updated']+=1
                continue
            upd=call('PATCH',s,f"{args.base_url.rstrip('/')}/api/v1/targets/{tid}/configuration",headers=headers(args.token),json={'scan_speed':args.scan_speed},timeout=args.timeout)
            if upd.status_code not in (200,204):
                res['errors'].append({'code':'update_failed','target_id':tid,'status':upd.status_code})
            else:
                res['metrics']['targets_updated']+=1

        if res['errors']:
            res['ok']=False; res['status']='error'; done(res,args.output); return EXIT_RUNTIME
        if res['metrics']['targets_updated']==0:
            res['status']='skipped'
        done(res,args.output)
        return 0

    except PermissionError as e:
        res['ok']=False; res['status']='error'; res['errors'].append({'code':'auth_error','message':str(e)}); done(res,args.output); return EXIT_EXTERNAL_API
    except requests.RequestException as e:
        res['ok']=False; res['status']='error'; res['errors'].append({'code':'external_api','message':str(e)}); done(res,args.output); return EXIT_EXTERNAL_API
    except Exception as e:
        res['ok']=False; res['status']='error'; res['errors'].append({'code':'runtime_error','message':str(e)}); done(res,args.output); return EXIT_RUNTIME

if __name__=='__main__':
    sys.exit(main())
