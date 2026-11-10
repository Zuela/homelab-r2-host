"""Readable HTML for alerts in the Matrix room (Dan 2026-11-03). Bold title, problems as a list, healthy items folded into one line.
Alert text can come from anywhere, so all of it is escaped. The plain body stays as the fallback."""
from html import escape


def render(title, message):
    bad, fine, other = [], [], []
    for line in message.splitlines():
        if line.startswith('BAD  '):
            bad.append(line[5:])
        elif line.startswith('ok   '):
            fine.append(line[5:].split(':')[0])
        elif line.strip():
            other.append(line.strip())
    parts = [f'<strong>{escape(title)}</strong>'] if title else []
    parts += [f'<p>{escape(x)}</p>' for x in other]
    if bad:
        parts.append('<ul>' + ''.join(f'<li>{escape(x)}</li>' for x in bad) + '</ul>')
    if fine:
        parts.append(f'<p><em>Fine: {escape(", ".join(fine))}</em></p>')
    return ''.join(parts)
