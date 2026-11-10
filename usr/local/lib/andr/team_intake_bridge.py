#!/usr/bin/env python3
"""Carries Sage's verdict lines into the intake API (andr-ops task 0026). Sage can only reach Bitbucket today.
Sage appends JSON lines to INTAKE_VERDICTS_PATH on research main; this reads origin/main and POSTs them. Stateless:
the API keeps the first verdict per drop, so resending is safe. Retire this once Sage can call the API directly."""
import json, os, subprocess, urllib.error, urllib.request

REPO = os.environ['INTAKE_RESEARCH']
PATH = os.environ.get('INTAKE_VERDICTS_PATH', 'topics/content-intelligence/intake-verdicts.jsonl')
URL = os.environ['INTAKE_API_URL']
TOKEN = os.environ['INTAKE_API_TOKEN']

subprocess.run(['git', '-C', REPO, 'fetch', '-q', 'origin', 'main'], capture_output=True, timeout=120)
r = subprocess.run(['git', '-C', REPO, 'show', f'origin/main:{PATH}'], capture_output=True, text=True)
lines = [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []
count = {200: 0, 400: 0, 404: 0, 409: 0}
for line in lines:
    req = urllib.request.Request(f'{URL}/verdicts', line.encode(), {'Authorization': f'Bearer {TOKEN}'})
    try:
        code = urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        code = e.code
    count[code] = count.get(code, 0) + 1
if count[200] or count[400] or count[404]:
    print(f'new {count[200]} rejected {count[400]} unknown {count[404]} already {count[409]}')
else:
    print(f'new 0 already {count[409]}')
