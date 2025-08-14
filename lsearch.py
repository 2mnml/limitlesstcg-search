#!/usr/bin/env python3
import re
import html as htmllib
import asyncio
import argparse
import time
import random
from typing import List, Tuple, Dict
from urllib.parse import urlparse, parse_qs, urlencode
from pathlib import Path
import webbrowser

import aiohttp
from aiohttp import ClientSession, ClientTimeout

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

try:
    from tqdm.asyncio import tqdm_asyncio
    from tqdm import tqdm
except Exception:
    tqdm = None
    tqdm_asyncio = None

BASE = "https://play.limitlesstcg.com"
LIST_URL = (f"{BASE}/tournaments/completed"
            "?game=PTCG&format=STANDARD&platform=all&type=online&time=4weeks&show=100")

RPS = 360
CONCURRENCY_PAGES = 12
CONCURRENCY_DECKS = 180
FAIL_ON = 1
JITTER_MAX_S = 0.005
TIMEOUT = 18
UA = "Mozilla/5.0 (LimitlessScraper/fixed-locked/4.6)"

RE_INPUT_VALUE = re.compile(r'<input[^>]*\bname=["\']input["\'][^>]*\bvalue=["\'](.*?)["\']', re.I | re.S)
NAME_FIELD_RE = re.compile(r'"name":"([^"]+)"', re.I)
RE_JS_DECKBLOCK = re.compile(r"const\s+decklist\s*=\s*`(.*?)`", re.S)
RE_DECKLIST_BLOCK = re.compile(r'<div[^>]*class="decklist"[^>]*>(.*?)</div>\s*</div>\s*</div>', re.I | re.S)
RE_ANCHOR_TEXT = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
RE_DATA_MAX = re.compile(r'<ul[^>]*class="pagination"[^>]*data-max="(\d+)"', re.I)
RE_TOURNEY_STANDINGS = re.compile(r'href="(/tournament/[^"/]+/standings)"', re.I)
RE_DECKLIST = re.compile(r'href="(/tournament/[^"/]+/player/[^"/]+/decklist)"', re.I)
RE_ARCHETYPE = re.compile(r'<div[^>]*class=["\']deck["\'][^>]*\bdata-tooltip=["\']([^"\']+)["\']', re.I)
RE_DETAILS = re.compile(
    r'<div[^>]*class=["\']details["\'][^>]*>\s*(\d+)\s+points\s*\((\d+)-(\d+)-(\d+)\)\s*(?:<i>\s*drop\s*</i>)?\s*</div>',
    re.I
)

def _set_q(url: str, key: str, value: str) -> str:
    u = urlparse(url)
    q = parse_qs(u.query, keep_blank_values=True)
    q[key] = [value]
    return u._replace(query=urlencode(q, doseq=True)).geturl()

def _safe_filename(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s or "results"

def _dedupe_keep_order(urls: List[str]) -> List[str]:
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

class FixedPacer:
    def __init__(self, rps: int):
        from collections import deque
        self.rps = max(1, int(rps))
        self.starts = deque()
        self.lock = asyncio.Lock()
        self.total_started = 0
    async def acquire(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.starts and (now - self.starts[0] >= 1.0):
                    self.starts.popleft()
                if len(self.starts) < self.rps:
                    self.starts.append(now)
                    self.total_started += 1
                    if JITTER_MAX_S > 0:
                        j = (random.random() * 2 - 1) * JITTER_MAX_S
                        if j > 0:
                            await asyncio.sleep(j)
                    return
                sleep_for = 1.0 - (now - self.starts[0])
            await asyncio.sleep(max(0.001, sleep_for))

async def _get(session: ClientSession, pacer: FixedPacer, url: str, stop_evt: asyncio.Event) -> str:
    if stop_evt.is_set():
        raise asyncio.CancelledError
    await pacer.acquire()
    try:
        async with session.get(url, timeout=ClientTimeout(total=TIMEOUT)) as r:
            r.raise_for_status()
            return await r.text()
    except Exception as e:
        if not stop_evt.is_set():
            print(f"[fail-fast] error on {url}")
            if isinstance(e, aiohttp.ClientResponseError):
                print(f"[fail-fast] HTTP {e.status}: {e.message}")
            else:
                print(f"[fail-fast] {type(e).__name__}: {e}")
            stop_evt.set()
        raise

def _max_page(html: str) -> int:
    m = RE_DATA_MAX.search(html)
    return int(m.group(1)) if m else 1

def _extract_standings(html: str) -> List[str]:
    seen, out = set(), []
    for m in RE_TOURNEY_STANDINGS.finditer(html):
        url = BASE + m.group(1)
        if url not in seen:
            seen.add(url); out.append(url)
    return out

def _extract_decks(html: str) -> List[str]:
    seen, out = set(), []
    for m in RE_DECKLIST.finditer(html):
        url = BASE + m.group(1)
        if url not in seen:
            seen.add(url); out.append(url)
    return out

def _has_card_from_hidden_json(html: str, needle: str) -> bool:
    m = RE_INPUT_VALUE.search(html)
    if not m:
        return False
    raw = htmllib.unescape(m.group(1))
    for name in NAME_FIELD_RE.findall(raw):
        if needle in name.lower():
            return True
    return False

def _has_card_from_js_block(html: str, needle: str) -> bool:
    m = RE_JS_DECKBLOCK.search(html)
    if not m:
        return False
    return needle in m.group(1).lower()

def _has_card_from_anchor_texts(html: str, needle: str) -> bool:
    m = RE_DECKLIST_BLOCK.search(html)
    block = m.group(1) if m else html
    for txt in RE_ANCHOR_TEXT.findall(block):
        if needle in htmllib.unescape(txt).lower():
            return True
    plain = re.sub(r"<[^>]+>", " ", block)
    return needle in htmllib.unescape(plain).lower()

def _deck_has_card(html: str, card_lower: str) -> bool:
    if _has_card_from_hidden_json(html, card_lower):
        return True
    if _has_card_from_js_block(html, card_lower):
        return True
    if _has_card_from_anchor_texts(html, card_lower):
        return True
    return False

def _extract_archetype(html: str) -> str:
    m = RE_ARCHETYPE.search(html)
    return m.group(1).strip() if m else "Other"

def _extract_details(html: str) -> Tuple[int, int, int, int, bool]:
    m = RE_DETAILS.search(html)
    if m:
        points = int(m.group(1)); wins = int(m.group(2)); losses = int(m.group(3)); ties = int(m.group(4))
        dropped = ("drop" in m.group(0).lower())
        return (points, wins, losses, ties, dropped)
    details_div = re.search(r'<div[^>]*class=["\']details["\'][^>]*>(.*?)</div>', html, re.I | re.S)
    if details_div:
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", details_div.group(1))).lower()
        pts = re.search(r'(\d+)\s*points', text)
        rec = re.search(r'\((\d+)-(\d+)-(\d+)\)', text)
        dropped = "drop" in text
        points = int(pts.group(1)) if pts else 0
        if rec:
            wins, losses, ties = int(rec.group(1)), int(rec.group(2)), int(rec.group(3))
        else:
            wins = losses = ties = 0
        return (points, wins, losses, ties, dropped)
    return (0, 0, 0, 0, False)

def _player_from_url(url: str) -> str:
    m = re.search(r"/player/([^/]+)/decklist", url)
    if not m:
        return ""
    slug = m.group(1)
    name = slug.replace("-", " ").strip()
    return name.title()

async def run(card: str) -> Tuple[int, int, List[Dict]]:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    conn = aiohttp.TCPConnector(
        limit=CONCURRENCY_PAGES + CONCURRENCY_DECKS,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    pacer = FixedPacer(RPS)
    stop_evt = asyncio.Event()
    error_count = 0
    matches: List[Dict] = []

    async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
        first = await _get(session, pacer, LIST_URL, stop_evt)
        maxp = _max_page(first)
        pages = [LIST_URL] + [_set_q(LIST_URL, "page", str(p)) for p in range(2, maxp + 1)]

        tourneys: List[str] = []
        sem_pages = asyncio.Semaphore(CONCURRENCY_PAGES)

        async def fetch_page(url: str):
            nonlocal error_count
            if stop_evt.is_set(): return
            try:
                async with sem_pages:
                    html = await _get(session, pacer, url, stop_evt)
                    tourneys.extend(_extract_standings(html))
            except Exception:
                error_count += 1
                stop_evt.set()

        if tqdm:
            await tqdm_asyncio.gather(*(fetch_page(u) for u in pages), desc="List pages", unit="page")
        else:
            await asyncio.gather(*(fetch_page(u) for u in pages))

        uniq_tourneys = _dedupe_keep_order(tourneys)
        if stop_evt.is_set():
            return 0, 0, matches

        deck_urls: List[str] = []
        sem_pages2 = asyncio.Semaphore(CONCURRENCY_PAGES)

        async def fetch_tourney(url: str):
            nonlocal error_count
            if stop_evt.is_set(): return
            try:
                async with sem_pages2:
                    html = await _get(session, pacer, url, stop_evt)
                    deck_urls.extend(_extract_decks(html))
            except Exception:
                error_count += 1
                stop_evt.set()

        if tqdm:
            await tqdm_asyncio.gather(*(fetch_tourney(u) for u in uniq_tourneys), desc="Tournaments", unit="t")
        else:
            await asyncio.gather(*(fetch_tourney(u) for u in uniq_tourneys))

        uniq_decks = _dedupe_keep_order(deck_urls)
        if stop_evt.is_set():
            return len(uniq_tourneys), len(uniq_decks), matches

        needle = card.strip().lower()
        sem_decks = asyncio.Semaphore(CONCURRENCY_DECKS)

        async def check_deck(url: str):
            nonlocal error_count
            if stop_evt.is_set(): return
            try:
                async with sem_decks:
                    html = await _get(session, pacer, url, stop_evt)
                    if _deck_has_card(html, needle):
                        points, wins, losses, ties, dropped = _extract_details(html)
                        denom = wins + losses
                        win_rate = (wins / denom) if denom > 0 else 0.0
                        matches.append({
                            "url": url,
                            "player": _player_from_url(url),
                            "archetype": _extract_archetype(html),
                            "points": points,
                            "wins": wins,
                            "losses": losses,
                            "ties": ties,
                            "dropped": dropped,
                            "win_rate": win_rate,
                            "played": wins + losses + ties,
                        })
            except Exception:
                error_count += 1
                if error_count >= FAIL_ON:
                    stop_evt.set()

        if tqdm:
            await tqdm_asyncio.gather(*(check_deck(u) for u in uniq_decks), desc="Decks", unit="deck")
        else:
            await asyncio.gather(*(check_deck(u) for u in uniq_decks))

        return len(uniq_tourneys), len(uniq_decks), matches

def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    mins = int(seconds // 60)
    secs = int(round(seconds - mins * 60))
    return f"{mins}:{secs:02d}"

def _render_html(card: str, tournaments: int, decks: int, matches: List[Dict], elapsed_s: float) -> str:
    matches = [m for m in matches if m["win_rate"] >= 0.40]
    groups: Dict[str, List[Dict]] = {}
    for m in matches:
        groups.setdefault(m["archetype"], []).append(m)
    for k in groups:
        groups[k].sort(key=lambda x: (x["win_rate"], x["points"], x["played"]), reverse=True)

    safe = _safe_filename(card)
    total = sum(len(v) for v in groups.values())
    elapsed = _format_elapsed(elapsed_s)

    head = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Decks with “{card}”</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {{
  --bg:#0b0f14; --fg:#e6edf3; --muted:#9fb1c1; --card:#121821; --accent:#7cc4ff; --chip:#1e2630;
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; background:var(--bg); color:var(--fg); }}
.header {{ padding:20px 24px; border-bottom:1px solid #1f2a36; position:sticky; top:0; backdrop-filter:saturate(1.2) blur(6px); background:rgba(11,15,20,.9); }}
.h1 {{ font-size:20px; margin:0 0 6px; }}
.meta {{ color:var(--muted); font-size:14px; display:flex; gap:12px; flex-wrap:wrap; }}
.container {{ max-width:1100px; margin:20px auto; padding:0 16px 40px; }}
.group {{ margin:22px 0; background:var(--card); border:1px solid #1f2a36; border-radius:14px; overflow:hidden; }}
.group-hd {{ display:flex; align-items:center; justify-content:space-between; padding:12px 16px; background:linear-gradient(180deg, #121821 0%, #0f141b 100%); border-bottom:1px solid #1f2a36; }}
.group-title {{ font-weight:600; font-size:16px; }}
.badge {{ background:var(--chip); padding:4px 8px; border-radius:999px; color:var(--muted); font-size:12px; }}
.table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
.table th, .table td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #1f2a36; font-size:14px; }}
.table th {{ color:var(--muted); font-weight:500; }}
.table a {{ color:var(--accent); text-decoration:none; }}
.table a:hover {{ text-decoration:underline; }}
.col-pct   {{ width: 10ch; }}
.col-rec   {{ width: 16ch; }}
.col-player{{ width: auto; }}
.col-link  {{ width: 14ch; }}
.pct {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
.rec {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
.drop {{ color:#ff9c9c; font-weight:600; margin-left:6px; }}
.controls {{ margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }}
input[type="search"] {{ background:#0f141b; color:var(--fg); border:1px solid #1f2a36; border-radius:10px; padding:8px 10px; outline:none; }}
.hide {{ display:none; }}
.footer {{ color:var(--muted); text-align:center; padding:20px 0 40px; }}
</style>
</head>
<body>
<div class="header">
  <div class="h1">Decks containing “{card}”</div>
  <div class="meta">
    <div><strong>{total}</strong> matches · grouped by archetype</div>
    <div>{tournaments} tournaments · {decks} deck pages scanned</div>
    <div>Elapsed: {elapsed}</div>
  </div>
  <div class="controls">
    <input id="filter" type="search" placeholder="Filter by archetype or player…">
  </div>
</div>
<div class="container">
"""
    rows = []
    for arch in sorted(groups.keys()):
        block = [(
            f'<div class="group">'
            f'<div class="group-hd"><div class="group-title">{arch}</div><div class="badge">{len(groups[arch])}</div></div>'
            f'<table class="table">'
            f'<colgroup><col class="col-pct"><col class="col-rec"><col class="col-player"><col class="col-link"></colgroup>'
            f'<thead><tr><th>Win %</th><th>Record</th><th>Player</th><th>Link</th></tr></thead><tbody>'
        )]
        for m in groups[arch]:
            pct = f"{m['win_rate']*100:.2f}%"
            rec = f"{m['wins']}-{m['losses']}-{m['ties']}"
            if m.get("dropped"):
                rec += ' <span class="drop">Drop</span>'
            player = (m.get("player") or "").strip() or "—"
            block.append(
                f"<tr data-arch=\"{arch.lower()}\" data-player=\"{player.lower()}\">"
                f"<td class=\"pct\">{pct}</td>"
                f"<td class=\"rec\">{rec}</td>"
                f"<td>{player}</td>"
                f"<td><a href=\"{m['url']}\" target=\"_blank\">Open deck</a></td>"
                f"</tr>"
            )
        block.append("</tbody></table></div>")
        rows.append("\n".join(block))

    tail = """
</div>
<div class="footer">Generated locally · Use your browser’s “Print → Save as PDF” to export</div>
<script>
const q = document.getElementById('filter');
q.addEventListener('input', () => {
  const needle = q.value.trim().toLowerCase();
  const rows = document.querySelectorAll('tbody tr');
  rows.forEach(tr => {
    if (!needle) { tr.classList.remove('hide'); return; }
    const arch = tr.getAttribute('data-arch') || '';
    const player = tr.getAttribute('data-player') || '';
    tr.classList.toggle('hide', !(arch.includes(needle) || player.includes(needle)));
  });
});
</script>
</body>
</html>
"""
    return head + "\n".join(rows) + tail

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()

    t0 = time.time()
    tournaments, decks, matches = asyncio.run(run(args.card))
    elapsed = time.time() - t0

    html = _render_html(args.card, tournaments, decks, matches, elapsed)
    out_path = Path(f"{_safe_filename(args.card)}.html").resolve()
    out_path.write_text(html, encoding="utf-8")

    print(f"Tournaments:   {tournaments}")
    print(f"Decks:         {decks}")
    print(f"Matches:       {sum(1 for m in matches if m['win_rate'] >= 0.40)}")
    print(f"Elapsed Time:  {_format_elapsed(elapsed)}")
    print(f"Output:        {out_path}")

    try:
        webbrowser.open(out_path.as_uri())
    except Exception:
        pass

if __name__ == "__main__":
    main()
