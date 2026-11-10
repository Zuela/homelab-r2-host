"""Copies every message on the ntfy homelab-alerts topic into the Matrix homelab-alerts room (Dan 2026-11-02).
Keeps ntfy working as it was. Remembers the last message, so a Matrix outage delays alerts instead of losing them, within ntfy's 24 hours."""
import json, os, sys, time, urllib.request
sys.path[:0] = ['/usr/local/sbin', '/usr/local/lib/andr']
from matrix_client_simple import MatrixClient
from matrix_format import render

BASE, TOKEN = os.environ['NTFY_BASE_URL'], open(os.environ['NTFY_TOKEN_FILE']).read().strip()
STATE = '/var/lib/ntfy-to-matrix/last'
ROOM = json.load(open('/root/.creds/matrix-namespace-rooms.json'))['homelab_alerts_room_id']
os.makedirs(os.path.dirname(STATE), exist_ok=True)


def deliver(e):
    text = '\n'.join(x for x in (e.get('title'), e.get('message', '')) if x)
    while True:
        try:
            c = MatrixClient()
            c.ensure_token()
            c.send_text(ROOM, text, html=render(e.get('title') or '', e.get('message', '')))
            return
        except Exception as err:
            print(f'matrix send failed, retrying: {str(err)[:120]}', flush=True)
            time.sleep(60)


since = open(STATE).read().strip() if os.path.exists(STATE) else str(int(time.time()))
while True:
    try:
        req = urllib.request.Request(f'{BASE}/homelab-alerts/json?since={since}', headers={'Authorization': f'Bearer {TOKEN}'})
        with urllib.request.urlopen(req, timeout=120) as stream:
            for raw in stream:
                e = json.loads(raw)
                if e.get('event') != 'message':
                    continue
                deliver(e)
                since = e['id']
                open(STATE, 'w').write(since)
                print(f'forwarded {since}', flush=True)
    except Exception as err:
        print(f'stream ended: {str(err)[:120]}', flush=True)
        time.sleep(10)
