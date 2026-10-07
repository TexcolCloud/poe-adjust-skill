"""Mechanical release and immutable-history checks; not a quality benchmark."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parents[1]
label = sys.argv[1] if len(sys.argv)>1 else 'closeout'
assert label in {'pre-live','closeout'}
read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
version = re.search(r'^VERSION = "([^"]+)"', (root/'poe-adjust/scripts/poe.py').read_text(encoding='utf-8'), re.M).group(1)
prefix = version.replace('.', '')
snapshot = read(root/f'.work/{prefix}-final-source-before.json')
assert snapshot == read(root/f'.work/{prefix}-final-source-after.json')
assert all(sha(root/row['path']) == row['sha256'] for row in snapshot)
installed = read(root/f'.work/{prefix}-install-manifest.json')
assert all(sha(Path(installed['installation'])/row['path']) == row['sha256'] for row in installed['files'])
frozen = []
for path in sorted((root/'.work').glob('live-*/manifest.json')):
    manifest = read(path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list) or not manifest.get('frozen_workspace'): continue
    base = Path(manifest['frozen_workspace']).resolve()
    assert base.is_relative_to((root/'.work').resolve())
    assert all((base/r['path']).resolve().is_relative_to(base) and sha(base/r['path']) == r['sha256'] for r in manifest['files'])
    frozen.append({'case':path.parent.name, 'file_count':len(manifest['files']), 'unchanged':True})
archives = []
for path in sorted((root/'.work').glob('*-format-package-verification.json')):
    record = read(path)
    assert sha(Path(record['archive'])) == record['archive_sha256']
    archives.append({'version':record['skill_version'], 'sha256':record['archive_sha256'], 'unchanged':True})
live = read(root/f'.work/live-{prefix}-manifest.json')
assert sha(Path(live['original_path'])) == live['original_sha256']
prior_grades = []
for name, score in [('live-280-character-first',73), ('live-282-character-first',60),
                    ('live-283-recovery-first',66), ('live-284-encounter-first',64),
                    ('live-285-leveling-first',43)]:
    value = read(root/f'.work/{name}/review.json')
    assert value['quality_score'] == score
    prior_grades.append({'case':name, 'first_score':score, 'core_acceptance':value['core_acceptance_passed']})
automation = tomllib.loads(Path(r'C:\Users\23835\.codex\automations\poe\automation.toml').read_text(encoding='utf-8'))
assert automation['status'] == 'PAUSED'
log = (root/f'.work/{prefix}-final-regression.log').read_text(encoding='utf-8')
result = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK(?:\s|$)', log)
assert result and 'skipped' not in log
report = {'checked_at_utc':datetime.now(timezone.utc).isoformat(), 'version':version, 'phase':label,
          'runtime_source_commit':installed['source_commit'], 'source_and_test_files':len(snapshot),
          'source_zip_installed_delivery_files':len(installed['files']), 'runtime_and_tests_unchanged':True,
          'original_xml_unchanged':True, 'frozen':frozen, 'frozen_file_count':sum(r['file_count'] for r in frozen),
          'archives':archives, 'prior_grades':prior_grades, 'automation_status':automation['status'],
          'offline_tests':int(result[1]), 'offline_seconds':float(result[2]), 'offline_skipped':0,
          'git_diff_check':subprocess.run(['git','diff','--check'],cwd=root,capture_output=True,text=True).returncode == 0,
          'scope':'Mechanical identity and preservation only; no general model accuracy or build-success claim.'}
assert report['git_diff_check']
out = root/f'.work/{prefix}-{label}-verification.json'
assert not out.exists()
out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k not in ('frozen','archives')},ensure_ascii=False))
