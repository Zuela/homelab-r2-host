"""Backup and job health for r2. Argument daily: prints only backup problems, nothing when clean. Argument weekly: prints every line.
Names and ages only, never job content. Runs read-only."""
import glob, os, re, subprocess, sys, time

now = time.time()


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60).stdout


def age(path_glob):
    files = glob.glob(path_glob)
    return (now - max(os.path.getmtime(f) for f in files)) / 3600 if files else None


def in_journal(unit, words, hours):
    return bool(re.search(words, sh(f'journalctl -u {unit} --since "-{hours} hours" --no-pager -o cat'), re.I))


def cron_ran(script, days):
    return bool(sh(f'journalctl -t CRON --since "-{days} days" --no-pager -o cat | grep -m1 "CMD (.*{script}"'))


def line(ok, name, detail=''):
    return ('ok   ' if ok else 'BAD  ') + name + (f': {detail}' if detail else '')


backups = []
pbs = subprocess.run('nc -z -w3 192.168.5.10 8007', shell=True).returncode == 0
backups.append(line(pbs, 'ProDesk backup server', '' if pbs else 'not reachable'))
a = age('/var/log/vzdump/lxc-*.log')
backups.append(line(a is not None and a < 30, 'nightly container backup', f'last ran {a / 24:.0f} days ago' if a else 'no log'))
for unit, name in (('homelab-r2-secrets-backup.service', 'secrets backup'), ('r2-studio-datasets-backup.service', 'studio backup')):
    bad = in_journal(unit, r'unreachable|skipped|failed', 26)
    backups.append(line(not bad, name, 'upload skipped' if bad else ''))
a = age('/tank/andr-bundles/*/*.bundle')
backups.append(line(a is not None and a < 8 * 24, 'Sunday AndR bundles'))

jobs = []
for script, name, days in (('andr-repo-watch', 'repo pulls', 1), ('lif-muse-outbox-pull', 'hourly LIF mirror', 1),
                           ('andr-research-integrity', 'Thursday integrity', 8), ('andr-sheet-refresh', 'Monday Sheet refresh', 8),
                           ('andr-research-backlinks', 'monthly backlinks', 32)):
    if script == 'andr-sheet-refresh' and now < 1794268800:   # first run 2026-11-09
        jobs.append(line(True, name, 'first run Nov 9'))
    elif script == 'andr-research-backlinks' and now < 1796104800:   # first cron run 2026-12-01
        jobs.append(line(True, name, 'first cron run Dec 1'))
    else:
        jobs.append(line(cron_ran(script, days), name, '' if cron_ran(script, days) else f'no run in {days} days'))
cap = sh('pct exec 218 -- systemctl show intake-capture.service -p Result --value').strip()
jobs.append(line(cap == 'success', 'nightly capture', '' if cap == 'success' else (cap or 'container not reachable')))
team = sh('pct exec 219 -- systemctl is-active team-intake').strip()
jobs.append(line(team == 'active', 'team intake service', '' if team == 'active' else team or 'container not reachable'))
runs = re.findall(r'^\| (\d{4}-\d{2}-\d{2}) \|', open('/tank/andr/research/topics/content-intelligence/runs.md', encoding='utf-8').read(), re.M)
days = (now - time.mktime(time.strptime(runs[-1], '%Y-%m-%d'))) / 86400 if runs else 99
jobs.append(line(days < 9, 'Sage content runs', f'last logged {days:.0f} days ago'))

# Fence (andr-ops task 0036): production code must match andr-ops main. Lists anything running that differs.
DEPLOYED = [('219', '/opt/team-intake/team_intake.py', 'services/team-intake/team_intake.py'),
            ('219', '/opt/sheet-refresh/sheet_refresh.py', 'services/sheet-refresh/sheet_refresh.py'),
            ('', '/usr/local/lib/andr/team_intake_bridge.py', 'services/team-intake/verdict_bridge.py'),
            ('', '/usr/local/lib/andr/connector_refresh.py', 'services/sheet-refresh/connector_refresh.py'),
            ('', '/usr/local/lib/andr/sheet_refresh.py', 'services/sheet-refresh/sheet_refresh.py')] + [
            ('', f'/usr/local/lib/andr/{n}', f'services/r2-jobs/{n}') for n in ('inspo_report.py', 'repo_freshness.py', 'research_counts.py', 'suggest_export.py')]
off = []
for ct, path, repo in DEPLOYED:
    main = subprocess.run(['git', '-C', '/tank/andr/andr-ops', 'show', f'origin/main:{repo}'], capture_output=True).stdout
    live = subprocess.run(['pct', 'exec', ct, '--', 'cat', path] if ct else ['cat', path], capture_output=True).stdout
    if not main or main != live:
        off.append(os.path.basename(path) + (f' (CT {ct})' if ct else ''))
jobs.append(line(not off, 'deployed code matches andr-ops main', ', '.join(off) + ' differ' if off else ''))

mode = sys.argv[1] if len(sys.argv) > 1 else 'weekly'
if mode == 'daily':
    print('\n'.join(x[5:] for x in backups if x.startswith('BAD')))
else:
    n = sum(x.startswith('BAD') for x in backups + jobs)
    print(f'{n} need a look' if n else 'All clear')
    print('\n'.join(backups + jobs))
