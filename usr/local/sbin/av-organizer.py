#!/usr/bin/env python3
"""AV organizer (r2): sort finished downloads into the Jellyfin library.

Rebuilt 2026-11-10 from the pre-r2 organizer's log (/tank/jellyfin/media/.av-organizer.log),
which ran on the old /mnt/ZUAY/AV box until 2026-08-22 and was never migrated.

Layout (under --av-root):
  TV/<Show>/Season NN/<Show> - SxxEyy[ - Title].ext   anything with SxxEyy in its name
  Movies/<Title> (<Year>)/<Title> (<Year>).ext        video files with a year
  Movies/Unsorted/<name>.ext                          video files with no year
  Misc/Unsorted/<name>.ext                            everything else (nfo, sfv, subs, images, ...)

A file whose destination already holds identical content is pruned; otherwise " (1)", " (2)" ...
is appended. Release folders emptied by a move are removed. --dry-run only logs.
"""

from __future__ import annotations

import argparse
import filecmp
import logging
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm", ".mpg", ".mpeg"}
PARTIAL_EXTS = {".!qb", ".part", ".crdownload", ".tmp"}
SKIP_DIRS = {"incomplete", "torrent-inprogress", ".tmp"}

EPISODE_RE = re.compile(r"^[Ss](\d{1,2})[Ee](\d{1,3})$")
YEAR_RE = re.compile(r"^(19\d{2}|20\d{2})$")
RES_RE = re.compile(r"^\d{3,4}[pPiI]$")
# Release-name tokens that end a title (compared lowercase).
MARKERS = {
    "2160p", "1080p", "720p", "480p", "4k", "uhd", "hdr", "sdr", "dv", "10bit", "8bit",
    "web", "web-dl", "webdl", "webrip", "bluray", "blu-ray", "brrip", "bdrip", "dvdrip", "hdtv",
    "hdrip", "camrip", "cam", "ts", "tc", "hdcam", "remux",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "h", "aac", "ac3", "dts",
    "ddp", "dd", "atmos", "truehd", "6ch",
    "amzn", "dsnp", "nf", "hulu", "hmax", "max", "atvp", "pcok", "pmtp", "now", "it",
    "repack", "proper", "internal",
    "xxx", "vr180",
}
COUNTRY = {"us", "uk", "au", "nz", "ca"}

log = logging.getLogger("av-organizer")


def tokens(stem: str) -> list[str]:
    """Split a release name into words on dots, underscores, spaces and brackets."""
    return [t for t in re.split(r"[.\s_()\[\]{}]+", stem) if t]


def is_marker(tok: str) -> bool:
    low = tok.lower()
    if low in MARKERS or RES_RE.match(tok):
        return True
    # codec/group glue like "x264-SPARKS", "DDP5", "H.264-FLUX" pieces, "AAC2"
    return bool(re.match(r"^(x26[45]|h26[45]|ddpa?\d|dd\+?\d|aac\d|ac3|dts)", low))


def tidy_case(words: list[str], source_lower: bool) -> list[str]:
    return [w[:1].upper() + w[1:] if source_lower else w for w in words]


def join_initials(words: list[str]) -> list[str]:
    """V I P -> VIP (dotted acronyms arrive as single letters)."""
    out: list[str] = []
    run: list[str] = []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) >= 2:
            out.append("".join(run).upper())
        else:
            out.extend(run)
        run = []
        if w:
            out.append(w)
    return out


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    return re.sub(r"\s+", " ", s).strip(" .-")


def episode_title(words: list[str]) -> str:
    out = []
    for w in words:
        if is_marker(w):
            break
        if w in {"&", "-", "+"}:
            continue
        if not out and w[:1].isdigit():
            return ""  # "400.P.M" style time titles: legacy dropped these
        out.append(w)
    return " ".join(join_initials(out))


def show_dir(av_root: Path, show: str) -> str:
    """Reuse an existing TV folder when the parsed name differs only by year/country/number suffix."""
    tv = av_root / "TV"
    try:
        existing = {re.sub(r"[^a-z0-9]", "", p.name.lower()): p.name for p in tv.iterdir() if p.is_dir()}
    except FileNotFoundError:
        existing = {}
    words = show.split()
    while words:
        key = re.sub(r"[^a-z0-9]", "", " ".join(words).lower())
        if key in existing:
            return existing[key]
        last = words[-1].lower()
        if YEAR_RE.match(last) or last.isdigit() or last in COUNTRY:
            words = words[:-1]
        else:
            break
    return show


def plan(src: Path, av_root: Path) -> Path:
    ext = src.suffix.lower()
    stem = src.stem
    toks = tokens(stem)
    lower = stem == stem.lower()

    # TV: any file with an SxxEyy token
    for i, t in enumerate(toks):
        m = EPISODE_RE.match(t)
        if m and i > 0:
            season, ep = int(m.group(1)), int(m.group(2))
            show_words = [w for w in toks[:i] if w not in {"&", "-"}]
            show = safe_name(" ".join(tidy_case(show_words, lower)))
            show = show_dir(av_root, show)
            title = safe_name(episode_title(tidy_case(toks[i + 1:], lower)))
            name = f"{show} - S{season:02d}E{ep:02d}" + (f" - {title}" if title else "")
            return av_root / "TV" / show / f"Season {season:02d}" / f"{name}{ext}"

    if ext in VIDEO_EXTS:
        # Movie: title words before the last year token
        years = [i for i, t in enumerate(toks) if YEAR_RE.match(t) and i > 0]
        if years:
            y = years[-1]
            words = [w for w in toks[:y] if w not in {"&", "-", "+"}]
            title = safe_name(" ".join(tidy_case(words, lower)))
            name = f"{title} ({toks[y]})"
            return av_root / "Movies" / name / f"{name}{ext}"
        return av_root / "Movies" / "Unsorted" / f"{misc_name(toks, lower)}{ext}"

    return av_root / "Misc" / "Unsorted" / f"{misc_name(toks, lower)}{ext}"


def misc_name(toks: list[str], lower: bool) -> str:
    words = []
    for w in toks:
        if is_marker(w):
            break
        words.append(re.sub(r"-\d{3,4}$", "", w))  # "vac-rgn250612gc-3072" -> "vac-rgn250612gc"
    words = tidy_case(words or toks[:1], lower)
    if words and lower:
        words[0] = words[0][:1].upper() + words[0][1:]
    return safe_name(" ".join(words)) or "unnamed"


def unique_dest(dest: Path) -> Path:
    n = 1
    cand = dest
    while cand.exists():
        cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        n += 1
    return cand


def is_stable(p: Path, settle: float) -> bool:
    try:
        st = p.stat()
    except FileNotFoundError:
        return False
    return time.time() - st.st_mtime >= settle


def candidates(inbox: Path):
    for root, dirs, files in os.walk(inbox):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = Path(root) / f
            if f.startswith(".") or p.suffix.lower() in PARTIAL_EXTS:
                continue
            yield p


def make_dirs(path: Path, owner: tuple[int, int] | None):
    missing = []
    d = path
    while not d.exists():
        missing.append(d)
        d = d.parent
    path.mkdir(parents=True, exist_ok=True)
    if owner:
        for d in missing:
            os.chown(d, *owner)


def prune_empty(dirs: set, inbox: Path):
    """Remove folders emptied by this pass, walking up to (not including) the inbox."""
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        while d != inbox and inbox in d.parents and d.name.lower() not in SKIP_DIRS:
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                break
            d = d.parent


def run_pass(inboxes: list[Path], av_root: Path, dry_run: bool, settle: float, seen: set) -> int:
    moved = pruned = 0
    owner = None
    try:
        st = av_root.stat()
        owner = (st.st_uid, st.st_gid)
    except FileNotFoundError:
        pass
    for inbox in inboxes:
        if not inbox.is_dir():
            continue
        emptied: set = set()
        for src in candidates(inbox):
            if not is_stable(src, settle):
                continue
            dest = plan(src, av_root)
            if dest.exists() and dest.stat().st_size == src.stat().st_size and filecmp.cmp(src, dest, shallow=False):
                if dry_run:
                    if src not in seen:
                        log.info("DRY prune duplicate %s (matches %s)", src, dest)
                        seen.add(src)
                    continue
                log.info("prune duplicate %s (matches %s)", src, dest)
                src.unlink()
                emptied.add(src.parent)
                pruned += 1
                continue
            dest = unique_dest(dest)
            if dry_run:
                if src not in seen:
                    log.info("DRY move %s -> %s", src, dest)
                    seen.add(src)
                continue
            make_dirs(dest.parent, owner)
            log.info("move %s -> %s", src, dest)
            shutil.move(str(src), str(dest))
            emptied.add(src.parent)
            moved += 1
        prune_empty(emptied, inbox)
    if moved or pruned:
        log.info("pass complete; files moved=%d files_pruned=%d", moved, pruned)
    return moved + pruned


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--av-root", default="/tank/jellyfin/media")
    ap.add_argument("--inbox", action="append", help="folder to sort from (repeatable); default <av-root>/_incoming")
    ap.add_argument("--log", default=None, help="log file; default <av-root>/.av-organizer.log")
    ap.add_argument("--mode", choices=["once", "monitor"], default="once")
    ap.add_argument("--interval", type=float, default=60, help="monitor mode: seconds between passes")
    ap.add_argument("--settle", type=float, default=120, help="skip files modified in the last N seconds")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan", nargs="+", metavar="NAME", help="print planned destinations for names and exit")
    args = ap.parse_args(argv)

    av_root = Path(args.av_root)
    if args.plan:
        for name in args.plan:
            print(f"{name} -> {plan(Path(name), av_root).relative_to(av_root)}")
        return 0

    inboxes = [Path(p) for p in (args.inbox or [str(av_root / "_incoming")])]
    handlers = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(args.log or str(av_root / ".av-organizer.log")))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    log.info("AV organizer starting")
    log.info("av_root=%s", av_root)
    log.info("inbox=%s", ",".join(map(str, inboxes)))
    log.info("mode=%s dry_run=%s", args.mode, args.dry_run)

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    seen: set = set()
    while True:
        try:
            run_pass(inboxes, av_root, args.dry_run, args.settle, seen)
        except Exception:
            log.exception("pass failed")
        if args.mode == "once" or stop:
            break
        for _ in range(int(args.interval)):
            if stop:
                break
            time.sleep(1)
    log.info("AV organizer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
