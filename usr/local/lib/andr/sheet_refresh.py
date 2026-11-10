#!/usr/bin/env python3
"""conference-tracker:sheet-refresh (andr-ops task 0007). Copies the master list into the AndR Conference Tracker Sheet.
Reads events.csv, conferences.md, and speakers.csv from TRACKER_DIR. Matches Events rows by id, never writes the team
columns (Our plan, Owner, Team notes), never deletes an Events row, and flags a row whose id left the master list.
Prints the plan and writes nothing unless SHEET_REFRESH_APPLY=1. All config comes from the environment.
Sage reads the open web, so tracker text is untrusted: text goes in RAW, and formulas are built only from checked values.
"""
import csv, datetime, json, os, re, sys
from pathlib import Path

TRACKER = os.environ.get('TRACKER_DIR', '/tracker')
SHEET_ID = os.environ.get('SHEET_ID', '')
CREDS = os.environ.get('GOOGLE_CREDENTIALS_FILE', '')
APPLY = os.environ.get('SHEET_REFRESH_APPLY', '0') == '1'
EVENTS, SPEAKERS, EXCLUDED, ABOUT = 'Events', 'Speakers', 'Excluded', 'About'
# Events columns, A to U. L, M, and N (index 11 to 13) belong to the team.
ID_COL, STATUS_COL, TEAM = 19, 20, range(11, 14)
GONE = 'not in master list'


class Formula(str):
    """A cell this job built from checked values. Everything else is written RAW."""


def read_csv(name):
    with open(f'{TRACKER}/{name}', encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def conference_cells():
    """id to (dates, theme, fit, ways in) from the conferences.md tables. A malformed row maps to None, so its cells are left as they are."""
    out = {}
    with open(f'{TRACKER}/conferences.md', encoding='utf-8') as f:
        for line in f:
            if not re.match(r'^\| [a-z0-9-]+ \|', line):
                continue
            c = [x.strip() for x in line.strip().strip('|').split('|')]
            if len(c) == 12:
                out[c[0]] = (c[3], c[6], c[7], c[9])
            elif len(c) > 12:
                out[c[0]] = None
    return out


def date_formula(value):
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', value or '')
    return Formula(f'=DATE({int(m[1])},{int(m[2])},{int(m[3])})') if m else ''


def name_cell(e):
    url, name = e['official_url'], e['name']
    if re.fullmatch(r'https?://[^\s"\\]+', url or ''):
        return Formula('=HYPERLINK("{}","{}")'.format(url, name.replace('"', '""')))
    return name


def event_row(e, conf):
    """The 21 Events cells for one event. None means leave the cell alone: team cells, and text with no clean source row."""
    dates, about, fit, ways = conf.get(e['id']) or (None, None, None, None)
    status = e['event_status'] if e['event_status'] in ('past', 'tentative', 'at-risk', 'cancelled', 'excluded') else ''
    return [name_cell(e), e['priority'], e['region'], date_formula(e['start_date']), date_formula(e['end_date']), dates,
            e['city'], e['venue'], e['country'], date_formula(e['next_deadline']), e['deadline_note'], None, None, None,
            e['price_note'], fit, ways, about, e['sources'].replace(';', ', ').replace(',  ', ', '), e['id'], status]


def col(i):
    return chr(ord('A') + i)


def changes_for(rownum, new, old):
    """Writes for one Events row, split by how Sheets must read them."""
    raw, formula = [], []
    for i, v in enumerate(new):
        if i in TEAM or v is None:
            continue
        current = old[i] if i < len(old) else ''
        if str(current) != v:
            (formula if isinstance(v, Formula) else raw).append((f'{EVENTS}!{col(i)}{rownum}', [[v]]))
    return raw, formula


def plan(events, conf, speakers, sheet_rows):
    """Works out every write. Returns (raw, formula, report lines)."""
    raw, formula, report = [], [], []
    by_id = {r[ID_COL]: n for n, r in enumerate(sheet_rows[1:], 2) if len(r) > ID_COL and r[ID_COL]}
    if len(sheet_rows) < 1 or sheet_rows[0][ID_COL:STATUS_COL + 1] != ['ID', 'Status'] or sheet_rows[0][11:14] != ['Our plan', 'Owner', 'Team notes']:
        sys.exit('Events header is not the expected layout; nothing written')
    next_row = len(sheet_rows) + 1
    live = [e for e in events if e['event_status'] != 'excluded']
    for e in live:
        if not conf.get(e['id']):
            report.append(f'warning {e["id"]}: no clean row in conferences.md, its text cells left as they are')
        if e['id'] in by_id:
            n = by_id[e['id']]
            r, f = changes_for(n, event_row(e, conf), sheet_rows[n - 1])
            if r or f:
                report.append(f'update {e["id"]}: {len(r) + len(f)} cell(s)')
        else:
            n, next_row = next_row, next_row + 1
            r, f = changes_for(n, event_row(e, conf), [])
            report.append(f'add {e["id"]}')
        raw += r
        formula += f
    keep = {e['id'] for e in live}
    excluded_ids = {e['id'] for e in events if e['event_status'] == 'excluded'}
    for i, n in by_id.items():
        if i in keep:
            continue
        flag = 'excluded' if i in excluded_ids else GONE
        row = sheet_rows[n - 1]
        if (row[STATUS_COL] if len(row) > STATUS_COL else '') != flag:
            raw.append((f'{EVENTS}!{col(STATUS_COL)}{n}', [[flag]]))
            report.append(f'flag {i}: {flag}')
    return raw, formula, report


def excluded_values(events):
    return [[e['name'], e['exclude_reason']] for e in events if e['event_status'] == 'excluded']


def speaker_values(speakers, events):
    themes = {e['id']: e['themes'].replace(';', '; ') for e in events}
    text, links = [], []
    for n, s in enumerate(speakers, 2):
        text.append([s['person'], s['role'].capitalize(), s['company'], s['talk_title'], s['level'].capitalize(), themes.get(s['event_id'], s['themes']),
                     s['source_url'], s['event_id']])
        links.append([f'=IFERROR(INDEX(Events!$A:$A,MATCH($J{n},Events!$T:$T,0)),$J{n})',
                      f'=IFERROR(IF(INDEX(Events!$D:$D,MATCH($J{n},Events!$T:$T,0))="",INDEX(Events!$F:$F,MATCH($J{n},Events!$T:$T,0)),'
                      f'INDEX(Events!$D:$D,MATCH($J{n},Events!$T:$T,0))),"")'])
    added = [[date_formula(s['added'])] for s in speakers]
    return text, links, added


def head_commit():
    """Short commit of the tracker clone, read without git so a read-only mount works anywhere."""
    git = f'{TRACKER}/.git'
    read = lambda p: Path(p).read_text().strip()
    ref = read(f'{git}/HEAD')
    if not ref.startswith('ref: '):
        return ref[:7]
    name = ref[5:]
    if os.path.exists(f'{git}/{name}'):
        return read(f'{git}/{name}')[:7]
    for line in read(f'{git}/packed-refs').splitlines():
        if line.endswith(name):
            return line[:7]
    return 'unknown'


class Sheets:
    def __init__(self, creds_file, sheet_id):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import credentials, service_account
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        if json.load(open(creds_file)).get('type') == 'authorized_user':
            c = credentials.Credentials.from_authorized_user_file(creds_file, scopes)
        else:
            c = service_account.Credentials.from_service_account_file(creds_file, scopes=scopes)
        self.s, self.base = AuthorizedSession(c), f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}'

    def read(self, rng):
        r = self.s.get(f'{self.base}/values/{rng}', params={'valueRenderOption': 'FORMULA'}, timeout=60)
        r.raise_for_status()
        return r.json().get('values', [])

    def write(self, option, data):
        body = {'valueInputOption': option, 'data': [{'range': a, 'values': v} for a, v in data]}
        self.s.post(f'{self.base}/values:batchUpdate', json=body, timeout=120).raise_for_status()

    def clear(self, rng):
        self.s.post(f'{self.base}/values/{rng}:clear', json={}, timeout=60).raise_for_status()


def refresh(sheets, today=None):
    events, speakers, conf = read_csv('events.csv'), read_csv('speakers.csv'), conference_cells()
    raw, formula, report = plan(events, conf, speakers, sheets.read(f'{EVENTS}!A1:U'))
    for line in report:
        print(line)
    print(f'{len(report)} line(s), {len(events)} in master list, apply={APPLY}')
    if not APPLY:
        return
    if raw:
        sheets.write('RAW', raw)
    if formula:
        sheets.write('USER_ENTERED', formula)
    sheets.clear(f'{EXCLUDED}!A2:B')
    sheets.write('RAW', [(f'{EXCLUDED}!A2:B', excluded_values(events))])
    text, links, added = speaker_values(speakers, events)
    sheets.clear(f'{SPEAKERS}!A2:K')
    sheets.write('RAW', [(f'{SPEAKERS}!C2:J', text)])
    sheets.write('USER_ENTERED', [(f'{SPEAKERS}!A2:B', links), (f'{SPEAKERS}!K2:K', added)])
    day = (today or datetime.date.today()).strftime('%-d %b %Y')
    sheets.write('RAW', [(f'{ABOUT}!B5', [[f'{day}, from commit {head_commit()}']])])
    left = plan(events, conf, speakers, sheets.read(f'{EVENTS}!A1:U'))
    if left[0] or left[1]:
        sys.exit(f'read-back failed, {len(left[0]) + len(left[1])} cell(s) differ after writing')


if __name__ == '__main__':
    if not (SHEET_ID and CREDS):
        sys.exit('SHEET_ID and GOOGLE_CREDENTIALS_FILE must be set')
    refresh(Sheets(CREDS, SHEET_ID))
