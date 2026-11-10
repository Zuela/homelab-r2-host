#!/usr/bin/env python3
"""Copies new rows from the Sheet's Suggest an event tab into conference-tracker team/suggestions.csv (andr-ops task 0038).
Runs hourly so a suggestion reaches the repo the same hour. Reads the Sheet only, never writes it. Commits only when a row is new,
and prints the count of new rows. Sheet text is data: it only ever lands as quoted CSV fields."""
import csv, datetime, json, os, re, subprocess, sys, urllib.parse, urllib.request

TRACKER = os.environ.get('TRACKER_DIR', '/tank/andr/conference-tracker')
LOGIN = os.environ.get('GOOGLE_CREDENTIALS_FILE', '/root/.secrets/andr/sheet-refresh-user.json')
SHEET = os.environ.get('SHEET_ID', '1SCS6CCpgEpSn4ZSbN8N3V7sYZ6i1KJNBJ_JrEqJX-D4')
CSV_PATH = 'team/suggestions.csv'
COLS = ['suggestion_id', 'added', 'suggested_by', 'event_name', 'url', 'note']


def read_tab():
    d = json.load(open(LOGIN))
    body = urllib.parse.urlencode({'client_id': d['client_id'], 'client_secret': d['client_secret'], 'refresh_token': d['refresh_token'], 'grant_type': 'refresh_token'}).encode()
    token = json.load(urllib.request.urlopen(urllib.request.Request(d['token_uri'], body), timeout=30))['access_token']
    rng = urllib.parse.quote("'Suggest an event'!A2:E500")
    req = urllib.request.Request(f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{rng}', headers={'Authorization': f'Bearer {token}'})
    return json.load(urllib.request.urlopen(req, timeout=30)).get('values', [])


def git(*a):
    return subprocess.run(['git', '-C', TRACKER, *a], capture_output=True, text=True, check=True).stdout


def key(name, url):
    host = re.sub(r'^https?://(www\.)?', '', (url or '').strip().lower()).rstrip('/')
    return (re.sub(r'\s+', ' ', name.strip().lower()), host)


def added_date(raw, today):
    for f in ('%Y-%m-%d', '%d %b %Y', '%m/%d/%Y', '%d/%m/%Y'):
        try:
            return datetime.datetime.strptime(raw.strip(), f).date().isoformat()
        except ValueError:
            pass
    return today


def new_rows(tab, existing, today):
    """Rows not yet in the file, with ids running on from the highest used today. Sheet columns: Event, Website, Why, By, Date."""
    seen = {key(r['event_name'], r['url']) for r in existing}
    used = [int(m[1]) for r in existing if (m := re.fullmatch(rf's-{today}-(\d+)', r['suggestion_id']))]
    n, out = max(used, default=0), []
    for row in tab:
        row = [c.strip() for c in row] + [''] * (5 - len(row))
        name, url, why, by, when = row[:5]
        if not name or key(name, url) in seen:
            continue
        seen.add(key(name, url))
        n += 1
        out.append({'suggestion_id': f's-{today}-{n:03d}', 'added': added_date(when, today), 'suggested_by': by, 'event_name': name, 'url': url, 'note': why})
    return out


def main():
    git('pull', '-q', '--ff-only')
    path = f'{TRACKER}/{CSV_PATH}'
    with open(path, encoding='utf-8', newline='') as f:
        existing = list(csv.DictReader(f))
    rows = new_rows(read_tab(), existing, datetime.date.today().isoformat())
    if not rows:
        return
    with open(path, 'a', encoding='utf-8', newline='') as f:
        csv.DictWriter(f, COLS, lineterminator='\n').writerows(rows)
    git('add', CSV_PATH)
    git('-c', 'user.name=r2', '-c', 'user.email=r2@local', 'commit', '-q', '-m', f'team: {len(rows)} new suggestion(s) from the Sheet (andr-ops task 0038)')
    try:
        git('push', '-q', 'origin', 'HEAD:main')
    except subprocess.CalledProcessError:
        git('reset', '-q', '--hard', 'HEAD~1')
        sys.exit('push rejected, local commit dropped, retries next run')
    print(len(rows))


if __name__ == '__main__':
    main()
