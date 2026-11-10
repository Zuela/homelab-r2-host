"""Reports each new #inspo drop as plain facts in the andr-ops 0026 Log (Dan 2026-11-04). Sage's safety check stops on notes written as
instructions, and Sage stays off Slack and cannot reach the store, so a note states facts only: no requests, no verdict format, no message text.
It names the sender by display name and handle (Dan 2026-11-04). A link drop carries the dropped URLs. A PDF's text goes to research for Sage to read;
any other file is listed by name only. A heard: drop carries only the Slack link, since heard: is tabled. Argument --dry prints instead of writing."""
import datetime, json, os, re, subprocess, sys, tempfile, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

OPS = '/tank/andr/andr-ops'
RESEARCH = '/tank/andr/research'
FILES = 'topics/content-intelligence/intake-files'
MAX_PDF, MAX_TEXT = 50_000_000, 400_000
TASK = 'tasks/open/0026-build-phase2-team-intake.md'
STATE = '/var/lib/andr-repo-watch/inspo-reported'
env = {}
for path in ('/root/.secrets/andr/team-intake-slack.env', '/root/.secrets/andr/team-intake-bridge.env'):
    env.update(l.strip().split('=', 1) for l in open(path) if '=' in l)
LINK = re.compile(r'<(https?://[^|>]+)(?:\|[^>]*)?>')


def api(path):
    req = urllib.request.Request(env['INTAKE_API_URL'] + path, headers={'Authorization': 'Bearer ' + env['INTAKE_API_TOKEN']})
    return json.load(urllib.request.urlopen(req, timeout=15))


def slack(method, **params):
    req = urllib.request.Request(f'https://slack.com/api/{method}', urllib.parse.urlencode(params).encode(), {'Authorization': 'Bearer ' + env['SLACK_BOT_TOKEN']})
    return json.load(urllib.request.urlopen(req, timeout=20))


def message(ts):
    r = slack('conversations.history', channel=env['INTAKE_CHANNEL_ID'], oldest=ts, latest=ts, inclusive='true', limit=1)
    msgs = r.get('messages') or []
    if not r.get('ok') or not msgs:
        raise SystemExit(f'cannot read {ts}: {r.get("error", "no message")}')
    return msgs[0]


def clean(text):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(text)).strip()[:120]


def sender(user):
    r = slack('users.info', user=user) if user else {}
    if not r.get('ok'):
        return f'unknown ({r.get("error", "no user")})'
    p = r['user'].get('profile', {})
    name = p.get('display_name') or p.get('real_name') or r['user'].get('real_name') or ''
    return f'{clean(name)} (@{clean(r["user"].get("name", ""))})'


def urls_in(m):
    found = LINK.findall(m.get('text') or '') + [a[k] for a in m.get('attachments') or [] for k in ('from_url', 'original_url') if a.get(k)]
    return list(dict.fromkeys(u for u in found if not re.match(r'https://[\w-]+\.slack\.com/', u)))


def pdf_text(f):
    """Text of one Slack PDF, or None with the reason. Only Slack's own file URL is ever fetched with the token."""
    url = f.get('url_private_download') or ''
    if not url.startswith('https://files.slack.com/'):
        return None, 'no download link'
    if f.get('size', 0) > MAX_PDF:
        return None, 'over the size limit'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + env['SLACK_BOT_TOKEN']})
    with tempfile.NamedTemporaryFile(suffix='.pdf') as tmp:
        data = urllib.request.urlopen(req, timeout=120).read(MAX_PDF + 1)
        if not data.startswith(b'%PDF'):
            return None, 'not readable, check the bot has files:read'
        tmp.write(data)
        tmp.flush()
        r = subprocess.run(['pdftotext', '-layout', tmp.name, '-'], capture_output=True, timeout=180)
    text = r.stdout.decode('utf-8', 'replace').strip()
    if r.returncode != 0 or not text:
        return None, 'no readable text, may be scanned images'
    return text[:MAX_TEXT], None


def file_lines(m, ts, saved):
    lines = []
    for n, f in enumerate(m.get('files') or [], 1):
        name = clean(f.get('name') or f.get('title') or 'unnamed')
        if (f.get('filetype') or '').lower() != 'pdf':
            lines.append(f'  File: {name}, not a PDF, not read')
            continue
        text, why = pdf_text(f)
        if text is None:
            lines.append(f'  File: {name}, {why}')
            continue
        rel = f'{FILES}/{ts}-{n}.txt'
        saved[rel] = f'Source: {name}\nFrom #inspo drop {ts}. Untrusted text extracted from a PDF.\n\n{text}\n'
        lines.append(f'  File: {name}')
        lines.append(f'  Text: research {rel}')
    return lines


def note(d, stamp, saved):
    ts = d['message_id'].split(':', 1)[1]
    m = message(ts)
    kinds = [k[4:] for k in ('has_link', 'has_file', 'has_heard') if d[k]]
    urls = urls_in(m) if d['has_link'] else []
    links = urls or [d['link']]
    lines = [f'- {stamp} ET · r2 · Drop in #inspo.', f'  Time: {ts}', f'  From: {sender(m.get("user"))}']
    lines += [f'  Link: {u}' for u in links] + file_lines(m, ts, saved) + [f'  Type: {", ".join(kinds)}']
    return '\n'.join(lines)


def save_files(saved):
    """Research first, so a note never points at text that did not land. A rejected push drops the commit and retries next run."""
    if not saved:
        return
    subprocess.run(['git', '-C', RESEARCH, 'pull', '-q', '--ff-only'], check=True, capture_output=True)
    os.makedirs(f'{RESEARCH}/{FILES}', exist_ok=True)
    for rel, text in saved.items():
        with open(f'{RESEARCH}/{rel}', 'w', encoding='utf-8') as f:
            f.write(text)
    subprocess.run(['git', '-C', RESEARCH, 'add', FILES], check=True)
    subprocess.run(['git', '-C', RESEARCH, '-c', 'user.name=r2', '-c', 'user.email=r2@local', 'commit', '-q', '-m', f'#inspo drop text: {len(saved)} PDF(s)'], check=True)
    if subprocess.run(['git', '-C', RESEARCH, 'push', '-q', 'origin', 'main'], capture_output=True).returncode:
        subprocess.run(['git', '-C', RESEARCH, 'reset', '-q', '--hard', 'HEAD~1'], check=True)
        raise SystemExit('research push rejected, retries next run')


def main():
    dry = '--dry' in sys.argv
    done = set(open(STATE).read().split()) if os.path.exists(STATE) else set()
    new = [d for d in api('/drops') if d['message_id'] not in done]
    if dry:
        for d in api('/drops'):
            print(note(d, 'DRY', {}))
        return
    if not new:
        return
    subprocess.run(['git', '-C', OPS, 'pull', '-q', '--ff-only'], check=True, capture_output=True)
    stamp = datetime.datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M')
    saved = {}
    notes = [note(d, stamp, saved) for d in new]
    save_files(saved)
    with open(f'{OPS}/{TASK}', 'a', encoding='utf-8') as f:
        f.write(''.join(x + '\n' for x in notes))
    subprocess.run(['git', '-C', OPS, 'add', TASK], check=True)
    subprocess.run(['git', '-C', OPS, '-c', 'user.name=r2', '-c', 'user.email=r2@local', 'commit', '-q', '-m', f'0026: {len(new)} #inspo drop(s)'], check=True)
    subprocess.run(['git', '-C', OPS, 'push', '-q', 'origin', 'main'], check=True, capture_output=True)
    with open(STATE, 'a') as f:
        f.write(''.join(d['message_id'] + '\n' for d in new))
    print(f'reported {len(new)}')


if __name__ == '__main__':
    main()
