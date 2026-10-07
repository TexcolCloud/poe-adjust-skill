"""Start one current release record from the prior immutable module grades."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

root=Path(__file__).resolve().parents[1]
read=lambda path:json.loads(path.read_text(encoding='utf-8-sig'))
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
parser=argparse.ArgumentParser()
parser.add_argument('--prior',required=True,help='Previous completed round manifest inside .work')
parser.add_argument('--started-at',required=True,help='Previously recorded actual iteration start, with timezone')
args=parser.parse_args()
started=datetime.fromisoformat(args.started_at)
assert started.tzinfo is not None
prior_path=(root/args.prior).resolve()
assert prior_path.is_relative_to((root/'.work').resolve()) and prior_path.is_file()
prior=read(prior_path)
version=re.search(r'^VERSION = "([^"]+)"',(root/'poe-adjust/scripts/poe.py').read_text(encoding='utf-8'),re.M).group(1)
assert version!=prior['version']
target=root/f'.work/live-{version.replace(".","")}-manifest.json'
assert not target.exists()
protected=list(prior['protected_grades'])
for case in ('materialization','scenario','review'):
    record=prior[case]
    path=root/record['quality_review']
    assert path.resolve().is_relative_to((root/'.work').resolve()) and record['first_delivery_observed']
    assert sha(path)==record['quality_review_sha256']
    grade=read(path)
    assert grade['version']==prior['version'] and grade['case']==case and grade['quality_score']==record['quality_score']
    protected.append({'case':case,'path':record['quality_review'],'sha256':sha(path),
                      'score':record['quality_score'],'version':prior['version']})
value={'version':version,'iteration_started_at_utc':started.isoformat(),
       'recorded_at_utc':datetime.now(timezone.utc).isoformat(),
       'phase':'release_verification','base_commit':prior['source_commit'],
       'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
       'source_snapshot':f'.work/{version.replace(".","")}-final-source-before.json',
       'deadline':None,'authorization':'User explicitly revoked the cutoff and authorized continuous iteration with unchanged requirements.',
       'automation_status':prior['automation_status'],'prior_round':prior_path.relative_to(root).as_posix(),
       'original_path':prior['original_path'],'original_sha256':prior['original_sha256'],
       'protected_grades':protected,'protected_review_addenda':prior['protected_review_addenda'],
       'methodology':prior['methodology'],'monitoring_policy':prior['monitoring_policy'],
       'automation_interval_minutes':prior.get('automation_interval_minutes'),
       'current_fix_basis':[prior[case]['quality_review'] for case in ('materialization','scenario','review')],
       'scope':'Next round only. Previous first scores and frozen artifacts remain immutable. No release or live acceptance claimed.'}
target.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'manifest':str(target),'protected_module_grades':len(protected),'deadline':None,'automation_status':'ACTIVE'}))
