"""research:backlinks generator (andr-ops task 0017). Rewrites only the angles:auto fence in each thesis file from angles.md."""
import glob, os, re, sys

root = sys.argv[1]
th, angles = f'{root}/topics/theses', f'{root}/topics/content-intelligence/angles.md'
OPEN, CLOSE = '<!-- angles:auto -->', '<!-- /angles:auto -->'

rows = {}
for l in open(angles, encoding='utf-8'):
    if l.startswith('| A-'):
        c = [x.strip() for x in l.strip().strip('|').split('|')]
        for b in re.findall(r'\bB-\d{3}\b', c[-2]):
            rows.setdefault(b, []).append(f'- {c[0]} ({c[-1]}): {c[1]}')

new = {}
for f in sorted(glob.glob(f'{th}/B-*.md')):
    b = os.path.basename(f)[:5]
    s = open(f, encoding='utf-8').read()
    body = '\n'.join(rows.get(b, ['(no angles yet)']))
    block = f'{OPEN}\n## Angles\n\n{body}\n{CLOSE}'
    o, c = s.count(OPEN), s.count(CLOSE)
    if o == 0 and c == 0:
        t = s.rstrip('\n') + f'\n\n{block}\n'
    elif o == 1 and c == 1 and s.index(OPEN) < s.index(CLOSE):
        t = s[:s.index(OPEN)] + block + s[s.index(CLOSE) + len(CLOSE):]
    else:
        sys.exit(f'{os.path.basename(f)}: angles:auto fence broken, nothing written')
    if t != s:
        new[f] = t

for f, t in new.items():
    open(f, 'w', encoding='utf-8').write(t)
print(f'{len(new)} file(s) changed')
