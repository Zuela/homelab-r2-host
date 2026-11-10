"""Freshness fences for Sage's runs (andr-ops task 0033), run by andr-repo-watch against the pulled clones.
Sage's schedules do not retry, and a run that dies early leaves no trace, so this checks what each run should have left behind.
Prints one line per condition that just went stale; each alerts once until it is fresh again. A stale clone is the pull alert's job."""
import datetime as dt, os, re, subprocess

STATE = os.environ.get('FRESH_STATE', '/var/lib/andr-repo-watch/fresh')
TANK = os.environ.get('FRESH_TANK', '/tank/andr')
now = dt.datetime.fromisoformat(os.environ['FRESH_NOW']) if os.environ.get('FRESH_NOW') else dt.datetime.now()
HEARTBEAT_START = dt.datetime(2026, 11, 9, 11)


def last_commit(repo, *paths):
    r = subprocess.run(['git', '-C', f'{TANK}/{repo}', 'log', '-1', '--format=%ct', '--', *paths], capture_output=True, text=True)
    return dt.datetime.fromtimestamp(int(r.stdout)) if r.stdout.strip() else None


def weekday_start(weekday):
    """Midnight on the most recent such weekday, today included."""
    d = now.date() - dt.timedelta(days=(now.weekday() - weekday) % 7)
    return dt.datetime.combine(d, dt.time())


def runs_problem():
    text = open(f'{TANK}/research/topics/content-intelligence/runs.md', encoding='utf-8').read()
    dates = re.findall(r'^\| (\d{4}-\d{2}-\d{2}) \|', text, re.M)
    age = (now.date() - dt.date.fromisoformat(max(dates))).days if dates else None
    if age is None or age > 9:
        return f'Sage runs.md has no line in 9 days (last {max(dates) if dates else "none"})'


def tracker_problem():
    """The Monday monitor adds a runs.md line every run, quiet or not. That heartbeat starts Nov 9; until then the list files stand in."""
    monday = weekday_start(0)
    if now < monday.replace(hour=11):
        monday -= dt.timedelta(days=7)
    last = last_commit('conference-tracker', 'runs.md')
    if not last and monday < HEARTBEAT_START:
        last = last_commit('conference-tracker', 'conferences.md', 'events.csv')
    if not last or last < monday:
        return f'Conference monitor: no run logged since Monday {monday:%b %-d}'


def signals_problem():
    """Checked from Wednesday 18:00 for that day's scan line, written about 10:36."""
    wed = weekday_start(2)
    if now < wed.replace(hour=18):
        wed -= dt.timedelta(days=7)
    text = open(f'{TANK}/research/topics/content-intelligence/runs.md', encoding='utf-8').read()
    dates = re.findall(r'^\| (\d{4}-\d{2}-\d{2}) \| [^|]*weekly signal scan[^|]*\|', text, re.M | re.I)
    if not dates or dt.date.fromisoformat(max(dates)) < wed.date():
        return f'Sage weekly scan: no runs.md line since Wednesday {wed:%b %-d}'


os.makedirs(STATE, exist_ok=True)
for key, check in (('runs', runs_problem), ('tracker', tracker_problem), ('signals', signals_problem)):
    flag, problem = f'{STATE}/{key}', check()
    if problem and not os.path.exists(flag):
        open(flag, 'w').write(problem)
        print(problem)
    elif not problem and os.path.exists(flag):
        os.remove(flag)
