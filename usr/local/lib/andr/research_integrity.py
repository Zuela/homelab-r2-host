"""research:integrity checks (andr-ops task 0021). Reads an exported copy of research main; prints one finding per line."""
import collections, glob, os, re, sys

root, ops = sys.argv[1], sys.argv[2]
th, ci = f'{root}/topics/theses', f'{root}/topics/content-intelligence'
done = lambda n: bool(glob.glob(f'{ops}/tasks/done/{n}-*.md'))
out = []

files = {}
for f in glob.glob(f'{th}/B-*.md'):
    b = os.path.basename(f)[:5]
    if b in files:
        out.append(f'duplicate thesis id {b}')
    files[b] = f
md = {os.path.relpath(f, root): open(f, encoding='utf-8').read() for f in glob.glob(f'{root}/**/*.md', recursive=True)}

for p, s in md.items():
    for b in sorted(set(re.findall(r'\bB-\d{3}\b', s)) - set(files)):
        out.append(f'{p}: {b} does not resolve to a thesis file')
    if p != 'topics/theses/theses.md':
        for t in sorted(set(re.findall(r'\bT-0\d\d\b', s))):
            out.append(f'{p}: old thesis id {t}')
    for t in sorted(set(re.findall(r'\b([TG])(\d+)\b', s))):
        if not 1 <= int(t[1]) <= (8 if t[0] == 'T' else 7):
            out.append(f'{p}: theme id {t[0]}{t[1]} outside T1 to T8 and G1 to G7')

for b, f in sorted(files.items()):
    for n, l in enumerate(open(f, encoding='utf-8'), 1):
        if l.startswith('- ') and ' · ' in l:
            parts = l.split(' · ')
            if len(parts) < 5 or parts[3] not in ('intake', 'signal', 'field', 'report'):
                out.append(f'{os.path.relpath(f, root)}:{n}: evidence line lacks five fields with a valid lane')

sig_text = ''.join(open(f, encoding='utf-8').read() for f in glob.glob(f'{ci}/signals-*.md'))
sigs = collections.Counter(re.findall(r'Signal id: (S-\d{4}-\d\d-\d\d-\d\d)', sig_text))
out += [f'duplicate signal id {k}' for k, n in sigs.items() if n > 1]

for l in open(f'{ci}/angles.md', encoding='utf-8'):
    if l.startswith('| A-'):
        c = [x.strip() for x in l.strip().strip('|').split('|')]
        if c[-2] != 'none' and c[-2] not in files:
            out.append(f'angles.md: {c[0]} thesis {c[-2]} does not resolve')
        for s in sorted(set(re.findall(r'S-\d{4}-\d\d-\d\d-\d\d', l)) - set(sigs)):
            out.append(f'angles.md: {c[0]} cites unknown signal {s}')
for b, f in sorted(files.items()):
    for s in sorted(set(re.findall(r'S-\d{4}-\d\d-\d\d-\d\d', open(f, encoding='utf-8').read())) - set(sigs)):
        out.append(f'{os.path.relpath(f, root)}: cites unknown signal {s}')

if done('0016'):
    for p, s in md.items():
        if p.startswith('topics/theses/') and 'DRAFT for Dan' in s:
            out.append(f'{p}: stale DRAFT banner')
if done('0017'):
    for b, f in sorted(files.items()):
        s = open(f, encoding='utf-8').read()
        o, c = s.count('<!-- angles:auto -->'), s.count('<!-- /angles:auto -->')
        if o != 1 or c != 1 or s.index('<!-- angles:auto -->') > s.index('<!-- /angles:auto -->'):
            out.append(f'{os.path.relpath(f, root)}: angles:auto fence missing or broken')

print('\n'.join(out))
