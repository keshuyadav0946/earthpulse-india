#!/usr/bin/env python3
"""
EarthPulse India — fully automatic live-data updater.

Runs inside GitHub Actions on a schedule. For each of the five sites it
fetches the freshest data from the official source, validates it, and
patches ONLY the live-data layer inside that site's index.html (the data
objects the pages already render from). Archive/history records are never
touched. If a source fails or the data does not validate, that site is
left completely unchanged (its data stays honestly dated) and the run
continues with the next site.

Usage: python3 update_all.py [fire|cyclone|flood|landslide|coldwave|all]
Exit code is 0 even when a site is skipped; a summary is printed.
"""
import base64
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def log(site, msg):
    print(f'[{site}] {msg}', flush=True)


def fetch(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        'User-Agent': 'EarthPulse-AutoRefresh/1.0 (+https://keshuyadav0946.github.io/earthpulse-india/)'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fmt_ist(dt):
    """25 Oct 2026, 05:11 PM IST style."""
    return dt.strftime('%d %b %Y, %I:%M %p IST').replace(' 0', ' ', 1).replace(
        f'{dt.day:02d}', str(dt.day), 1) if False else \
        f'{dt.day} {MONTHS[dt.month - 1]} {dt.year}, {dt.strftime("%I:%M %p").lstrip("0")} IST'


def fmt_date(dt):
    return f'{dt.day} {MONTHS[dt.month - 1]} {dt.year}'


# ---------------------------------------------------------------------------
# index.html patching helpers
# ---------------------------------------------------------------------------

def load_page(site):
    path = os.path.join(ROOT, site, 'index.html')
    with open(path, encoding='utf-8') as f:
        return path, f.read()


def save_page(path, html):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


def find_b64_script(html, marker):
    """Return (full_match, decoded_text) of the base64 script whose decoded
    content contains marker, else (None, None)."""
    for m in re.finditer(r'<script src="data:application/javascript;base64,([A-Za-z0-9+/=]+)"></script>', html):
        try:
            dec = base64.b64decode(m.group(1)).decode('utf-8')
        except Exception:
            continue
        if marker in dec:
            return m, dec
    return None, None


def replace_b64_script(html, match, new_text):
    new_b64 = base64.b64encode(new_text.encode('utf-8')).decode('ascii')
    return html[:match.start(1)] + new_b64 + html[match.end(1):]


def extract_js_object(text, start_idx):
    """Extract a balanced {...} or [...] literal starting at start_idx
    (which must point at the opening brace). Returns (literal, end_idx)."""
    open_c = text[start_idx]
    close_c = '}' if open_c == '{' else ']'
    depth = 0
    i = start_idx
    in_str = None
    esc = False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == in_str:
                in_str = None
        else:
            if c in ('"', "'", '`'):
                in_str = c
            elif c == open_c:
                depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    return text[start_idx:i + 1], i + 1
        i += 1
    raise ValueError('unbalanced literal')


def replace_js_assignment(text, var_pattern, new_literal):
    """Find `var X = <literal>` (var_pattern is a regex ending at the '='),
    and replace the literal. Returns new text."""
    m = re.search(var_pattern, text)
    if not m:
        raise ValueError(f'assignment not found: {var_pattern}')
    i = m.end()
    while text[i] in ' \t\r\n':
        i += 1
    _, end = extract_js_object(text, i)
    return text[:i] + new_literal + text[end:]


def js_json(obj):
    """Compact JSON that is also valid JS (matches the pages' style)."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def transform_b64_scripts(html, fn):
    """Apply fn(decoded_text)->new_text|None to every base64 script;
    scripts for which fn returns None are left byte-identical."""
    parts = []
    last = 0
    changed = False
    for m in re.finditer(r'<script src="data:application/javascript;base64,([A-Za-z0-9+/=]+)"></script>', html):
        try:
            dec = base64.b64decode(m.group(1)).decode('utf-8')
        except Exception:
            continue
        new = fn(dec)
        if new is not None and new != dec:
            parts.append(html[last:m.start(1)])
            parts.append(base64.b64encode(new.encode('utf-8')).decode('ascii'))
            last = m.end(1)
            changed = True
    if not changed:
        return html
    parts.append(html[last:])
    return ''.join(parts)


def date_range_text(dates):
    d1 = datetime.strptime(dates[0], '%Y-%m-%d')
    d2 = datetime.strptime(dates[-1], '%Y-%m-%d')
    if len(dates) == 1 or (d1.month == d2.month and d1.year == d2.year and d1.day == d2.day):
        return fmt_date(d1)
    if d1.month == d2.month and d1.year == d2.year:
        return f'{d1.day}–{d2.day} {MONTHS[d1.month - 1]} {d1.year}'
    return f'{fmt_date(d1)} – {fmt_date(d2)}'


def ist_variants(s):
    """The pages write IST times both as '05:11 PM' and '5:11 PM'."""
    out = {s}
    m = re.search(r', 0(\d:\d\d [AP]M IST)$', s)
    if m:
        out.add(s[:m.start()] + ', ' + m.group(1))
    return out


# ---------------------------------------------------------------------------
# FIRE — NASA FIRMS -> FIREDB.hotspots + FIREDB.stats (SOS/globe/counts are
# computed by the page itself from this data, so this is the whole update)
# ---------------------------------------------------------------------------

FIRMS_SENSORS = ['VIIRS_NOAA20_NRT', 'VIIRS_NOAA21_NRT', 'MODIS_NRT']
INDIA_BBOX = '68,6,98,37'

# Simplified India outline (lon, lat) — coarse land filter so detections in
# neighbouring countries / ocean inside the bbox do not enter the data.
INDIA_POLY = [
    (68.2, 23.7), (69.5, 22.3), (72.6, 20.0), (72.9, 18.9), (73.5, 16.0),
    (74.8, 13.3), (76.0, 10.3), (77.4, 8.1), (78.2, 9.3), (79.3, 10.3),
    (80.3, 13.4), (80.9, 15.9), (82.3, 16.5), (84.0, 18.3), (86.4, 20.1),
    (87.0, 21.7), (88.0, 21.7), (89.0, 22.0), (89.0, 23.5), (88.4, 24.5),
    (88.1, 25.3), (88.9, 26.0), (89.6, 25.9), (90.5, 25.2), (91.0, 25.3),
    (92.0, 25.2), (92.3, 24.1), (93.0, 23.6), (94.2, 23.7), (94.7, 24.6),
    (95.2, 25.6), (95.9, 27.0), (96.2, 27.8), (97.0, 28.2), (97.4, 28.3),
    (96.3, 29.3), (95.0, 29.4), (94.6, 28.9), (93.8, 28.3), (92.6, 28.0),
    (92.1, 27.9), (91.9, 27.3), (91.0, 26.8), (90.4, 27.0), (89.6, 26.9),
    (88.9, 27.0), (88.0, 27.1), (88.0, 26.6), (87.9, 26.2), (86.9, 26.6),
    (85.3, 26.8), (84.8, 27.3), (83.9, 27.2), (83.0, 27.0), (81.9, 27.6),
    (81.3, 28.3), (80.9, 28.5), (80.3, 29.0), (79.8, 29.6), (79.5, 30.1),
    (79.2, 30.7), (78.9, 31.1), (78.4, 31.3), (78.0, 31.9), (77.8, 32.4),
    (77.5, 32.9), (76.9, 33.0), (76.3, 33.5), (75.8, 34.0), (75.3, 34.5),
    (74.9, 34.6), (74.5, 34.1), (74.0, 34.5), (73.9, 34.9), (73.5, 35.0),
    (73.9, 35.4), (74.5, 35.9), (74.8, 36.5), (74.9, 37.0), (74.5, 36.9),
    (73.8, 36.8), (73.1, 36.6), (72.8, 36.4), (72.5, 35.9), (72.1, 35.5),
    (71.7, 35.3), (71.5, 34.7), (71.1, 34.6), (70.9, 34.0), (70.6, 33.8),
    (70.1, 33.5), (69.9, 33.0), (70.0, 32.4), (70.3, 31.9), (70.1, 31.3),
    (69.6, 30.9), (69.3, 30.3), (69.0, 29.7), (68.9, 28.9), (68.9, 28.2),
    (68.2, 27.2), (68.2, 26.0), (68.2, 24.7),
]


def in_india(lat, lon):
    # ray casting on INDIA_POLY (x=lon, y=lat)
    inside = False
    n = len(INDIA_POLY)
    for i in range(n):
        x1, y1 = INDIA_POLY[i]
        x2, y2 = INDIA_POLY[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xin:
                inside = not inside
    return inside


def fire_zone(lat, lon):
    if lon >= 88.5 and lat >= 21.0:
        return 'Northeast'
    if lat >= 29.0:
        return 'North & Northwest'
    if lon <= 75.0:
        return 'West'
    if lat <= 16.0:
        return 'South'
    if lon >= 84.0:
        return 'East'
    return 'Central'


def update_fire():
    site = 'fire'
    key = os.environ.get('FIRMS_MAP_KEY', '').strip()
    if not key:
        log(site, 'FIRMS_MAP_KEY not set — skipped')
        return False
    raw_rows = []
    for sensor in FIRMS_SENSORS:
        url = f'https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{sensor}/{INDIA_BBOX}/2'
        try:
            text = fetch(url, timeout=90).decode('utf-8', errors='replace')
        except Exception as e:
            log(site, f'{sensor} fetch failed: {e} — skipped')
            return False
        lines = [ln for ln in text.strip().split('\n') if ln.strip()]
        if not lines or 'latitude' not in lines[0]:
            log(site, f'{sensor} unexpected response — skipped')
            return False
        hdr = lines[0].split(',')
        idx = {name: hdr.index(name) for name in
               ('latitude', 'longitude', 'acq_date', 'acq_time', 'frp') if name in hdr}
        if len(idx) < 5:
            log(site, f'{sensor} missing columns — skipped')
            return False
        for ln in lines[1:]:
            c = ln.split(',')
            try:
                raw_rows.append((float(c[idx['latitude']]), float(c[idx['longitude']]),
                                 c[idx['acq_date']], int(c[idx['acq_time']]),
                                 float(c[idx['frp']])))
            except Exception:
                continue
    if not raw_rows:
        log(site, 'no rows returned by FIRMS (possible UTC rollover) — skipped, old data kept')
        return False
    # India land filter + cross-sensor dedupe (30-min bucket, ~1 km cell,
    # keep the strongest FRP reading)
    best = {}
    for lat, lon, d, t, frp in raw_rows:
        if not in_india(lat, lon):
            continue
        hh, mm = divmod(t, 100)
        minutes = hh * 60 + mm
        k = (round(lat, 2), round(lon, 2), d, minutes // 30)
        if k not in best or frp > best[k][4]:
            best[k] = (lat, lon, d, minutes, frp)
    deduped = sorted(best.values(), key=lambda r: (r[2], r[3], r[0]))
    dates = sorted({r[2] for r in deduped})
    didx = {d: i for i, d in enumerate(dates)}
    points = [[round(r[0], 4), round(r[1], 4), round(r[4], 2), didx[r[2]], r[3]]
              for r in deduped]

    def pts_ts(p):
        y, mo, da = map(int, dates[p[3]].split('-'))
        return datetime(y, mo, da, tzinfo=timezone.utc).timestamp() + (p[4] or 0) * 60

    fetched = datetime.now(timezone.utc)
    fetched_epoch = int(fetched.timestamp())
    cutoff = fetched_epoch - 86400
    freshest_epoch = int(max(pts_ts(p) for p in points))
    live = [p for p in points if pts_ts(p) >= cutoff]
    frps = sorted(p[2] for p in points)
    live_frps = [p[2] for p in live]
    zones = {}
    for p in live:
        z = fire_zone(p[0], p[1])
        zones[z] = zones.get(z, 0) + 1
    by_date = {d: sum(1 for p in points if dates[p[3]] == d) for d in dates}

    path, html = load_page(site)
    match, dec = find_b64_script(html, 'window.FIREDB=')
    if match is None:
        log(site, 'FIREDB script not found — skipped')
        return False
    db = json.loads(dec[len('window.FIREDB='):].rstrip().rstrip(';'))
    old_stats = db.get('stats', {})
    old_hs = db.get('hotspots', {})
    db['hotspots'] = {
        'source': 'NASA FIRMS VIIRS (NOAA-20 + NOAA-21) + MODIS (Terra + Aqua) NRT',
        'fetched_for': 'India region (68E-98E, 6N-37N), last 2 days',
        'dates': dates,
        'fetched_utc': fetched.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'fetched_epoch': fetched_epoch,
        'fetched_ist': fmt_ist(fetched.astimezone(IST)),
        'freshest_utc': datetime.fromtimestamp(freshest_epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'freshest_epoch': freshest_epoch,
        'freshest_ist': fmt_ist(datetime.fromtimestamp(freshest_epoch, IST)),
        'live_cutoff_epoch': cutoff,
        'live_count': len(live),
        'points': points,
    }
    db['stats'] = {
        'total': len(points),
        'raw_total': len(raw_rows),
        'by_date': by_date,
        'zones': zones,
        'strong': sum(1 for f in frps if f >= 15),
        'max_frp': max(frps),
        'median_frp': frps[len(frps) // 2],
        'events_total': old_stats.get('events_total', len(db.get('events', []))),
        'events_deaths': old_stats.get('events_deaths', 0),
        'states': old_stats.get('states', 0),
        'with_photo': old_stats.get('with_photo', 0),
        'first_year': old_stats.get('first_year', ''),
        'last_year': old_stats.get('last_year', ''),
        'live_count': len(live),
        'live_strong': sum(1 for f in live_frps if f >= 15),
        'live_max_frp': max(live_frps) if live_frps else 0,
    }
    new_dec = 'window.FIREDB=' + js_json(db)
    html = replace_b64_script(html, match, new_dec)
    hs = db['hotspots']
    # Every other place the snapshot dates are written (hero slots in the raw
    # HTML, the language packs, the bot's knowledge, the SOS text templates):
    # swap the OLD snapshot dates for the NEW ones. All occurrences of these
    # exact strings refer to the snapshot, never to archive records.
    def swap_dates(text):
        out = text
        for field in ('freshest_ist', 'fetched_ist'):
            if not old_hs.get(field):
                continue
            olds = ist_variants(old_hs[field])
            news = ist_variants(hs[field])
            for ov in olds:
                for nv in news:
                    if len(nv) == len(ov) or ((' 0' in ov) == (' 0' in nv)):
                        out = out.replace(ov, nv)
                        break
                else:
                    out = out.replace(ov, hs[field])
        if old_hs.get('dates'):
            out = out.replace(date_range_text(old_hs['dates']), date_range_text(dates))
            # standalone day mentions (e.g. the bot's "sabse taaza guzar
            # (25 Oct 2026)") — map each snapshot day to the new day in the
            # same position; runs after the range swap so ranges stay intact
            for od, nd in zip(old_hs['dates'], dates):
                out = out.replace(fmt_date(datetime.strptime(od, '%Y-%m-%d')),
                                  fmt_date(datetime.strptime(nd, '%Y-%m-%d')))
        return out
    html = transform_b64_scripts(html, lambda dec: swap_dates(dec) if 'window.FIREDB=' not in dec[:20] else None)
    html = swap_dates(html)
    html = re.sub(r'sabse taaza guzar [^·]+· snapshot liya gaya [^<]+',
                  f'sabse taaza guzar {hs["freshest_ist"]} · snapshot liya gaya {hs["fetched_ist"]}',
                  html, count=1)
    if html == open(path, encoding='utf-8').read():
        log(site, 'no change')
        return False
    save_page(path, html)
    log(site, f'updated: {len(points)} points ({len(raw_rows)} raw), live {len(live)}, freshest {hs["freshest_ist"]}')
    return True


# ---------------------------------------------------------------------------
# Shared helpers for the remaining sites
# ---------------------------------------------------------------------------

def pdf_text(data_bytes):
    """Extract text from PDF bytes (pypdf). Returns '' on failure."""
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data_bytes))
        return '\n'.join((p.extract_text() or '') for p in r.pages)
    except Exception:
        return ''


def extract_js_string(text, start_idx):
    """start_idx points at the opening quote (' or \"). Returns (value, end)."""
    q = text[start_idx]
    i = start_idx + 1
    out = []
    while i < len(text):
        c = text[i]
        if c == '\\':
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == q:
            return ''.join(out), i + 1
        out.append(c)
        i += 1
    raise ValueError('unterminated string')


def replace_string_literal(text, prefix, new_value, quote="'"):
    """Find a string literal whose content starts with prefix and replace the
    whole literal with new_value (JS-escaped). Returns new text or None."""
    idx = text.find(prefix)
    if idx < 0:
        return None
    # walk back to the opening quote
    qpos = idx - 1
    while qpos >= 0 and text[qpos] not in ('"', "'"):
        qpos -= 1
    if qpos < 0:
        return None
    q = text[qpos]
    _, end = extract_js_string(text, qpos)
    esc = new_value.replace('\\', '\\\\').replace(q, '\\' + q).replace('\n', '\\n')
    return text[:qpos] + q + esc + q + text[end:]


def hhmm_to_12h(hhmm):
    hhmm = hhmm.replace(':', '')
    h, m = int(hhmm[:2]), int(hhmm[2:4])
    ap = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f'{h12}:{m:02d} {ap}'


def clean_ws(s):
    return re.sub(r'\s+', ' ', s or '').strip()


# ---------------------------------------------------------------------------
# CYCLONE — IMD National Bulletin (mausam.imd.gov.in) -> LIVE / EMG objects,
# hero, live-cloud popups, marker labels, i18n live summaries
# ---------------------------------------------------------------------------

CYCLONE_PAGE = 'https://mausam.imd.gov.in/imd_latest/contents/cyclone.php'
CYCLONE_BASE = 'https://mausam.imd.gov.in/'

COASTAL_PLACES = {
    'nellore': (14.44, 80.03), 'narsapur': (16.43, 81.68), 'machilipatnam': (16.18, 81.13),
    'ongole': (15.50, 80.05), 'kakinada': (16.93, 82.23), 'visakhapatnam': (17.68, 83.22),
    'kalingapatnam': (18.34, 84.12), 'gopalpur': (19.27, 84.92), 'puri': (19.80, 85.83),
    'paradip': (20.26, 86.68), 'dhamra': (20.79, 86.92), 'balasore': (21.49, 86.93),
    'sagar island': (21.65, 88.08), 'haldia': (22.03, 88.06), 'chennai': (13.08, 80.27),
    'puducherry': (11.93, 79.83), 'nagapattinam': (10.77, 79.84), 'thoothukudi': (8.81, 78.20),
    'tuticorin': (8.81, 78.20), 'kavali': (14.92, 79.99), 'bapatla': (15.90, 80.47),
    'chirala': (15.82, 80.35), 'yanam': (16.73, 82.22), 'tuni': (17.36, 82.55),
    'thiruvananthapuram': (8.48, 76.95), 'kochi': (9.97, 76.28), 'kozhikode': (11.25, 75.77),
    'mangaluru': (12.87, 74.88), 'karwar': (14.81, 74.13), 'panaji': (15.49, 73.83),
    'ratnagiri': (16.99, 73.30), 'mumbai': (19.08, 72.88), 'surat': (21.17, 72.83),
    'veraval': (20.92, 70.36), 'porbandar': (21.64, 69.61), 'dwarka': (22.24, 68.97),
}

DIR_HINGLISH = {
    'north': 'uttar', 'south': 'dakshin', 'east': 'poorv', 'west': 'paschim',
    'northeast': 'uttar-poorv', 'northwest': 'uttar-paschim',
    'southeast': 'dakshin-poorv', 'southwest': 'dakshin-paschim',
    'north-northeast': 'uttar-uttar-poorv', 'north-northwest': 'uttar-uttar-paschim',
    'south-southeast': 'dakshin-dakshin-poorv', 'south-southwest': 'dakshin-dakshin-paschim',
    'east-northeast': 'poorv-uttar-poorv', 'east-southeast': 'poorv-dakshin-poorv',
    'west-northwest': 'paschim-uttar-paschim', 'west-southwest': 'paschim-dakshin-paschim',
}


def dir_hinglish(phrase):
    p = phrase.strip().lower()
    return DIR_HINGLISH.get(p, p)


def parse_imd_bulletin(text):
    """Parse an IMD National Bulletin's text. Returns dict or None."""
    # pypdf fragments this DOCX-born PDF word-by-word; flatten whitespace and
    # repair split hyphenated words ("north- northwestwards").
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'(\w)- (\w)', r'\1-\2', text)
    if 'NATIONAL BULLETIN NO' not in text:
        return None
    b = {}
    m = re.search(r'NATIONAL BULLETIN NO\.?\s*(\d+)\s*\((BOB|ARB)/\s*(\d+)\s*/\s*(\d{4})\)', text)
    if not m:
        return None
    b['no'] = int(m.group(1))
    b['basin'] = f'{m.group(2)} {int(m.group(3)):02d}'
    m = re.search(r'TIME OF ISSUE:\s*(\d{4})\s*HOURS IST', text)
    b['issue_hhmm'] = m.group(1) if m else None
    m = re.search(r'DATED:\s*(\d{2})\.(\d{2})\.(\d{4})', text)
    if not m:
        return None
    b['date'] = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    # Subject line: from "Sub:" to where the body repeats it (" The Severe
    # Cyclonic Storm ..."), falling back to the warning bracket's close.
    subj = ''
    sm0 = re.search(r'Sub:\s*(.+?)(?=\sThe (?:Extremely Severe |Very Severe |Severe |Super )?Cyclonic Storm|\sThe Deep Depression|\sThe Depression)', text)
    if sm0:
        subj = sm0.group(1)
    else:
        sm0 = re.search(r'Sub:\s*(.+?)\]\s', text)
        if sm0:
            subj = sm0.group(1) + ']'
    subj = clean_ws(subj)
    sm = re.search(r'(Extremely Severe Cyclonic Storm|Very Severe Cyclonic Storm|Severe Cyclonic Storm|Super Cyclonic Storm|Cyclonic Storm|Deep Depression|Depression)\s*[“"]([^”"]+)[”"]', subj)
    if sm:
        b['stage'] = sm.group(1)
        b['name'] = sm.group(2).strip().title()
    else:
        sm2 = re.search(r'(Deep Depression|Depression|Well[- ]Marked Low Pressure Area|Low Pressure Area)', subj, re.I)
        if not sm2:
            return None
        b['stage'] = sm2.group(1).title()
        b['name'] = None
    b['warning'] = ''
    for bm in re.finditer(r'\[(.+?)\]', subj):
        if 'Warning' in bm.group(1) or 'Alert' in bm.group(1) or 'Message' in bm.group(1):
            b['warning'] = clean_ws(bm.group(1))
    # Body: centred position
    pm = re.search(r'lay centered at (\d{4}) hours IST of today, the \d+\s*(?:st|nd|rd|th) \w+, \d{4}.*?near Latitude ([\d.]+)°N and Longitude ([\d.]+)°E,?\s*(about .+?)\.\s', text)
    if not pm:
        return None
    b['centre_hhmm'] = pm.group(1)
    b['lat'] = float(pm.group(2))
    b['lon'] = float(pm.group(3))
    b['distances'] = clean_ws(pm.group(4))
    mm = re.search(r'moved ([\w-]+?)wards? with the speed of (\d+) kmph during past 0?6 hours', text)
    if mm:
        b['move_dir'] = mm.group(1)
        b['move_speed'] = int(mm.group(2))
    # Crossing / landfall forecast sentence
    cm = re.search(r'likely to cross (.+?)\.\s', text, re.S)
    b['cross_sentence'] = clean_ws(cm.group(1)) if cm else ''
    xm = re.search(r'between (.+?) and (.+?),?\s+by (.+?) of (\d{1,2})\s*(?:st|nd|rd|th) (\w+),?\s*(\d{4})', b['cross_sentence'])
    if xm:
        b['cross_a'] = clean_ws(xm.group(1))
        b['cross_b'] = clean_ws(xm.group(2))
        b['cross_when'] = clean_ws(xm.group(3))
        b['cross_day'] = int(xm.group(4))
        b['cross_month'] = xm.group(5)
        b['cross_year'] = int(xm.group(6))
    wm2 = re.search(r'maximum sustained wind speed of ([\d-]+) kmph gusting to (\d+) kmph', b['cross_sentence'])
    if wm2:
        b['cross_winds'] = wm2.group(1)
        b['cross_gust'] = wm2.group(2)
    sm3 = re.search(r'as a ([a-z ]+?cyclonic storm|[a-z ]+?depression)', b['cross_sentence'], re.I)
    if sm3:
        b['cross_stage'] = clean_ws(sm3.group(1)).title()
    # Current winds: first wind range in the forecast table region
    tw = re.search(r'Forecast location and intensity.*?(\d+)-(\d+) gusting to (\d+)', text)
    if tw:
        b['winds'] = f'{tw.group(1)}-{tw.group(2)}'
        b['gust'] = tw.group(3)
    elif wm2:
        b['winds'] = wm2.group(1)
        b['gust'] = wm2.group(2)
    # Storm surge
    sg = re.search(r'[Ss]torm surge of (?:about )?([\d.]+)(?:\s*(?:to|-)\s*([\d.]+))?\s*m', text)
    if sg:
        b['surge'] = (sg.group(1), sg.group(2))
    # Rainfall headline
    rm = re.search(r'(Extremely heavy rainfall|Very heavy rainfall|Heavy rainfall)[^\n.]*', text)
    b['rain_line'] = clean_ws(rm.group(0)) if rm else ''
    # Dissipation / final bulletin signals
    low = text.lower()
    b['final'] = ('last bulletin' in low or 'no further bulletin' in low or
                  'weakened into a well-marked low pressure area' in low or
                  'weakened into a well marked low pressure area' in low)
    return b


def cyclone_templates(b):
    """Build all replacement texts from parsed bulletin b."""
    dstr = fmt_date(b['date'])
    tstr = hhmm_to_12h(b['issue_hhmm']) if b['issue_hhmm'] else ''
    cstr = hhmm_to_12h(b['centre_hhmm'])
    stage = b['stage']
    name = b['name']
    disp = f"{stage} ‘{name}’" if name else stage
    disp_plain = f"{stage} {name}" if name else stage
    sea = 'Arabian Sea' if b['basin'].startswith('ARB') else 'Bay of Bengal'
    sea_hi = 'Arab Samundar' if sea == 'Arabian Sea' else 'Bay of Bengal'
    # distances -> Hinglish list
    dist_items = []
    for part in re.split(r',\s*|\s+and\s+', b['distances'].replace('about ', '', 1)):
        dm = re.match(r'(\d+)\s*km\s+([\w-]+)\s+of\s+(.+?)(?:\s*\(|$)', part.strip())
        if dm:
            dist_items.append(f"{dm.group(3).strip()} se lagbhag {dm.group(1)} km {dir_hinglish(dm.group(2))}")
    dist_hi = ', '.join(dist_items)
    cross = b.get('cross_sentence', '')
    warn = b.get('warning', '')
    warn_short = re.sub(r'\s*[—-]\s*(Red|Orange|Yellow) Message.*$', '', warn)
    warn_level = ''
    wm = re.search(r'(Red|Orange|Yellow) Message', warn)
    if wm:
        warn_level = wm.group(1)
    surge_txt = ''
    if b.get('surge'):
        lo, hi = b['surge']
        surge_txt = f"Storm surge lagbhag {lo}" + (f"–{hi}" if hi else "") + " m tak ka anumaan hai."
    note = (f"IMD National Bulletin No. {b['no']} ({dstr}, {tstr} IST): {disp_plain} "
            f"lay centered at {cstr} IST near {b['lat']}°N, {b['lon']}°E — {b['distances']}. "
            + (f"It is likely to cross {cross}. " if cross else '')
            + (surge_txt + ' ' if surge_txt else '')
            + f"(Bulletin ka dated snapshot hai — taaza sthiti ke liye IMD ka naya bulletin dekho.)")
    landfall_short = ''
    marker_lat = marker_lon = None
    if b.get('cross_a'):
        landfall_short = f"{b['cross_a']}–{b['cross_b']}"
        pa = COASTAL_PLACES.get(b['cross_a'].strip().lower())
        pb = COASTAL_PLACES.get(b['cross_b'].strip().lower())
        if pa and pb:
            marker_lat = round((pa[0] + pb[0]) / 2, 2)
            marker_lon = round((pa[1] + pb[1]) / 2, 2)
        elif pa or pb:
            marker_lat, marker_lon = pa or pb
        cross_date = f"{b['cross_day']} {b['cross_month'][:3]} {b['cross_year']}"
        emg_landfall = f"FORECAST {cross_date} · {b['cross_when']}"
        marker_label = f"FORECAST landfall zone · {landfall_short} · {b['cross_when']} {b['cross_day']} {b['cross_month'][:3]}"
    else:
        emg_landfall = 'FORECAST — landfall abhi tay nahi'
        marker_label = ''
    emg_snapshot = (f"{dstr}, {tstr} IST — IMD National Bulletin No. {b['no']}: {disp_plain} active at "
                    f"{b['lat']}°N, {b['lon']}°E (winds {b.get('winds','?')} km/h, gusting {b.get('gust','?')}); "
                    f"{warn_short}"
                    + (f"; FORECAST: {b.get('cross_stage', stage).lower()}, landfall {landfall_short} "
                       f"({b.get('cross_when','')}, {b.get('cross_day','')} {b.get('cross_month','')}) — landfall has NOT happened yet"
                       if b.get('cross_a') else ''))
    return {
        'dstr': dstr, 'tstr': tstr, 'cstr': cstr, 'disp': disp, 'disp_plain': disp_plain,
        'sea': sea, 'sea_hi': sea_hi, 'dist_hi': dist_hi, 'note': note,
        'landfall_short': landfall_short, 'emg_landfall': emg_landfall,
        'emg_snapshot': emg_snapshot, 'marker_label': marker_label,
        'marker_lat': marker_lat, 'marker_lon': marker_lon,
        'warn_short': warn_short, 'warn_level': warn_level, 'surge_txt': surge_txt,
    }


def update_cyclone():
    site = 'cyclone'
    try:
        page = fetch(CYCLONE_PAGE, timeout=45).decode('utf-8', errors='replace')
    except Exception as e:
        log(site, f'IMD cyclone page fetch failed: {e} — skipped')
        return False
    m = re.search(r'backend/assets/cyclone_pdf/(\d+)__National_Bulletin_No_(\d+)-(\d{2})([A-Za-z]{3})(\d{4})_(\d{4})IST_docx\.pdf', page)
    path, html = load_page(site)
    sm, s6 = find_b64_script(html, 'var LIVE=')
    if sm is None:
        log(site, 'LIVE script not found — skipped')
        return False
    old_live_m = re.search(r'var LIVE=(\{.*?\});', s6, re.S)
    old_name = None
    if old_live_m:
        nm = re.search(r'"name":\s*"([^"]+)"', old_live_m.group(1)) or re.search(r'name:"([^"]+)"', old_live_m.group(1))
        old_name = nm.group(1) if nm else None
    if not m:
        log(site, 'no National Bulletin link on IMD page — skipped (dissipation not assumed)')
        return False
    pdf_url = CYCLONE_BASE + 'backend/assets/cyclone_pdf/' + m.group(0).split('cyclone_pdf/')[1]
    try:
        pdf = fetch(pdf_url, timeout=90)
    except Exception as e:
        log(site, f'bulletin PDF fetch failed: {e} — skipped')
        return False
    text = pdf_text(pdf)
    b = parse_imd_bulletin(text)
    if not b:
        log(site, 'bulletin parse failed — skipped')
        return False
    if b['no'] and old_live_m and f"Bulletin No. {b['no']}" in old_live_m.group(1) and b.get('final') is False:
        log(site, f"bulletin {b['no']} already published — no change")
        return False
    t = cyclone_templates(b)
    is_storm = b['stage'] not in ('Depression', 'Deep Depression', 'Well Marked Low Pressure Area', 'Low Pressure Area')
    calm = b['final'] and not is_storm
    # --- LIVE object ---
    if calm:
        live = {'active': False, 'system': None, 'depression': None,
                'snapshot': f"{t['dstr']} · Bulletin No. {b['no']} · {t['tstr']} IST",
                'source': f"RSMC New Delhi / IMD — National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST)"}
    elif is_storm:
        live = {'active': True,
                'system': {'name': b['name'], 'state': f"LIVE · {b['stage']}", 'lat': b['lat'], 'lon': b['lon'], 'note': t['note']},
                'depression': None,
                'snapshot': f"{t['dstr']} · Bulletin No. {b['no']} · {t['tstr']} IST",
                'source': f"RSMC New Delhi / IMD — National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST)"}
    else:
        live = {'active': False, 'system': None,
                'depression': {'name': b['name'] or b['basin'], 'state': f"LIVE · {b['stage']}", 'lat': b['lat'], 'lon': b['lon'], 'note': t['note']},
                'snapshot': f"{t['dstr']} · Bulletin No. {b['no']} · {t['tstr']} IST",
                'source': f"RSMC New Delhi / IMD — National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST)"}
    new_s6 = replace_js_assignment(s6, r'var LIVE=', js_json(live))
    # --- live-cloud popups + marker labels (storm state only) ---
    if is_storm and not calm:
        if old_name and b['name'] and old_name != b['name']:
            new_s6 = new_s6.replace(f'{old_name} — LIVE', f"{b['name']} — LIVE")
        head_zone_old = re.search(r'"(FORECAST landfall zone — [^"]+)"', new_s6)
        if head_zone_old and t['landfall_short']:
            new_s6 = new_s6.replace(head_zone_old.group(1), f"FORECAST landfall zone — {t['landfall_short']}")
        zone_body = (f"<p><b>FORECAST — abhi hua NAHI hai:</b> IMD ke anuman se {b['name']} <b>{b.get('cross_stage', b['stage'])}</b> bankar "
                     f"<b>{b.get('cross_day','')} {b.get('cross_month','')} {b.get('cross_year','')}</b> ({b.get('cross_when','')}) ke aas-paas "
                     f"<b>{t['landfall_short']}</b> ke beech kinare ko paar kar sakta hai — hawa {b.get('cross_winds','')} km/h, jhonke {b.get('cross_gust','')} km/h tak. "
                     f"{t['surge_txt']}</p><p>Dashed FORECAST track samundar me {b['name']} ({b['lat']}°N, {b['lon']}°E) se is kinare ki ore jaata hai — ye anuman hai, asal track sirf RSMC/IMD bulletin se pakka hoga.</p>")
        r = replace_string_literal(new_s6, '<p><b>FORECAST — abhi hua NAHI hai:</b>', zone_body)
        if r:
            new_s6 = r
        storm_body = (f"<p><b>IMD National Bulletin No. {b['no']} · {t['dstr']}, {t['tstr']} IST</b></p>"
                      f"<p>{t['disp']} <b>{t['cstr']}</b> par {t['sea_hi']} ke upar kendrit hai — kendr <b>{b['lat']}°N, {b['lon']}°E</b>: {t['dist_hi']}. "
                      f"Hawa <b>{b.get('winds','')} km/h</b>, jhonke {b.get('gust','')} km/h tak; gati lagbhag <b>{b.get('move_speed','')} km/h</b>. "
                      f"{t['warn_short']}{' (' + t['warn_level'] + ' Message)' if t['warn_level'] else ''} jaari hai.</p>")
        r = replace_string_literal(new_s6, '<p><b>IMD National Bulletin No.', storm_body)
        if r:
            new_s6 = r
        # landfall marker label + coords
        mm = re.search(r'\{label:"FORECAST landfall zone · [^"]+",\s*lat:[\d.]+,\s*lng:[\d.]+', new_s6)
        if mm and t['marker_label'] and t['marker_lat'] is not None:
            new_s6 = new_s6[:mm.start()] + f'{{label:"{t["marker_label"]}",lat:{t["marker_lat"]},lng:{t["marker_lon"]}' + new_s6[mm.end():]
    html = replace_b64_script(html, sm, new_s6)
    # --- EMG (SOS) object in its own script ---
    em, s7 = find_b64_script(html, 'var EMG=')
    if em is not None:
        emg_active = bool(is_storm and not calm and b.get('cross_a')) or bool(not is_storm and not calm and b['stage'] == 'Deep Depression' and b.get('cross_a'))
        emg = {'active': emg_active, 'system': t['disp'], 'landfall': t['emg_landfall'], 'snapshot': t['emg_snapshot']}
        if calm:
            emg = {'active': False, 'system': '', 'landfall': '', 'snapshot': f"{t['dstr']}, {t['tstr']} IST — IMD National Bulletin No. {b['no']}: ab koi sakriya cyclone nahi hai."}
        new_s7 = replace_js_assignment(s7, r'var EMG=', js_json(emg))
        html = replace_b64_script(html, em, new_s7)
    # --- Hero H1 + sub (raw HTML) ---
    if calm:
        h1_new = 'India, watched for <span class="stormy">storms</span> — abhi koi sakriya cyclone nahi hai: samundar shaant hai.'
        sub_new = (f"Do sawalon ka ek imaandar jawab: <b>abhi India ke aas-paas koi sakriya cyclone nahi hai</b> "
                   f"(IMD National Bulletin No. {b['no']} · {t['dstr']}, {t['tstr']} IST): pichhla system kamzor hokar khatam ho chuka hai. "
                   f"Jaise hi IMD koi naya system ghoshit karega, ye page usi bulletin se update ho jayega.")
    elif is_storm:
        coast_short = t['warn_short'].replace('Cyclone Warning for ', '').replace('Cyclone Alert for ', '')
        warn_word = 'Cyclone Warning' if 'Warning' in b.get('warning', '') else 'Cyclone Alert'
        h1_new = (f"India, watched for <span class=\"stormy\">storms</span> — ek sakriya cyclone hai: {t['disp']} {t['sea_hi']} me hai — "
                  f"{coast_short} ke liye {warn_word} jaari hai.")
        sub_new = (f"Do sawalon ka ek imaandar jawab: <b>abhi India ke aas-paas ek sakriya cyclone hai — {t['disp']}</b> "
                   f"(IMD National Bulletin No. {b['no']} · {t['dstr']}, {t['tstr']} IST): ye <b>{t['dstr']} ki {t['cstr']}</b> par "
                   f"<b>{b['lat']}°N, {b['lon']}°E</b> par kendrit hai — <b>{t['dist_hi']}</b>. "
                   f"Ye lagbhag <b>{b.get('move_speed','')} km/h</b> se {dir_hinglish(b.get('move_dir',''))} ki ore badh raha hai, "
                   f"hawa <b>{b.get('winds','')} km/h</b>, jhonke {b.get('gust','')} km/h tak. "
                   + (f"<b>FORECAST (anuman — abhi hua nahi hai):</b> {b.get('cross_sentence','')}" if b.get('cross_sentence') else ''))
    else:
        h1_new = (f"India, watched for <span class=\"stormy\">storms</span> — abhi cyclone nahi, lekin ek {b['stage']} {t['sea_hi']} me sakriya hai.")
        sub_new = (f"Do sawalon ka ek imaandar jawab: <b>abhi cyclonic storm nahi hai, lekin ek {b['stage']} sakriya hai</b> "
                   f"(IMD National Bulletin No. {b['no']} · {t['dstr']}, {t['tstr']} IST): kendr <b>{b['lat']}°N, {b['lon']}°E</b> — <b>{t['dist_hi']}</b>. "
                   f"IMD ke agle bulletin par nazar rakhi ja rahi hai.")
    html = re.sub(r'(<h1>)(.*?)(</h1>)', lambda mo: mo.group(1) + h1_new + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(<p class="hero-sub">)(.*?)(</p>)', lambda mo: mo.group(1) + sub_new + mo.group(3), html, count=1, flags=re.S)
    # --- i18n live summaries (default / en / hi packs) ---
    im, s4 = find_b64_script(html, '@live_summary')
    if im is not None:
        if calm:
            sums = {
                'default': f"IMD National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST) ke hisaab se ab koi sakriya cyclone nahi hai. Samundar shaant hai — jaise hi naya system bane, yahan usi bulletin ki jaankari aa jayegi.",
                'en': f"As per IMD National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST), there is no active cyclone now. The seas are calm — when a new system forms, this panel will carry that bulletin.",
                'hi': f"IMD राष्ट्रीय बुलेटिन संख्या {b['no']} ({t['dstr']}, {t['tstr']} IST) के अनुसार अब कोई सक्रिय चक्रवात नहीं है। समुद्र शांत है — नया सिस्टम बनते ही यहाँ उसी बुलेटिन की जानकारी आ जाएगी।",
            }
            vcs = {
                'default': f"is snapshot (IMD bulletin · {t['dstr']}, {t['tstr']} IST) me cyclonic storm 0 — samundar shaant hai",
                'en': f"in this snapshot (IMD bulletin · {t['dstr']}, {t['tstr']} IST) cyclonic storms: 0 — seas are calm",
                'hi': f"इस स्नैपशॉट (IMD बुलेटिन · {t['dstr']}, {t['tstr']} IST) में चक्रवाती तूफ़ान 0 — समुद्र शांत है",
            }
        elif is_storm:
            sums = {
                'default': (f"IMD National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST) ke hisaab se ab ek sakriya cyclone hai — <b>{t['disp']}</b>. "
                            f"Ye {t['dstr']} {t['cstr']} par <b>{b['lat']}°N, {b['lon']}°E</b> par kendrit tha — {t['dist_hi']}. "
                            f"Hawa <b>{b.get('winds','')} km/h</b>, jhonke {b.get('gust','')} km/h tak. IMD ne <b>{t['warn_short']}</b>{' (' + t['warn_level'] + ' Message)' if t['warn_level'] else ''} jaari kiya hai. "
                            + (f"<b>FORECAST (anuman — abhi hua nahi hai):</b> {b.get('cross_sentence','')}" if b.get('cross_sentence') else '')),
                'en': (f"As per IMD National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST), an active cyclone exists — <b>{t['disp_plain']}</b>. "
                       f"It lay centred at {t['cstr']} on {t['dstr']} near <b>{b['lat']}°N, {b['lon']}°E</b> — {b['distances']}. "
                       f"Winds <b>{b.get('winds','')} km/h</b>, gusting to {b.get('gust','')} km/h. IMD has issued <b>{b.get('warning','')}</b>. "
                       + (f"<b>FORECAST (not yet happened):</b> It is likely to cross {b.get('cross_sentence','')}" if b.get('cross_sentence') else '')),
                'hi': (f"IMD राष्ट्रीय बुलेटिन संख्या {b['no']} ({t['dstr']}, {t['tstr']} IST) के अनुसार अब एक सक्रिय चक्रवात है — <b>{t['disp']}</b>। "
                       f"यह {t['dstr']} {t['cstr']} पर <b>{b['lat']}°N, {b['lon']}°E</b> पर केंद्रित था। "
                       f"हवा <b>{b.get('winds','')} km/h</b>, झोंके {b.get('gust','')} km/h तक। IMD ने <b>{t['warn_short']}</b> जारी किया है।"),
            }
            vcs = {
                'default': f"is snapshot (IMD bulletin · {t['dstr']}, {t['tstr']} IST) me cyclonic storm 1 — {t['disp']} sakriya hai" + (f", {t['landfall_short']} ki ore landfall ka anuman ({b.get('cross_day','')} {b.get('cross_month','')})" if t['landfall_short'] else ''),
                'en': f"in this snapshot (IMD bulletin · {t['dstr']}, {t['tstr']} IST) cyclonic storms: 1 — {t['disp_plain']} is active" + (f", forecast to make landfall near {t['landfall_short']} ({b.get('cross_day','')} {b.get('cross_month','')})" if t['landfall_short'] else ''),
                'hi': f"इस स्नैपशॉट (IMD बुलेटिन · {t['dstr']}, {t['tstr']} IST) में चक्रवाती तूफ़ान 1 — {t['disp']} सक्रिय है",
            }
        else:
            sums = {
                'default': f"IMD National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST) ke hisaab se abhi cyclonic storm nahi hai — ek <b>{b['stage']}</b> {t['sea_hi']} me sakriya hai, kendr <b>{b['lat']}°N, {b['lon']}°E</b> — {t['dist_hi']}.",
                'en': f"As per IMD National Bulletin No. {b['no']} ({t['dstr']}, {t['tstr']} IST), there is no cyclonic storm yet — a <b>{b['stage']}</b> is active over the {t['sea']}, centred near <b>{b['lat']}°N, {b['lon']}°E</b> — {b['distances']}.",
                'hi': f"IMD राष्ट्रीय बुलेटिन संख्या {b['no']} ({t['dstr']}, {t['tstr']} IST) के अनुसार अभी चक्रवाती तूफ़ान नहीं है — एक <b>{b['stage']}</b> सक्रिय है, केंद्र <b>{b['lat']}°N, {b['lon']}°E</b>।",
            }
            vcs = {
                'default': f"is snapshot (IMD bulletin · {t['dstr']}, {t['tstr']} IST) me cyclonic storm 0 — lekin ek {b['stage']} sakriya hai",
                'en': f"in this snapshot (IMD bulletin · {t['dstr']}, {t['tstr']} IST) cyclonic storms: 0 — but a {b['stage']} is active",
                'hi': f"इस स्नैपशॉट (IMD बुलेटिन · {t['dstr']}, {t['tstr']} IST) में चक्रवाती तूफ़ान 0 — लेकिन एक {b['stage']} सक्रिय है",
            }
        new_s4 = s4
        for key, pack in (('@live_summary', sums), ('@live_vc_sub', vcs)):
            for lang in ('default', 'en', 'hi'):
                new_s4 = replace_i18n_value(new_s4, lang, key, pack[lang])
        # --- deep live-text pack: globe card, legend, bot answers, walker ---
        W, G = b.get('winds', ''), b.get('gust', '')
        SP, DH = b.get('move_speed', ''), dir_hinglish(b.get('move_dir', ''))
        CR, LF = b.get('cross_sentence', ''), t['landfall_short']
        CD, CM, CWX = b.get('cross_day', ''), b.get('cross_month', ''), b.get('cross_when', '')
        LAT, LON = b['lat'], b['lon']
        NO, DS, TS = b['no'], t['dstr'], t['tstr']
        WS, WL = t['warn_short'], t['warn_level']
        if calm:
            extra = {
                '@globe_card_p2': {
                    'default': "Abhi samundar shaant hai — koi toofaan sakriya nahi. Globe par halke ghumav me woh 7 kinare dikh rahe hain jahan RSMC nazar rakhe hue hai. Jaise hi koi toofaan bane ya sakriya ho, woh yahi laal ghumav me sabse pehle dikhega.",
                    'en': "The seas are calm right now — no storm is active. The faint swirls on the globe are the 7 coasts under RSMC watch. As soon as a storm forms or becomes active, it will appear here first as a red swirl.",
                    'hi': "अभी समुद्र शांत है — कोई तूफ़ान सक्रिय नहीं। ग्लोब पर हलके घुमाव में वे 7 तट दिख रहे हैं जहाँ RSMC नज़र रखे हुए है। जैसे ही कोई तूफ़ान बने या सक्रिय हो, वह यहीं लाल घुमाव में सबसे पहले दिखेगा।"},
                '@globe_leg_active': {
                    'default': "Laal ghumav = sakriya toofaan (abhi koi nahi)",
                    'en': "Red swirl = active storm (none right now)",
                    'hi': "लाल घुमाव = सक्रिय तूफ़ान (अभी कोई नहीं)"},
                '@globe_cap': {
                    'default': "Ghoom raha hai — halka ghumav = watch kinara · sakriya toofaan: 0",
                    'en': "Spinning — faint swirl = watch coast · active storms: 0",
                    'hi': "घूम रहा है — हलका घुमाव = निगरानी तट · सक्रिय तूफ़ान: 0"},
                '@globe_dep_h': {
                    'default': "Abhi koi cyclone sakriya nahi hai",
                    'en': "No cyclone is active right now",
                    'hi': "अभी कोई चक्रवात सक्रिय नहीं है"},
                '@globe_dep_sub': {
                    'default': "Samundar abhi shaant hai. Globe par sirf LIVE haal dikhta hai — halke neele ghumav woh 7 watch kinare hain jahan nazar rakhi ja rahi hai.",
                    'en': "The seas are calm. The globe shows only the LIVE state — the faint blue swirls are the 7 watch coasts being monitored.",
                    'hi': "समुद्र अभी शांत है। ग्लोब पर सिर्फ़ LIVE हाल दिखता है — हलके नीले घुमाव वे 7 निगरानी तट हैं जहाँ नज़र रखी जा रही है।"},
                '@area_default': {
                    'default': "Poora India — abhi koi cyclonic storm nahi; samundar shaant hai",
                    'en': "All of India — no cyclonic storm right now; the seas are calm",
                    'hi': "पूरा भारत — अभी कोई चक्रवाती तूफ़ान नहीं; समुद्र शांत है"},
                '@scan': {
                    'default': "Scan #{n} · window: coastal watch zones + RSMC active-storm state · last scan {t} IST · sakriya cyclone: 0",
                    'en': "Scan #{n} · window: coastal watch zones + RSMC active-storm state · last scan {t} IST · active cyclones: 0",
                    'hi': "स्कैन #{n} · विंडो: तटीय निगरानी क्षेत्र + RSMC सक्रिय-तूफ़ान स्थिति · अंतिम स्कैन {t} IST · सक्रिय चक्रवात: 0"},
                '@bot_greet': {
                    'default': "Namaste! Main <b>Vayu (वायु)</b> hoon — hawa wala dost, sirf cyclones ke liye. Pehle ek imaandar baat: <b>abhi koi cyclonic storm sakriya nahi hai</b> (IMD National Bulletin No. " + str(NO) + ", " + DS + ") — samundar shaant hai. Toofaan par kuch bhi puchho.",
                    'en': "Namaste! I am <b>Vayu (वायु)</b> — your wind friend, only for cyclones. First, an honest word: <b>no cyclonic storm is active right now</b> (IMD National Bulletin No. " + str(NO) + ", " + DS + ") — the seas are calm. Ask me anything about storms.",
                    'hi': "नमस्ते! मैं <b>Vayu (वायु)</b> हूँ — हवा वाला दोस्त, सिर्फ़ चक्रवातों के लिए। पहले एक ईमानदार बात: <b>अभी कोई चक्रवाती तूफ़ान सक्रिय नहीं है</b> (IMD राष्ट्रीय बुलेटिन संख्या " + str(NO) + ", " + DS + ") — समुद्र शांत है। तूफ़ान पर कुछ भी पूछो।"},
                '@ans_live': {
                    'default': f"Seedha sach: <b>abhi koi cyclonic storm sakriya nahi hai</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST). Samundar shaant hai — jaise hi koi system bane, woh sabse pehle upar wale LIVE panel me dikhega. Archive ke 40 verified landfall (1971–2025) neeche diye hain.",
                    'en': f"Straight truth: <b>no cyclonic storm is active right now</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST). The seas are calm — as soon as a system forms, it will show first in the LIVE panel above. The archive of 40 verified landfalls (1971–2025) is below.",
                    'hi': f"सीधा सच: <b>अभी कोई चक्रवाती तूफ़ान सक्रिय नहीं है</b> (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}, {TS} IST)। समुद्र शांत है — कोई सिस्टम बनते ही वह सबसे पहले ऊपर वाले LIVE पैनल में दिखेगा।"},
                '@ans_watch': {
                    'default': f"<b>Watch note:</b> abhi koi system watch par nahi hai — IMD National Bulletin No. {NO} ({DS}) ke hisaab se samundar shaant hai. Naya low ya depression bante hi woh IMD bulletin me aayega aur ye page khud update ho jayega.",
                    'en': f"<b>Watch note:</b> no system is under watch right now — as per IMD National Bulletin No. {NO} ({DS}) the seas are calm. When a new low or depression forms, it will appear in IMD's bulletin and this page will update itself.",
                    'hi': f"<b>निगरानी नोट:</b> अभी कोई सिस्टम निगरानी पर नहीं है — IMD राष्ट्रीय बुलेटिन संख्या {NO} ({DS}) के अनुसार समुद्र शांत है। नया low या depression बनते ही यह page खुद update हो जाएगा।"},
                '@ans_area': {
                    'default': "<b>{name} ({state})</b> ke sabse paas ka watch zone hai <b>{zone}</b> — wahan {c} verified landfall hue hain. Abhi koi sakriya cyclone nahi hai — samundar shaant hai, phir bhi apne ilaake ke liye RSMC chetaavni par nazar rakho. Area checker me naam likhkar poora itihaas dekh sakte ho.",
                    'en': "<b>{name} ({state})</b> — its nearest watch zone is <b>{zone}</b>, where {c} verified landfalls have happened. No cyclone is active right now — the seas are calm, but still keep an eye on RSMC warnings for your area. Type the name in the area checker to see the full history.",
                    'hi': "<b>{name} ({state})</b> के सबसे पास का निगरानी क्षेत्र है <b>{zone}</b> — वहाँ {c} सत्यापित landfall हुए हैं। अभी कोई सक्रिय चक्रवात नहीं है — समुद्र शांत है, फिर भी अपने इलाके के लिए RSMC चेतावनी पर नज़र रखो।"},
                '@ans_general': {
                    'default': "Main is poori Cyclone site ko jaanti hoon: upar <b>LIVE WATCH</b> map (7 watch zone, abhi sakriya toofaan 0), beech me <b>date search</b>, aur neeche <b>40 verified records (1971–2025, India me kul 37,551 maut)</b>. Kuch khaas puchho — jaise “Amphan me kya hua tha?”, “Sabse bada cyclone?”, “Paradip check karo”, ya koi date.",
                    'en': "I know this whole Cyclone site: the <b>LIVE WATCH</b> map on top (7 watch zones, active storms right now: 0), <b>date search</b> in the middle, and <b>40 verified records (1971–2025; 37,551 total deaths in India)</b> below. Ask something specific — like “What happened in Amphan?”, “Biggest cyclone?”, “Check Paradip”, or any date.",
                    'hi': "मैं इस पूरी Cyclone site को जानती हूँ: ऊपर <b>LIVE WATCH</b> map (7 निगरानी क्षेत्र, अभी सक्रिय तूफ़ान 0), बीच में <b>date search</b>, और नीचे <b>40 सत्यापित records (1971–2025, भारत में कुल 37,551 मौत)</b>। कुछ ख़ास पूछो — जैसे “Amphan में क्या हुआ था?”, “सबसे बड़ा चक्रवात?”, “Paradip check करो”, या कोई date।"},
                '@ans_2026': {
                    'default': f"2026 ka seedha sach: is samay koi LIVE system nahi hai (IMD National Bulletin No. {NO}, {DS}) — samundar shaant hai. Is site ka archive 1971–2025 ke verified landfalls ka hai; jab koi naya system bane, woh archive me nahi, upar LIVE panel me dikhega.",
                    'en': f"The straight truth for 2026: no LIVE system exists at this time (IMD National Bulletin No. {NO}, {DS}) — the seas are calm. This site's archive covers verified landfalls of 1971–2025; when a new system forms, it will show in the LIVE panel above, not in the archive.",
                    'hi': f"2026 का सीधा सच: इस समय कोई LIVE सिस्टम नहीं है (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}) — समुद्र शांत है। इस site का archive 1971–2025 के सत्यापित landfalls का है; नया सिस्टम बनने पर वह ऊपर LIVE पैनल में दिखेगा।"},
                '@y2026_p': {
                    'default': f"2026 me is samay (IMD National Bulletin No. {NO}, {DS}) koi sakriya cyclonic storm nahi hai — samundar shaant hai. Is archive me sirf guzar chuke verified landfalls (1971–2025) hain; live haal hamesha upar wale panel me milega.",
                    'en': f"At this time in 2026 (IMD National Bulletin No. {NO}, {DS}) no cyclonic storm is active — the seas are calm. This archive holds only past verified landfalls (1971–2025); the live state is always in the panel above.",
                    'hi': f"2026 में इस समय (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}) कोई सक्रिय चक्रवाती तूफ़ान नहीं है — समुद्र शांत है। इस archive में सिर्फ़ गुज़र चुके सत्यापित landfalls (1971–2025) हैं; live हाल हमेशा ऊपर वाले पैनल में मिलेगा।"},
                '@walker_bubble': {
                    'default': f"Namaste! Main Vayu hoon — abhi koi cyclone sakriya nahi hai, samundar shaant hai (IMD bulletin · {DS}, {TS} IST). Kuch puchhna hai toh mujhe dabao!",
                    'en': f"Namaste! I am Vayu — no cyclone is active right now, the seas are calm (IMD bulletin · {DS}, {TS} IST). Tap me if you want to ask anything!",
                    'hi': f"नमस्ते! मैं Vayu हूँ — अभी कोई चक्रवात सक्रिय नहीं है, समुद्र शांत है (IMD बुलेटिन · {DS}, {TS} IST)। कुछ पूछना है तो मुझे दबाओ!"},
            }
        elif is_storm:
            extra = {
                '@globe_card_p2': {
                    'default': f"Ab globe par ek laal ghumav hai — {t['disp']} LIVE ({LAT}°N, {LON}°E; IMD National Bulletin No. {NO}, {DS}, {TS} IST). Hawa {W} km/h (jhonke {G}), gati lagbhag {SP} km/h {DH}. {WS} jaari hai. Halke ghumav woh 7 kinare hain jahan nazar rakhi ja rahi hai.",
                    'en': f"There is now a red swirl on the globe — {t['disp_plain']} LIVE ({LAT}°N, {LON}°E; IMD National Bulletin No. {NO}, {DS}, {TS} IST). Winds {W} km/h (gusting {G}). {b.get('warning','')} is in force. The faint swirls are the 7 coasts under watch.",
                    'hi': f"अब ग्लोब पर एक लाल घुमाव है — {t['disp']} LIVE ({LAT}°N, {LON}°E; IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}, {TS} IST)। हवा {W} km/h (झोंके {G})। {WS} जारी है। हलके घुमाव वे 7 तट हैं जहाँ नज़र रखी जा रही है।"},
                '@globe_leg_active': {
                    'default': f"Laal ghumav = sakriya toofaan — ‘{b['name']}’ LIVE",
                    'en': f"Red swirl = active storm — ‘{b['name']}’ LIVE",
                    'hi': f"लाल घुमाव = सक्रिय तूफ़ान — ‘{b['name']}’ LIVE"},
                '@globe_cap': {
                    'default': f"Ghoom raha hai — halka ghumav = watch kinara · sakriya toofaan: 1 — {b['name']}",
                    'en': f"Spinning — faint swirl = watch coast · active storms: 1 — {b['name']}",
                    'hi': f"घूम रहा है — हलका घुमाव = निगरानी तट · सक्रिय तूफ़ान: 1 — {b['name']}"},
                '@globe_dep_h': {
                    'default': f"{t['disp']} — LIVE",
                    'en': f"{t['disp_plain']} — LIVE",
                    'hi': f"{t['disp']} — LIVE"},
                '@globe_dep_sub': {
                    'default': f"{b['name']} {t['sea_hi']} me sakriya hai ({LAT}°N, {LON}°E) — globe par laal ghumav dekho.",
                    'en': f"{b['name']} is active over the {t['sea']} ({LAT}°N, {LON}°E) — see the red swirl on the globe.",
                    'hi': f"{b['name']} {t['sea_hi']} में सक्रिय है ({LAT}°N, {LON}°E) — ग्लोब पर लाल घुमाव देखो।"},
                '@area_default': {
                    'default': f"Poora India — 1 sakriya cyclone: {t['disp']} ({LAT}°N, {LON}°E); FORECAST landfall: {LF}, {CD} {CM} ({CWX})",
                    'en': f"All of India — 1 active cyclone: {t['disp_plain']} ({LAT}°N, {LON}°E); FORECAST landfall: {LF}, {CD} {CM} ({CWX})",
                    'hi': f"पूरा भारत — 1 सक्रिय चक्रवात: {t['disp']} ({LAT}°N, {LON}°E); FORECAST landfall: {LF}, {CD} {CM}"},
                '@scan': {
                    'default': "Scan #{n} · window: coastal watch zones + RSMC active-storm state · last scan {t} IST · sakriya cyclone: 1 — " + str(b['name']),
                    'en': "Scan #{n} · window: coastal watch zones + RSMC active-storm state · last scan {t} IST · active cyclones: 1 — " + str(b['name']),
                    'hi': "स्कैन #{n} · विंडो: तटीय निगरानी क्षेत्र + RSMC सक्रिय-तूफ़ान स्थिति · अंतिम स्कैन {t} IST · सक्रिय चक्रवात: 1 — " + str(b['name'])},
                '@bot_greet': {
                    'default': f"Namaste! Main <b>Vayu (वायु)</b> hoon — hawa wala dost, sirf cyclones ke liye. Pehle ek imaandar baat: <b>abhi ek sakriya cyclone hai — {t['disp']}</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST) — {WS} jaari hai. Toofaan par kuch bhi puchho.",
                    'en': f"Namaste! I am <b>Vayu (वायु)</b> — your wind friend, only for cyclones. First, an honest word: <b>an active cyclone exists — {t['disp_plain']}</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST) — {b.get('warning','')} is in force. Ask me anything about storms.",
                    'hi': f"नमस्ते! मैं <b>Vayu (वायु)</b> हूँ — हवा वाला दोस्त, सिर्फ़ चक्रवातों के लिए। पहले एक ईमानदार बात: <b>अभी एक सक्रिय चक्रवात है — {t['disp']}</b> (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}, {TS} IST) — {WS} जारी है। तूफ़ान पर कुछ भी पूछो।"},
                '@ans_live': {
                    'default': f"Seedha sach: <b>abhi ek {b['stage']} sakriya hai — ‘{b['name']}’</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST; kendr {LAT}°N, {LON}°E — {t['dist_hi']}). Hawa <b>{W} km/h</b>, jhonke {G} km/h tak; gati ~{SP} km/h {DH} ki ore. IMD ne <b>{WS}</b>{' (' + WL + ' Message)' if WL else ''} jaari kiya hai. <b>FORECAST (anuman — abhi hua nahi hai):</b> {CR} Sabse pakki taaza jaankari upar wale LIVE panel aur IMD ke naye bulletin me milegi.",
                    'en': f"Straight truth: <b>a {b['stage']} is active — ‘{b['name']}’</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST; centred {LAT}°N, {LON}°E — {b['distances']}). Winds <b>{W} km/h</b>, gusting to {G} km/h. IMD has issued <b>{b.get('warning','')}</b>. <b>FORECAST (not yet happened):</b> It is likely to cross {CR} The surest fresh information is in the LIVE panel above and IMD's next bulletin.",
                    'hi': f"सीधा सच: <b>अभी एक {b['stage']} सक्रिय है — ‘{b['name']}’</b> (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}, {TS} IST; केंद्र {LAT}°N, {LON}°E)। हवा <b>{W} km/h</b>, झोंके {G} km/h तक। IMD ने <b>{WS}</b> जारी किया है। <b>FORECAST (अनुमान — अभी हुआ नहीं है):</b> सबसे पक्की ताज़ा जानकारी ऊपर वाले LIVE पैनल और IMD के नए बुलेटिन में मिलेगी।"},
                '@ans_watch': {
                    'default': f"<b>Watch note:</b> abhi nazar isi sakriya system par hai — <b>{t['disp']}</b> (IMD National Bulletin No. {NO}, {DS}) {t['sea_hi']} me hai aur <b>{WS}</b> jaari hai. Iske alawa koi doosra system abhi watch par nahi hai. Naya low/depression bante hi IMD bulletin me aayega aur ye page update ho jayega.",
                    'en': f"<b>Watch note:</b> all eyes are on this active system — <b>{t['disp_plain']}</b> (IMD National Bulletin No. {NO}, {DS}) over the {t['sea']}, with <b>{b.get('warning','')}</b> in force. No other system is under watch right now. A new low/depression will appear in IMD's bulletin and this page will update.",
                    'hi': f"<b>निगरानी नोट:</b> अभी नज़र इसी सक्रिय सिस्टम पर है — <b>{t['disp']}</b> (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}) और <b>{WS}</b> जारी है। इसके अलावा कोई दूसरा सिस्टम निगरानी पर नहीं है।"},
                '@ans_area': {
                    'default': "<b>{name} ({state})</b> ke sabse paas ka watch zone hai <b>{zone}</b> — wahan {c} verified landfall hue hain. Abhi sakriya cyclone <b>‘" + str(b['name']) + "’</b> " + t['sea_hi'] + f" me hai ({LAT}°N, {LON}°E; IMD Bulletin No. {NO}, {DS}) aur FORECAST landfall {LF} ka hai — apne ilaake ke liye RSMC chetaavni par nazar rakho. Area checker me naam likhkar poora itihaas dekh sakte ho.",
                    'en': "<b>{name} ({state})</b> — its nearest watch zone is <b>{zone}</b>, where {c} verified landfalls have happened. The active cyclone <b>‘" + str(b['name']) + "’</b> is over the " + t['sea'] + f" ({LAT}°N, {LON}°E; IMD Bulletin No. {NO}, {DS}) with FORECAST landfall near {LF} — keep an eye on RSMC warnings for your area. Type the name in the area checker to see the full history.",
                    'hi': "<b>{name} ({state})</b> के सबसे पास का निगरानी क्षेत्र है <b>{zone}</b> — वहाँ {c} सत्यापित landfall हुए हैं। अभी सक्रिय चक्रवात <b>‘" + str(b['name']) + "’</b> है (IMD बुलेटिन संख्या " + str(NO) + f", {DS}) और FORECAST landfall {LF} का है — अपने इलाके के लिए RSMC चेतावनी पर नज़र रखो।"},
                '@ans_general': {
                    'default': f"Main is poori Cyclone site ko jaanti hoon: upar <b>LIVE WATCH</b> map (7 watch zone, abhi sakriya toofaan 1 — {t['disp']}), beech me <b>date search</b>, aur neeche <b>40 verified records (1971–2025, India me kul 37,551 maut)</b>. Kuch khaas puchho — jaise “Amphan me kya hua tha?”, “Sabse bada cyclone?”, “Paradip check karo”, ya koi date.",
                    'en': f"I know this whole Cyclone site: the <b>LIVE WATCH</b> map on top (7 watch zones, active storms right now: 1 — {t['disp_plain']}), <b>date search</b> in the middle, and <b>40 verified records (1971–2025; 37,551 total deaths in India)</b> below. Ask something specific — like “What happened in Amphan?”, “Biggest cyclone?”, “Check Paradip”, or any date.",
                    'hi': f"मैं इस पूरी Cyclone site को जानती हूँ: ऊपर <b>LIVE WATCH</b> map (7 निगरानी क्षेत्र, अभी सक्रिय तूफ़ान 1 — {t['disp']}), बीच में <b>date search</b>, और नीचे <b>40 सत्यापित records (1971–2025, भारत में कुल 37,551 मौत)</b>। कुछ ख़ास पूछो — जैसे “Amphan में क्या हुआ था?”, “सबसे बड़ा चक्रवात?”, “Paradip check करो”, या कोई date।"},
                '@ans_2026': {
                    'default': f"2026 ka seedha sach: is samay ka LIVE system <b>{t['disp']}</b> hai (IMD National Bulletin No. {NO}, {DS}, {TS} IST) — kendr {LAT}°N, {LON}°E, hawa {W} km/h (jhonke {G}). <b>FORECAST:</b> {CR} Is site ka archive 1971–2025 ke verified landfalls ka hai, isliye 2026 ka chalta system archive me nahi, upar LIVE panel me dikhta hai.",
                    'en': f"The straight truth for 2026: the LIVE system right now is <b>{t['disp_plain']}</b> (IMD National Bulletin No. {NO}, {DS}, {TS} IST) — centred {LAT}°N, {LON}°E, winds {W} km/h (gusting {G}). <b>FORECAST:</b> It is likely to cross {CR} This site's archive covers verified landfalls of 1971–2025, so the running 2026 system shows in the LIVE panel above, not in the archive.",
                    'hi': f"2026 का सीधा सच: इस समय का LIVE सिस्टम <b>{t['disp']}</b> है (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}, {TS} IST) — केंद्र {LAT}°N, {LON}°E, हवा {W} km/h (झोंके {G})। यह चलता सिस्टम archive में नहीं, ऊपर LIVE पैनल में दिखता है।"},
                '@y2026_p': {
                    'default': f"2026 ka season jeevant hai: is samay ka LIVE system {t['disp']} hai (IMD National Bulletin No. {NO}, {DS}) — {LAT}°N, {LON}°E par, hawa {W} km/h, jhonke {G} km/h tak. FORECAST: {CR} Ye chalta system archive me nahi aayega — archive sirf guzar chuke verified landfalls (1971–2025) ka hai; live haal hamesha upar wale panel me milega.",
                    'en': f"The 2026 season is alive: the LIVE system right now is {t['disp_plain']} (IMD National Bulletin No. {NO}, {DS}) — at {LAT}°N, {LON}°E, winds {W} km/h, gusting to {G} km/h. FORECAST: It is likely to cross {CR} This running system will not enter the archive — the archive holds only past verified landfalls (1971–2025); the live state is always in the panel above.",
                    'hi': f"2026 का season जीवंत है: इस समय का LIVE सिस्टम {t['disp']} है (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}) — {LAT}°N, {LON}°E पर, हवा {W} km/h, झोंके {G} km/h तक। यह चलता सिस्टम archive में नहीं आएगा — archive सिर्फ़ गुज़र चुके सत्यापित landfalls (1971–2025) का है।"},
                '@walker_bubble': {
                    'default': f"Namaste! Main Vayu hoon — {t['disp']} abhi LIVE hai ({LAT}°N, {LON}°E); {WS} jaari hai (IMD bulletin · {DS}, {TS} IST) — {CD} {CM} ko {LF} ke beech landfall ka anuman. Kuch puchhna hai toh mujhe dabao!",
                    'en': f"Namaste! I am Vayu — {t['disp_plain']} is LIVE right now ({LAT}°N, {LON}°E); {b.get('warning','')} is in force (IMD bulletin · {DS}, {TS} IST) — landfall is forecast near {LF} on {CD} {CM}. Tap me if you want to ask anything!",
                    'hi': f"नमस्ते! मैं Vayu हूँ — {t['disp']} अभी LIVE है ({LAT}°N, {LON}°E); {WS} जारी है (IMD बुलेटिन · {DS}, {TS} IST) — {CD} {CM} को {LF} के बीच landfall का अनुमान। कुछ पूछना है तो मुझे दबाओ!"},
            }
        else:
            extra = {
                '@globe_card_p2': {
                    'default': f"Abhi cyclonic storm nahi hai — lekin ek {b['stage']} {t['sea_hi']} me sakriya hai ({LAT}°N, {LON}°E; IMD National Bulletin No. {NO}, {DS}, {TS} IST). Jaise hi ye toofaan bane, woh yahi laal ghumav me dikhega. Halke ghumav woh 7 kinare hain jahan nazar rakhi ja rahi hai.",
                    'en': f"There is no cyclonic storm yet — but a {b['stage']} is active over the {t['sea']} ({LAT}°N, {LON}°E; IMD National Bulletin No. {NO}, {DS}, {TS} IST). If it becomes a storm, it will appear here as a red swirl. The faint swirls are the 7 coasts under watch.",
                    'hi': f"अभी चक्रवाती तूफ़ान नहीं है — लेकिन एक {b['stage']} सक्रिय है ({LAT}°N, {LON}°E; IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}, {TS} IST)। तूफ़ान बनते ही वह यहीं लाल घुमाव में दिखेगा।"},
                '@globe_leg_active': {
                    'default': "Laal ghumav = sakriya toofaan (abhi koi nahi — ek depression hai)",
                    'en': "Red swirl = active storm (none right now — a depression is active)",
                    'hi': "लाल घुमाव = सक्रिय तूफ़ान (अभी कोई नहीं — एक depression है)"},
                '@globe_cap': {
                    'default': f"Ghoom raha hai — halka ghumav = watch kinara · sakriya toofaan: 0 — 1 {b['stage']} ({b['basin']})",
                    'en': f"Spinning — faint swirl = watch coast · active storms: 0 — 1 {b['stage']} ({b['basin']})",
                    'hi': f"घूम रहा है — हलका घुमाव = निगरानी तट · सक्रिय तूफ़ान: 0 — 1 {b['stage']} ({b['basin']})"},
                '@area_default': {
                    'default': f"Poora India — abhi koi cyclonic storm nahi; {b['stage']} {b['basin']} {t['sea_hi']} me hai ({LAT}°N, {LON}°E)",
                    'en': f"All of India — no cyclonic storm right now; a {b['stage']} ({b['basin']}) is over the {t['sea']} ({LAT}°N, {LON}°E)",
                    'hi': f"पूरा भारत — अभी कोई चक्रवाती तूफ़ान नहीं; {b['stage']} {b['basin']} सक्रिय है ({LAT}°N, {LON}°E)"},
                '@scan': {
                    'default': "Scan #{n} · window: coastal watch zones + RSMC active-storm state · last scan {t} IST · sakriya cyclone: 0 — 1 depression",
                    'en': "Scan #{n} · window: coastal watch zones + RSMC active-storm state · last scan {t} IST · active cyclones: 0 — 1 depression",
                    'hi': "स्कैन #{n} · विंडो: तटीय निगरानी क्षेत्र + RSMC सक्रिय-तूफ़ान स्थिति · अंतिम स्कैन {t} IST · सक्रिय चक्रवात: 0 — 1 depression"},
                '@bot_greet': {
                    'default': f"Namaste! Main <b>Vayu (वायु)</b> hoon — hawa wala dost, sirf cyclones ke liye. Pehle ek imaandar baat: <b>abhi koi cyclonic storm sakriya nahi hai</b>, lekin {t['sea_hi']} me <b>{b['stage']} {b['basin']}</b> sakriya hai (IMD bulletin, {DS}) — is par nazar rakhi ja rahi hai. Toofaan par kuch bhi puchho.",
                    'en': f"Namaste! I am <b>Vayu (वायु)</b> — your wind friend, only for cyclones. First, an honest word: <b>no cyclonic storm is active right now</b>, but a <b>{b['stage']} ({b['basin']})</b> is active over the {t['sea']} (IMD bulletin, {DS}) and is being watched. Ask me anything about storms.",
                    'hi': f"नमस्ते! मैं <b>Vayu (वायु)</b> हूँ — हवा वाला दोस्त, सिर्फ़ चक्रवातों के लिए। पहले एक ईमानदार बात: <b>अभी कोई चक्रवाती तूफ़ान सक्रिय नहीं है</b>, लेकिन <b>{b['stage']} {b['basin']}</b> सक्रिय है (IMD बुलेटिन, {DS}) — इस पर नज़र रखी जा रही है। तूफ़ान पर कुछ भी पूछो।"},
                '@ans_live': {
                    'default': f"Seedha sach: <b>abhi koi cyclonic storm sakriya nahi hai</b> — lekin {t['sea_hi']} me <b>{b['stage']} {b['basin']}</b> hai (IMD bulletin, {DS}, {TS} IST; kendr {LAT}°N, {LON}°E — {t['dist_hi']}). Ye toofaan bane ya na bane, pakki jaankari sirf IMD ke agle bulletin se milegi — upar wale LIVE panel par nazar rakho.",
                    'en': f"Straight truth: <b>no cyclonic storm is active right now</b> — but a <b>{b['stage']} ({b['basin']})</b> is over the {t['sea']} (IMD bulletin, {DS}, {TS} IST; centred {LAT}°N, {LON}°E — {b['distances']}). Whether it becomes a storm or not, only IMD's next bulletin will confirm — keep an eye on the LIVE panel above.",
                    'hi': f"सीधा सच: <b>अभी कोई चक्रवाती तूफ़ान सक्रिय नहीं है</b> — लेकिन <b>{b['stage']} {b['basin']}</b> है (IMD बुलेटिन, {DS}, {TS} IST; केंद्र {LAT}°N, {LON}°E)। पक्की जानकारी सिर्फ़ IMD के अगले बुलेटिन से मिलेगी — ऊपर वाले LIVE पैनल पर नज़र रखो।"},
                '@ans_watch': {
                    'default': f"<b>Watch note — ye cyclone NAHI hai:</b> {t['sea_hi']} ke upar ek <b>{b['stage']} ({b['basin']})</b> hai (IMD bulletin, {DS}, {TS} IST) — abhi ye sirf {b['stage']} hai, na cyclonic storm. Isiliye is par sirf nazar rakhi ja rahi hai — <b>abhi isse koi cyclone-chetaavni nahi banti</b>. Aur haan, abhi India ke aas-paas <b>koi sakriya cyclone nahi hai</b>.",
                    'en': f"<b>Watch note — this is NOT a cyclone:</b> a <b>{b['stage']} ({b['basin']})</b> is over the {t['sea']} (IMD bulletin, {DS}, {TS} IST) — right now it is only a {b['stage']}, not a cyclonic storm. That is why it is only being watched — <b>no cyclone warning arises from it yet</b>. And yes, <b>no cyclone is active</b> near India right now.",
                    'hi': f"<b>निगरानी नोट — यह चक्रवात नहीं है:</b> एक <b>{b['stage']} ({b['basin']})</b> है (IMD बुलेटिन, {DS}, {TS} IST) — अभी यह सिर्फ़ {b['stage']} है, चक्रवाती तूफ़ान नहीं। इसलिए इस पर सिर्फ़ नज़र रखी जा रही है — <b>अभी इससे कोई चक्रवात-चेतावनी नहीं बनती</b>। और हाँ, अभी भारत के आस-पास <b>कोई सक्रिय चक्रवात नहीं है</b>।"},
                '@ans_area': {
                    'default': "<b>{name} ({state})</b> ke sabse paas ka watch zone hai <b>{zone}</b> — wahan {c} verified landfall hue hain. Abhi koi sakriya cyclone nahi hai — ek " + b['stage'] + " " + t['sea_hi'] + f" me hai ({LAT}°N, {LON}°E; IMD Bulletin No. {NO}, {DS}) — apne ilaake ke liye RSMC chetaavni par nazar rakho. Area checker me naam likhkar poora itihaas dekh sakte ho.",
                    'en': "<b>{name} ({state})</b> — its nearest watch zone is <b>{zone}</b>, where {c} verified landfall have happened. No cyclone is active right now — a " + b['stage'] + " is over the " + t['sea'] + f" ({LAT}°N, {LON}°E; IMD Bulletin No. {NO}, {DS}) — keep an eye on RSMC warnings for your area. Type the name in the area checker to see the full history.",
                    'hi': "<b>{name} ({state})</b> के सबसे पास का निगरानी क्षेत्र है <b>{zone}</b> — वहाँ {c} सत्यापित landfall हुए हैं। अभी कोई सक्रिय चक्रवात नहीं है — एक " + b['stage'] + f" सक्रिय है ({LAT}°N, {LON}°E; IMD बुलेटिन संख्या {NO}, {DS}) — अपने इलाके के लिए RSMC चेतावनी पर नज़र रखो।"},
                '@ans_general': {
                    'default': "Main is poori Cyclone site ko jaanti hoon: upar <b>LIVE WATCH</b> map (7 watch zone, abhi sakriya toofaan 0 — ek depression hai), beech me <b>date search</b>, aur neeche <b>40 verified records (1971–2025, India me kul 37,551 maut)</b>. Kuch khaas puchho — jaise “Amphan me kya hua tha?”, “Sabse bada cyclone?”, “Paradip check karo”, ya koi date.",
                    'en': "I know this whole Cyclone site: the <b>LIVE WATCH</b> map on top (7 watch zones, active storms right now: 0 — a depression is active), <b>date search</b> in the middle, and <b>40 verified records (1971–2025; 37,551 total deaths in India)</b> below. Ask something specific — like “What happened in Amphan?”, “Biggest cyclone?”, “Check Paradip”, or any date.",
                    'hi': "मैं इस पूरी Cyclone site को जानती हूँ: ऊपर <b>LIVE WATCH</b> map (7 निगरानी क्षेत्र, अभी सक्रिय तूफ़ान 0 — एक depression है), बीच में <b>date search</b>, और नीचे <b>40 सत्यापित records (1971–2025, भारत में कुल 37,551 मौत)</b>। कुछ ख़ास पूछो — जैसे “Amphan में क्या हुआ था?”, “सबसे बड़ा चक्रवात?”, “Paradip check करो”, या कोई date।"},
                '@ans_2026': {
                    'default': f"2026 ka seedha sach: is samay koi cyclonic storm nahi hai — {t['sea_hi']} me ek <b>{b['stage']} ({b['basin']})</b> sakriya hai (IMD National Bulletin No. {NO}, {DS}) — kendr {LAT}°N, {LON}°E. Is site ka archive 1971–2025 ke verified landfalls ka hai; chalta system archive me nahi, upar LIVE panel me dikhta hai.",
                    'en': f"The straight truth for 2026: no cyclonic storm exists at this time — a <b>{b['stage']} ({b['basin']})</b> is active over the {t['sea']} (IMD National Bulletin No. {NO}, {DS}) — centred {LAT}°N, {LON}°E. This site's archive covers verified landfalls of 1971–2025; the running system shows in the LIVE panel above, not in the archive.",
                    'hi': f"2026 का सीधा सच: इस समय कोई चक्रवाती तूफ़ान नहीं है — एक <b>{b['stage']} ({b['basin']})</b> सक्रिय है (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}) — केंद्र {LAT}°N, {LON}°E। चलता सिस्टम archive में नहीं, ऊपर LIVE पैनल में दिखता है।"},
                '@y2026_p': {
                    'default': f"2026 me is samay (IMD National Bulletin No. {NO}, {DS}) koi cyclonic storm nahi hai — ek {b['stage']} ({b['basin']}) {t['sea_hi']} me sakriya hai, kendr {LAT}°N, {LON}°E. Is archive me sirf guzar chuke verified landfalls (1971–2025) hain; live haal hamesha upar wale panel me milega.",
                    'en': f"At this time in 2026 (IMD National Bulletin No. {NO}, {DS}) no cyclonic storm is active — a {b['stage']} ({b['basin']}) is over the {t['sea']}, centred {LAT}°N, {LON}°E. This archive holds only past verified landfalls (1971–2025); the live state is always in the panel above.",
                    'hi': f"2026 में इस समय (IMD राष्ट्रीय बुलेटिन संख्या {NO}, {DS}) कोई चक्रवाती तूफ़ान नहीं है — एक {b['stage']} ({b['basin']}) सक्रिय है, केंद्र {LAT}°N, {LON}°E। इस archive में सिर्फ़ गुज़र चुके सत्यापित landfalls (1971–2025) हैं।"},
                '@walker_bubble': {
                    'default': f"Namaste! Main Vayu hoon — abhi cyclone nahi hai, lekin {b['stage']} {b['basin']} {t['sea_hi']} me sakriya hai (IMD bulletin · {DS}, {TS} IST) — is par nazar rakhi ja rahi hai. Kuch puchhna hai toh mujhe dabao!",
                    'en': f"Namaste! I am Vayu — no cyclone right now, but a {b['stage']} ({b['basin']}) is active over the {t['sea']} (IMD bulletin · {DS}, {TS} IST) and is being watched. Tap me if you want to ask anything!",
                    'hi': f"नमस्ते! मैं Vayu हूँ — अभी चक्रवात नहीं है, लेकिन {b['stage']} {b['basin']} सक्रिय है (IMD बुलेटिन · {DS}, {TS} IST) — इस पर नज़र रखी जा रही है। कुछ पूछना है तो मुझे दबाओ!"},
            }
        for key, pack in extra.items():
            for lang in ('default', 'en', 'hi'):
                new_s4 = replace_i18n_value(new_s4, lang, key, pack[lang])
        # @tips is an ARRAY; its first tip carries the live claim — patch it
        if calm:
            tips = {'default': f"Namaste! Main Vayu hoon — abhi koi cyclone LIVE nahi hai, samundar shaant hai (IMD Bulletin No. {NO}, {DS}). Kuch puchhna hai toh mujhe dabao!",
                    'en': f"Hello! I am Vayu — no cyclone is LIVE right now, the seas are calm (IMD Bulletin No. {NO}, {DS}). Tap me if you want to ask anything!",
                    'hi': f"नमस्ते! मैं Vayu हूँ — अभी कोई चक्रवात LIVE नहीं है, समुद्र शांत है (IMD बुलेटिन संख्या {NO}, {DS})। कुछ पूछना है तो मुझे दबाओ!"}
        elif is_storm:
            tips = {'default': f"Namaste! Main Vayu hoon — {t['disp']} LIVE hai (IMD Bulletin No. {NO}, {DS}); {WS} jaari hai. Kuch puchhna hai toh mujhe dabao!",
                    'en': f"Hello! I am Vayu — {t['disp_plain']} is LIVE (IMD Bulletin No. {NO}, {DS}); {b.get('warning','')} is in force. Tap me if you want to ask anything!",
                    'hi': f"नमस्ते! मैं Vayu हूँ — {t['disp']} LIVE है (IMD बुलेटिन संख्या {NO}, {DS}); {WS} जारी है। कुछ पूछना है तो मुझे दबाओ!"}
        else:
            tips = {'default': f"Namaste! Main Vayu hoon — abhi cyclone nahi hai, lekin ek {b['stage']} ({b['basin']}) LIVE nazar me hai (IMD Bulletin No. {NO}, {DS}). Kuch puchhna hai toh mujhe dabao!",
                    'en': f"Hello! I am Vayu — no cyclone yet, but a {b['stage']} ({b['basin']}) is under LIVE watch (IMD Bulletin No. {NO}, {DS}). Tap me if you want to ask anything!",
                    'hi': f"नमस्ते! मैं Vayu हूँ — अभी चक्रवात नहीं है, लेकिन एक {b['stage']} ({b['basin']}) LIVE नज़र में है (IMD बुलेटिन संख्या {NO}, {DS})। कुछ पूछना है तो मुझे दबाओ!"}
        for lang in ('default', 'en', 'hi'):
            new_s4 = replace_tips_first(new_s4, lang, tips[lang])
        # depression banner strings when a depression is active
        if not is_storm and not calm:
            dep_h = {'default': f"Cyclone abhi nahi — lekin {b['stage']} {b['basin']} aa raha hai",
                     'en': f"No cyclone yet — but a {b['stage']} ({b['basin']}) is active",
                     'hi': f"चक्रवात अभी नहीं — लेकिन {b['stage']} {b['basin']} सक्रिय है"}
            dep_s = {'default': f"IMD bulletin ({DS}, {TS} IST): {b['stage']} {LAT}°N, {LON}°E par hai — {t['dist_hi']}",
                     'en': f"IMD bulletin ({DS}, {TS} IST): the {b['stage']} is at {LAT}°N, {LON}°E — {b['distances']}",
                     'hi': f"IMD बुलेटिन ({DS}, {TS} IST): {b['stage']} {LAT}°N, {LON}°E पर है"}
            for lang in ('default', 'en', 'hi'):
                new_s4 = replace_i18n_value(new_s4, lang, '@globe_dep_h', dep_h[lang])
                new_s4 = replace_i18n_value(new_s4, lang, '@globe_dep_sub', dep_s[lang])
        if new_s4 != s4:
            html = replace_b64_script(html, im, new_s4)
    # --- SOS panel texts (script with var EMG=): place / nowP / src / announce ---
    em2, s7b = find_b64_script(html, 'var EMG=')
    if em2 is not None:
        if is_storm and not calm:
            sos = {
                'place': {
                    'default': f"{LF} kinara ({WS})",
                    'en': f"{LF} coast ({WS})",
                    'hi': f"{LF} तट ({WS})"},
                'nowP': {
                    'default': f"<b>Khatra signal ON hai — sakriya cyclone ‘{b['name']}’ ki wajah se.</b> IMD National Bulletin No. {NO} ({DS}, {TS} IST) ke hisaab se <b>{t['disp']}</b> <b>{LAT}°N, {LON}°E</b> par hai ({t['dist_hi']}); hawa <b>{W} km/h</b>, jhonke {G} km/h tak, gati ~{SP} km/h {DH}. IMD ne <b>{b.get('warning','')}</b> jaari kiya hai. <b>FORECAST (anuman — abhi hua NAHI hai):</b> {CR} Landfall abhi hua NAHI hai — lekin taiyaari abhi se karo: kinare aur nichle ilaakon se door raho, sarkari chetaavni par chalo, aur asli khatre me turant 112 par call karo.",
                    'en': f"<b>The danger signal is ON — because of the active cyclone ‘{b['name']}’.</b> Per IMD National Bulletin No. {NO} ({DS}, {TS} IST), <b>{t['disp_plain']}</b> is at <b>{LAT}°N, {LON}°E</b> ({b['distances']}); winds <b>{W} km/h</b>, gusting to {G} km/h. IMD has issued <b>{b.get('warning','')}</b>. <b>FORECAST (not yet happened):</b> It is likely to cross {CR} Landfall has NOT happened yet — but prepare now: stay away from the coast and low-lying areas, follow official warnings, and in real danger call 112 immediately.",
                    'hi': f"<b>ख़तरा संकेत चालू है — सक्रिय चक्रवात ‘{b['name']}’ की वजह से।</b> IMD राष्ट्रीय बुलेटिन संख्या {NO} ({DS}, {TS} IST) के अनुसार <b>{t['disp']}</b> <b>{LAT}°N, {LON}°E</b> पर है; हवा <b>{W} km/h</b>, झोंके {G} km/h तक। IMD ने <b>{WS}</b> जारी किया है। <b>FORECAST (अनुमान — अभी हुआ नहीं है):</b> Landfall अभी हुआ नहीं है — लेकिन तैयारी अभी से करो: तट और निचले इलाकों से दूर रहो, सरकारी चेतावनी पर चलो, और असली ख़तरे में तुरंत 112 पर call करो।"},
                'src': {
                    'default': f"Source: RSMC New Delhi / IMD · National Bulletin No. {NO} ({DS}, {TS} IST) — {b['name']}: {LAT}°N, {LON}°E, hawa {W} km/h (jhonke {G}); {WS}; landfall ({LF}) FORECAST hai, abhi hua nahi hai",
                    'en': f"Source: RSMC New Delhi / IMD · National Bulletin No. {NO} ({DS}, {TS} IST) — {b['name']}: {LAT}°N, {LON}°E, winds {W} km/h (gusting {G}); {b.get('warning','')}; landfall ({LF}) is a FORECAST, it has not happened yet",
                    'hi': f"Source: RSMC नई दिल्ली / IMD · राष्ट्रीय बुलेटिन संख्या {NO} ({DS}, {TS} IST) — {b['name']}: {LAT}°N, {LON}°E, हवा {W} km/h (झोंके {G}); {WS}; landfall ({LF}) FORECAST है, अभी हुआ नहीं है"},
                'announce': {
                    'default': f"Khatra signal! {t['disp']} {t['sea_hi']} me sakriya hai — {WS} jaari hai. Forecast hai ki ye {CWX} {CD} {CM} ko {LF} ke beech landfall kar sakta hai. Taiyaari abhi se karo.",
                    'en': f"Danger signal! {t['disp_plain']} is active over the {t['sea']} — {b.get('warning','')} is in force. It is forecast to make landfall near {LF} ({CWX}, {CD} {CM}). Prepare now.",
                    'hi': f"ख़तरा संकेत! {t['disp']} {t['sea_hi']} में सक्रिय है — {WS} जारी है। Forecast है कि यह {CD} {CM} को {LF} के बीच landfall कर सकता है। तैयारी अभी से करो।"},
            }
        else:
            sos = {
                'place': {'default': '', 'en': '', 'hi': ''},
                'nowP': {
                    'default': "Abhi koi emergency nahi hai — koi toofaan landfall watch par nahi hai. Phir bhi ye number hamesha yaad rakho.",
                    'en': "There is no emergency right now — no storm is on landfall watch. Still, always remember these numbers.",
                    'hi': "अभी कोई emergency नहीं है — कोई तूफ़ान landfall watch पर नहीं है। फिर भी ये number हमेशा याद रखो।"},
                'src': {
                    'default': f"Source: RSMC New Delhi / IMD · National Bulletin No. {NO} ({DS}, {TS} IST)",
                    'en': f"Source: RSMC New Delhi / IMD · National Bulletin No. {NO} ({DS}, {TS} IST)",
                    'hi': f"Source: RSMC नई दिल्ली / IMD · राष्ट्रीय बुलेटिन संख्या {NO} ({DS}, {TS} IST)"},
                'announce': {
                    'default': "Abhi koi khatra signal nahi hai.",
                    'en': "No danger signal right now.",
                    'hi': "अभी कोई ख़तरा संकेत नहीं है।"},
            }
        new_s7b = s7b
        for field, vals in sos.items():
            new_s7b = replace_pack_field(new_s7b, field, vals)
        if new_s7b != s7b:
            html = replace_b64_script(html, em2, new_s7b)
    # --- static HTML fallback slots (pre-JS text + what Google reads) ---
    if calm:
        hero_note = (f"Live snapshot: {DS}, {TS} IST · IMD National Bulletin No. {NO} · abhi koi sakriya cyclone nahi hai · samundar shaant hai · "
                     f"asli taaza khabar sirf RSMC/IMD bulletin se milegi, neeche official live links diye hain")
        static_vc = f"Live update · {DS} (IMD National Bulletin No. {NO}, {TS} IST) — sakriya cyclone: 0 · samundar shaant hai"
        tag_txt = f"Live cyclone watch · Live update {DS} · IMD National Bulletin No. {NO} ({TS} IST) · Sakriya toofaan: 0 · Samundar shaant hai"
    elif is_storm:
        hero_note = (f"Live snapshot: {DS}, {TS} IST · IMD National Bulletin No. {NO} · {t['disp']} sakriya hai ({LAT}°N, {LON}°E) · "
                     f"{WS} · FORECAST landfall: {LF}, {CWX} {CD} {CM} (abhi hua nahi hai) · "
                     f"asli taaza khabar sirf RSMC/IMD bulletin se milegi, neeche official live links diye hain")
        static_vc = (f"Live update · {DS} (IMD National Bulletin No. {NO}, {TS} IST) — 1 sakriya cyclone: {t['disp']} ({LAT}°N, {LON}°E; hawa {W} km/h, jhonke {G}) · "
                     f"{WS} · FORECAST: {CD} {CM} {LF} ke beech landfall sambhav")
        season = 'post-monsoon peak' if b['date'].month >= 10 else ('pre-monsoon season' if b['date'].month >= 4 else 'winter season')
        tag_txt = (f"Live cyclone watch · Live update {DS} · IMD National Bulletin No. {NO} ({TS} IST) · Sakriya toofaan: 1 — {t['disp']} ({LAT}°N, {LON}°E) · "
                   f"{WS} · Season: {season}")
    else:
        hero_note = (f"Live snapshot: {DS}, {TS} IST · IMD National Bulletin No. {NO} · abhi cyclonic storm nahi — ek {b['stage']} ({b['basin']}) sakriya hai ({LAT}°N, {LON}°E) · "
                     f"asli taaza khabar sirf RSMC/IMD bulletin se milegi, neeche official live links diye hain")
        static_vc = f"Live update · {DS} (IMD National Bulletin No. {NO}, {TS} IST) — sakriya cyclone: 0 · ek {b['stage']} ({b['basin']}) sakriya hai"
        tag_txt = f"Live cyclone watch · Live update {DS} · IMD National Bulletin No. {NO} ({TS} IST) · Sakriya toofaan: 0 · Ek {b['stage']} sakriya hai"
    html = re.sub(r'(<p class="hero-note">)(.*?)(</p>)', lambda mo: mo.group(1) + hero_note + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(id="cyVcActiveSub">)(.*?)(</div>)', lambda mo: mo.group(1) + static_vc + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(id="cySummary">)(.*?)(</p>)', lambda mo: mo.group(1) + sums['default'] + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'Live cyclone watch · Live update [^<]*', tag_txt, html, count=1)
    # deeper static fallbacks (JS re-renders them, but Google/no-JS sees these)
    if calm:
        intro_p = (f"Sabse pehle woh baat jo is map ko imaandar banati hai: <b>ab koi sakriya cyclone NAHI hai</b> "
                   f"(IMD National Bulletin No. {NO}, {DS}, {TS} IST) — samundar shaant hai, isiliye upar wale 3D globe par aur neeche is 2D map par koi laal ghumav nahi dikhega. "
                   f"Jaise hi IMD koi naya system ghoshit karega, ye page usi bulletin se khud update ho jayega.")
        legend_txt = "Laal ghumav = sakriya toofaan (abhi koi nahi)"
        calm_inner = ("<b>Abhi koi cyclone sakriya nahi hai</b><span>Samundar abhi shaant hai. Globe par sirf LIVE haal dikhta hai — "
                      "halke neele ghumav woh 7 watch kinare hain jahan nazar rakhi ja rahi hai.</span>")
        lv_sub = f"Koi sakriya cyclone nahi — samundar shaant hai · IMD Bulletin No. {NO} ({DS}, {TS} IST)"
    elif is_storm:
        intro_p = (f"Sabse pehle woh baat jo is map ko imaandar banati hai: <b>ab ek sakriya cyclone HAI — {t['disp']}</b> "
                   f"(IMD National Bulletin No. {NO}, {DS}, {TS} IST): <b>{LAT}°N, {LON}°E</b> par kendrit, hawa {W} km/h (jhonke {G}), "
                   f"lagbhag {SP} km/h se {DH} ki ore badhta hua — isiliye upar wale 3D globe par aur neeche is 2D map par ye "
                   f"<b>laal ghumav ‘{b['name']} — LIVE’</b> bankar dikh raha hai, aur map par iska <b>FORECAST track {LF} wale kinare</b> ki ore jaata hai. "
                   f"IMD ne <b>{b.get('warning','')}</b> jaari kiya hai — landfall abhi hua NAHI hai, ye FORECAST hai.")
        legend_txt = f"Laal ghumav = sakriya toofaan — {t['disp']} LIVE ({LAT}°N, {LON}°E)"
        calm_inner = (f"<b>{t['disp']} — LIVE</b><span>IMD National Bulletin No. {NO} ({DS}, {TS} IST): {b['name']} {LAT}°N, {LON}°E par hai — "
                      f"hawa {W} km/h, jhonke {G} km/h tak. FORECAST: {CR} Globe par laal ghumav hi {b['name']} hai; halke neele ghumav woh 7 watch kinare hain.</span>")
        lv_sub = f"{t['disp']} LIVE — {LAT}°N, {LON}°E · IMD Bulletin No. {NO} ({DS}, {TS} IST) · {WS}"
    else:
        intro_p = (f"Sabse pehle woh baat jo is map ko imaandar banati hai: <b>ab koi sakriya cyclone NAHI hai</b> — lekin {t['sea_hi']} me ek "
                   f"<b>{b['stage']} ({b['basin']})</b> sakriya hai (IMD National Bulletin No. {NO}, {DS}, {TS} IST), kendr <b>{LAT}°N, {LON}°E</b>. "
                   f"Ye abhi cyclone nahi hai, isiliye globe par laal ghumav nahi hai — is par sirf nazar rakhi ja rahi hai.")
        legend_txt = "Laal ghumav = sakriya toofaan (abhi koi nahi — ek depression hai)"
        calm_inner = (f"<b>Cyclone abhi nahi — lekin {b['stage']} {b['basin']} aa raha hai</b><span>IMD bulletin ({DS}, {TS} IST): {b['stage']} "
                      f"{LAT}°N, {LON}°E par hai — {t['dist_hi']}</span>")
        lv_sub = f"Koi sakriya cyclone nahi — ek {b['stage']} ({b['basin']}) sakriya hai · IMD Bulletin No. {NO} ({DS}, {TS} IST)"
    html = re.sub(r'(India ke kinare — abhi toofaan ka haal kya hai\?</h2><p>)(.*?)(</p>)',
                  lambda mo: mo.group(1) + intro_p + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'Laal ghumav = sakriya toofaan — [^<]*', legend_txt, html, count=1)
    html = re.sub(r'Laal ghumav = sakriya toofaan \([^<]*', legend_txt, html, count=1)
    html = re.sub(r'RSMC snapshot [^<]*?re-scan har 5 min', f"RSMC snapshot {DS} · Bulletin No. {NO} · {TS} IST · re-scan har 5 min", html, count=1)
    html = re.sub(r'(id="cyGlobeCalm">)(.*?)(</div>)', lambda mo: mo.group(1) + calm_inner + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(id="cyGlobeCap">)([^<]*)(</span>)', lambda mo: mo.group(1) + extra['@globe_cap']['default'] + mo.group(3), html, count=1)
    html = re.sub(r'(id="cyGlobeCardP2"[^>]*>)(.*?)(</p>)', lambda mo: mo.group(1) + extra['@globe_card_p2']['default'] + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(<span class="lv-sub">)(.*?)(</span>)', lambda mo: mo.group(1) + lv_sub + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(id="cyAreaResult"><div class="ar-verdict">)(.*?)(</div>)', lambda mo: mo.group(1) + extra['@area_default']['default'] + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(id="cy26P"[^>]*>)(.*?)(</p>)', lambda mo: mo.group(1) + extra['@y2026_p']['default'] + mo.group(3), html, count=1, flags=re.S)
    html = re.sub(r'(id="vayuBubble">)(.*?)(</div>)', lambda mo: mo.group(1) + extra['@walker_bubble']['default'] + mo.group(3), html, count=1, flags=re.S)
    if html == open(path, encoding='utf-8').read():
        log(site, 'no change')
        return False
    save_page(path, html)
    log(site, f"updated: Bulletin No. {b['no']} — {t['disp_plain']} at {b['lat']}N {b['lon']}E, calm={calm}")
    return True


def replace_tips_first(script, lang, new_tip):
    """In the lang pack, @tips is an array; replace its FIRST element when it
    carries a live claim (mentions LIVE). Other tips stay untouched."""
    out = script
    pos = 0
    while True:
        lm = re.compile(r'"' + re.escape(lang) + r'"\s*:\s*\{').search(out, pos)
        if not lm:
            break
        brace = out.find('{', lm.start())
        try:
            _, block_end = extract_js_object(out, brace)
        except ValueError:
            break
        km = re.compile(r'"@tips"\s*:\s*\[\s*"').search(out, brace, block_end)
        if km:
            vstart = out.find('"', km.end() - 1)
            val, vend = extract_js_string(out, vstart)
            if 'LIVE' in val:
                esc = new_tip.replace('\\', '\\\\').replace('"', '\\"')
                out = out[:vstart] + '"' + esc + '"' + out[vend:]
                pos = vstart + len(esc) + 2
                continue
        pos = block_end
    return out


def replace_pack_field(script, field, values):
    """Replace an UNQUOTED field (place:"...", nowP:"...") inside every
    language pack of a script. values maps lang -> text; other languages
    receive the 'en' text (the sites' own fallback convention)."""
    langs = ['default', 'en', 'hi', 'bn', 'te', 'ta', 'mr', 'gu', 'kn', 'ml', 'pa', 'or', 'as', 'ur']
    out = script
    for lang in langs:
        val = values.get(lang, values.get('en', ''))
        pos = 0
        while True:
            lm = re.compile(r'"' + lang + r'"\s*:\s*\{').search(out, pos)
            if not lm:
                break
            brace = out.find('{', lm.start())
            try:
                _, block_end = extract_js_object(out, brace)
            except ValueError:
                break
            km = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(field) + r'\s*:\s*"').search(out, brace, block_end)
            if km:
                vstart = out.find('"', km.start())
                _, vend = extract_js_string(out, vstart)
                esc = val.replace('\\', '\\\\').replace('"', '\\"')
                out = out[:vstart] + '"' + esc + '"' + out[vend:]
                pos = vstart + len(esc) + 2
            else:
                pos = block_end
    return out


def replace_i18n_value(script, lang, key, new_value):
    """Replace the string value of "key" inside the "lang" pack of an i18n
    script. Packs look like "default": {...}, "en": {...}, "hi": {...}.
    Only the FIRST pack block for lang that contains key is patched; the
    pages repeat packs per section, so patch every block that has the key."""
    out = script
    pos = 0
    while True:
        lm = re.compile(r'"' + re.escape(lang) + r'"\s*:\s*\{').search(out, pos)
        if not lm:
            break
        brace = out.find('{', lm.start())
        try:
            _, block_end = extract_js_object(out, brace)
        except ValueError:
            break
        km = re.compile(r'"' + re.escape(key) + r'"\s*:\s*"').search(out, brace, block_end)
        if km:
            qpos = out.find('"', km.start())
            # find the value string start (the quote after the colon)
            vstart = out.find('"', km.end() - 1)
            _, vend = extract_js_string(out, vstart)
            esc = new_value.replace('\\', '\\\\').replace('"', '\\"')
            out = out[:vstart] + '"' + esc + '"' + out[vend:]
            pos = vstart + len(esc) + 2
        else:
            pos = block_end
    return out


# ---------------------------------------------------------------------------
# FLOOD — CWC Daily Flood Bulletin PDF -> STATIONS + table + citations
# ---------------------------------------------------------------------------

CWC_HOSTS = ['https://cwc.gov.in', 'https://cwcv1.adgstaging.in']
CWC_STATES = ('Andhra Pradesh|Arunachal Pradesh|Assam|Bihar|Chhattisgarh|Gujarat|Haryana|'
              'Himachal Pradesh|Jammu and Kashmir|Jharkhand|Karnataka|Kerala|Madhya Pradesh|'
              'Maharashtra|Odisha|Punjab|Rajasthan|Sikkim|Tamil Nadu|Telangana|Uttar Pradesh|'
              'Uttarakhand|West Bengal|Delhi|Tripura|Meghalaya|Manipur|Mizoram|Nagaland|Goa')

# Gauge stations whose location is confidently their town/gauge site.
STATION_COORDS = {
    'dumariaghat': (26.50403, 84.39588), 'baltara': (25.86, 86.48),
    'neamatighat': (26.86, 94.24), 'guwahati': (26.17, 91.70), 'pandu': (26.17, 91.70),
    'dibrugarh': (27.48, 94.91), 'tezpur': (26.63, 92.80), 'goalpara': (26.17, 90.62),
    'dhubri': (26.02, 89.98), 'silchar': (24.83, 92.78), 'annapurna ghat': (24.83, 92.78),
    'patna': (25.61, 85.17), 'gandhi ghat': (25.61, 85.17), 'bhagalpur': (25.24, 87.01),
    'varanasi': (25.32, 82.99), 'prayagraj': (25.43, 81.85), 'allahabad': (25.43, 81.85),
    'lucknow': (26.85, 80.91), 'ayodhya': (26.80, 82.20), 'gorakhpur': (26.76, 83.37),
    'ballia': (25.76, 84.15), 'buxar': (25.56, 83.98), 'hajipur': (25.69, 85.21),
    'darbhanga': (26.15, 85.90), 'muzaffarpur': (26.12, 85.36), 'saharsa': (25.88, 86.60),
    'purnia': (25.78, 87.47), 'katihar': (25.54, 87.58), 'kolkata': (22.57, 88.36),
    'durgapur': (23.52, 87.31), 'farakka': (24.80, 87.92), 'jalpaiguri': (26.52, 88.72),
    'cooch behar': (26.32, 89.45), 'cuttack': (20.46, 85.88), 'sambalpur': (21.47, 83.97),
    'bhubaneswar': (20.30, 85.82), 'raipur': (21.25, 81.63), 'surat': (21.17, 72.83),
    'bharuch': (21.71, 73.00), 'ahmedabad': (23.03, 72.58), 'hyderabad': (17.39, 78.49),
    'vijayawada': (16.51, 80.62), 'rajahmundry': (17.00, 81.78), 'bhadrachalam': (17.67, 80.89),
    'dowleswaram': (16.96, 81.76), 'polavaram': (17.25, 81.65), 'nellore': (14.44, 79.99),
    'nanded': (19.15, 77.33), 'sangli': (16.85, 74.58), 'karad': (17.29, 74.18),
    'pune': (18.52, 73.86), 'nashik': (20.00, 73.78), 'chennai': (13.08, 80.27),
    'madurai': (9.93, 78.12), 'jabalpur': (23.16, 79.99), 'hoshangabad': (22.75, 77.72),
    'narmadapuram': (22.75, 77.72), 'jagdalpur': (19.08, 82.03), 'konta': (17.80, 81.38),
    'garudeshwar': (21.88, 73.66), 'burhanpur': (21.31, 76.23),
}


def cwc_filenames(d):
    dd, mm, yy = f'{d.day:02d}', f'{d.month:02d}', str(d.year)
    return [f'cfcrcwcdfb-{dd}.{mm}.{yy}_5.pdf', f'cfcrcwcdfb-{dd}.{mm}.{yy}_0.pdf',
            f'cfcrcwcdfb{dd}-{mm}-{yy}_5.pdf', f'cfcr-cwcdfb-{dd}{mm}{yy}_0.pdf',
            f'cfcrcwcdfb-{dd}.{mm}.{yy}.pdf']


def discover_cwc_bulletin():
    """Try the newest plausible CWC bulletin URLs (last 10 days), most likely
    filename pattern first, under an overall deadline so a slow/blocked gov
    host can never stall the whole run. Returns (parsed, raw) or (None, None)."""
    import time
    deadline = time.time() + 150
    today = datetime.now(IST).date()
    days = [today - timedelta(days=back) for back in range(0, 11)]
    patterns = []
    for d in days:
        patterns.append(cwc_filenames(datetime(d.year, d.month, d.day)))
    # try pattern #0 for every date first, then #1, ... (newest bulletin wins)
    for pidx in range(len(patterns[0])):
        for didx, d in enumerate(days):
            if time.time() > deadline:
                return None, None
            fn = patterns[didx][pidx]
            for host in CWC_HOSTS:
                for base in ('/sites/default/files/', '/'):
                    if time.time() > deadline:
                        return None, None
                    try:
                        data = fetch(host + base + fn, timeout=10)
                    except Exception:
                        continue
                    if not data[:4] == b'%PDF':
                        continue
                    text = pdf_text(data)
                    parsed = parse_cwc_bulletin(text)
                    if parsed:
                        return parsed, text
    return None, None


def parse_cwc_bulletin(text):
    if not text or 'Bulletin No' not in text:
        return None
    m = re.search(r'Bulletin No:\s*(\d+)', text)
    # pypdf scrambles the header: the date (DD/MM/YYYY) can precede its label.
    dm = re.search(r'(\d{2})/(\d{2})/(\d{4})', text[:4000])
    if not m or not dm:
        return None
    b = {'no': int(m.group(1)),
         'date': datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))}
    idx = text.find('Total number of sites above Warning Level')
    if idx < 0:
        return None
    nums = [int(x) for x in re.findall(r'\d+', text[idx:idx + 500])]
    # the label itself holds no digits; the first 4 integers are A, B, C, Total
    if len(nums) < 4:
        return None
    a, bb, c, total = nums[0], nums[1], nums[2], nums[3]
    if a + bb + c != total or total > 500:
        return None
    # A = Extreme (HFL exceeded), B = Severe (at/above Danger Level),
    # C = Above Normal (at/above Warning Level). Danger-level sites = A + B.
    b['danger'] = a + bb
    b['total'] = total
    b['stations'] = []
    if total > 0:
        pat = re.compile(r'\n([A-Za-z][A-Za-z .()&]+)\n([A-Za-z][A-Za-z .()&]+)\n([A-Z][A-Z .&]{2,})\n(' + CWC_STATES + r')\n')
        seen = set()
        for sm in pat.finditer(text):
            river, station, district, state = (clean_ws(sm.group(1)), clean_ws(sm.group(2)),
                                               clean_ws(sm.group(3)).title(), sm.group(4))
            key = station.lower()
            if key in seen or len(station) < 3:
                continue
            seen.add(key)
            b['stations'].append({'name': station, 'river': river, 'district': district, 'state': state})
        if len(b['stations']) != total:
            return None
        for s in b['stations']:
            coords = STATION_COORDS.get(s['name'].lower())
            if not coords:
                return None
            s['lat'], s['lon'] = coords
    return b


def proximity_replace(text, old, new, markers, window=170):
    """Replace `old` with `new` only where a marker word sits nearby."""
    out = []
    i = 0
    while True:
        j = text.find(old, i)
        if j < 0:
            out.append(text[i:])
            break
        ctx = text[max(0, j - window):j + len(old) + window]
        out.append(text[i:j])
        out.append(new if any(mk in ctx for mk in markers) else old)
        i = j + len(old)
    return ''.join(out)


def update_flood():
    site = 'flood'
    b, _raw = discover_cwc_bulletin()
    if not b:
        log(site, 'no verifiable CWC bulletin found (gov site unreachable or format changed) — skipped')
        return False
    path, html = load_page(site)
    dstr = fmt_date(b['date'])
    stations_js = [[s['name'], s['river'], s['lat'], s['lon']] for s in b['stations']]
    new_html = replace_js_assignment(html, r'var STATIONS=', js_json(stations_js))
    # live stations table rows
    if b['stations']:
        rows = ''.join(f'<tr><td>{s["name"]}</td><td>{s["river"]}, {s["state"]}</td>'
                       f'<td><span class="tag">ABOVE WARNING</span></td></tr>' for s in b['stations'])
    else:
        rows = '<tr><td colspan="3">Abhi koi station warning level se upar nahi hai — nadiyan shaant hain</td></tr>'
    new_html = re.sub(r'(<th data-i18n="thStatus">[^<]*</th></tr>)(.*?)(</table>)',
                      lambda mo: mo.group(1) + rows + mo.group(3), new_html, count=1, flags=re.S)
    # narrative templates for the i18n liveText / liveNote keys + static HTML
    n = len(b['stations'])
    if n == 0:
        lt = {
            'default': f"Central Water Commission (CWC) ka abhi koi bhi river station warning level se upar nahi hai — aur koi bhi station danger level par nahi hai. Ye CWC ke Daily Flood Bulletin No. {b['no']} ({dstr}) ka haal hai — nadiyan abhi shaant hain.",
            'en': f"No Central Water Commission (CWC) river station is above warning level right now — and no station is at danger level either. This is the state per CWC Daily Flood Bulletin No. {b['no']} ({dstr}) — the rivers are calm.",
            'hi': f"Central Water Commission (CWC) का अभी कोई भी river station warning level से ऊपर नहीं है — और कोई भी station danger level पर नहीं है। यह CWC के Daily Flood Bulletin No. {b['no']} ({dstr}) का हाल है — नदियाँ अभी शांत हैं।"}
        ln = {
            'default': f"Map par abhi koi live warning station nahi hai. Danger level: 0 — iska matlab hai abhi koi station sabse khatarnak star par nahi hai. (CWC Bulletin No. {b['no']}, {dstr})",
            'en': f"There is no live warning station on the map right now. Danger level: 0 — no station is at the most dangerous level. (CWC Bulletin No. {b['no']}, {dstr})",
            'hi': f"Map पर अभी कोई live warning station नहीं है। Danger level: 0 — कोई station सबसे ख़तरनाक स्तर पर नहीं है। (CWC Bulletin No. {b['no']}, {dstr})"}
    else:
        lst = ', '.join(f"{s['river']} par {s['name']} ({s['district']}, {s['state']})" for s in b['stations'])
        lst_en = ', '.join(f"{s['name']} on the {s['river']} ({s['district']}, {s['state']})" for s in b['stations'])
        danger_txt = f"aur {b['danger']} station danger level par hain" if b['danger'] else "aur koi bhi station danger level par nahi hai"
        danger_en = f"and {b['danger']} station(s) are at danger level" if b['danger'] else "and no station is at danger level"
        lt = {
            'default': f"Central Water Commission (CWC) ke {n} river station warning level se upar hain — {lst} — {danger_txt}. Ye CWC ke Daily Flood Bulletin No. {b['no']} ({dstr}) ka haal hai.",
            'en': f"{n} Central Water Commission (CWC) river station(s) are above warning level — {lst_en} — {danger_en}. This is the state per CWC Daily Flood Bulletin No. {b['no']} ({dstr}).",
            'hi': f"Central Water Commission (CWC) के {n} river station warning level से ऊपर हैं — {lst} — यह CWC के Daily Flood Bulletin No. {b['no']} ({dstr}) का हाल है।"}
        names = ', '.join(s['name'] for s in b['stations'])
        ln = {
            'default': f"Ye {n} station ({names}) map par narangi diamond se dikhte hain. Danger level: {b['danger']} — poori jaankari CWC Bulletin No. {b['no']} ({dstr}) me hai.",
            'en': f"These {n} station(s) ({names}) show as orange diamonds on the map. Danger level: {b['danger']} — full details in CWC Bulletin No. {b['no']} ({dstr}).",
            'hi': f"ये {n} station ({names}) map पर नारंगी diamond से दिखते हैं। Danger level: {b['danger']} — पूरी जानकारी CWC Bulletin No. {b['no']} ({dstr}) में है।"}
    mm, pack = find_b64_script(new_html, 'liveText')
    if mm is not None:
        new_pack = pack
        for key, vals in (('liveText', lt), ('liveNote', ln)):
            for lang in ('default', 'en', 'hi'):
                new_pack = replace_i18n_value(new_pack, lang, key, vals[lang])
            for lang in ('bn', 'te', 'ta', 'mr', 'gu', 'kn', 'ml', 'pa', 'or', 'as', 'ur'):
                new_pack = replace_i18n_value(new_pack, lang, key, vals['en'])
        if new_pack != pack:
            new_html = replace_b64_script(new_html, mm, new_pack)
    new_html = re.sub(r'(data-i18n="liveText">)(.*?)(</p>)', lambda mo: mo.group(1) + lt['default'] + mo.group(3), new_html, count=1, flags=re.S)
    new_html = re.sub(r'(data-i18n="liveNote">)(.*?)(</p>)', lambda mo: mo.group(1) + ln['default'] + mo.group(3), new_html, count=1, flags=re.S)
    # citations: bulletin number + snapshot dates near CWC mentions, everywhere
    old_no = re.search(r'Bulletin No\.?\s*(\d+)', new_html)
    if old_no and int(old_no.group(1)) != b['no']:
        new_html = proximity_replace(new_html, f"Bulletin No. {old_no.group(1)}", f"Bulletin No. {b['no']}", ('CWC', 'Flood', 'Bulletin'))
        new_html = proximity_replace(new_html, f"Bulletin No {old_no.group(1)}", f"Bulletin No {b['no']}", ('CWC', 'Flood', 'Bulletin'))
    old_dates = set(re.findall(r'\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}', new_html))
    for od in old_dates:
        if od != dstr:
            new_html = proximity_replace(new_html, od, dstr, ('CWC', 'Bulletin'))
    # Jal bot LIVE sentence: station clause
    names_txt = ', '.join(f"{s['name']}, {s['river']}" for s in b['stations']) if b['stations'] else 'koi nahi'
    new_html = re.sub(r'— \d+ station चेतावनी से ऊपर \([^)]*\), ख़तरे पर \d+',
                      f"— {n} station चेतावनी से ऊपर ({names_txt}), ख़तरे पर {b['danger']}", new_html)
    new_html = re.sub(r'CWC का \d+ station जो चेतावनी से ऊपर है — [^।<]*',
                      f"CWC के {n} station jo chetaavni se upar hain — {names_txt}" if n else "CWC ka koi station chetaavni se upar nahi hai", new_html)
    new_html = re.sub(r'the \d+ CWC station above warning — [^.<]*',
                      f"the {n} CWC station(s) above warning — {names_txt}" if n else "no CWC station above warning right now", new_html)
    new_html = re.sub(r'— \d+ station above warning \([^)]*\), \d+ at danger',
                      f"— {n} station above warning ({names_txt}), {b['danger']} at danger" if n else f"— no station above warning, {b['danger']} at danger", new_html)
    new_html = re.sub(r'Live haal: \d+ station warning se upar \([^)]*\), danger par \d+',
                      f"Live haal: {n} station warning se upar ({names_txt}), danger par {b['danger']}" if n else f"Live haal: koi station warning se upar nahi, danger par {b['danger']}", new_html)
    if new_html == html:
        log(site, 'no change')
        return False
    save_page(path, new_html)
    log(site, f"updated: CWC Bulletin No. {b['no']} ({dstr}) — {n} warning station(s), danger {b['danger']}")
    return True


# ---------------------------------------------------------------------------
# LANDSLIDE — IMD district warnings (WFS) -> window.LNRAIN rain-trigger zones
# ---------------------------------------------------------------------------

IMD_WFS = ('https://reactjs.imd.gov.in/geoserver/ows?service=WFS&version=1.1.0'
           '&request=GetFeature&typename=imd:district_warnings_india'
           '&srsname=EPSG:4326&outputFormat=application/json')

# Landslide-prone hill districts (UPPERCASE as in the IMD feed) -> state.
PRONE_DISTRICTS = {}
for _state, _names in {
    'Kerala': 'WAYANAD,IDUKKI,MALAPPURAM,KOZHIKODE,KOTTAYAM,PATHANAMTHITTA,THIRUVANANTHAPURAM,KOLLAM,ERNAKULAM,THRISSUR,PALAKKAD,KANNUR,KASARAGOD,ALAPPUZHA',
    'Tamil Nadu': 'NILGIRIS,THE NILGIRIS,COIMBATORE,THENI,DINDIGUL,KANYAKUMARI,TIRUNELVELI,MADURAI,VIRUDHUNAGAR',
    'Karnataka': 'KODAGU,DAKSHINA KANNADA,UDUPI,UTTARA KANNADA,CHIKKAMAGALURU,HASSAN,SHIVAMOGGA,CHAMARAJANAGARA',
    'Maharashtra': 'RAIGAD,RATNAGIRI,SINDHUDURG,SATARA,PUNE,KOLHAPUR,NASHIK,THANE,PALGHAR,AHMEDNAGAR',
    'Goa': 'NORTH GOA,SOUTH GOA',
    'Uttarakhand': 'UTTARKASHI,CHAMOLI,RUDRAPRAYAG,PITHORAGARH,BAGESHWAR,CHAMPAWAT,NAINITAL,ALMORA,PAURI GARHWAL,TEHRI GARHWAL,DEHRADUN,HARIDWAR',
    'Himachal Pradesh': 'SHIMLA,KULLU,MANDI,CHAMBA,KANGRA,KINNAUR,SIRMOUR,SOLAN,LAHUL AND SPITI,LAHAUL AND SPITI,HAMIRPUR,UNA,BILASPUR',
    'Jammu and Kashmir': 'BARAMULLA,ANANTNAG,RAMBAN,DODA,KISHTWAR,POONCH,RAJOURI,UDHAMPUR,REASI,BANDIPORA,GANDERBAL,KUPWARA,KULGAM,SHOPIAN,PULWAMA,BUDGAM,SRINAGAR,KATHUA,SAMBA,JAMMU',
    'Sikkim': 'EAST SIKKIM,WEST SIKKIM,NORTH SIKKIM,SOUTH SIKKIM,GANGTOK,PAKYONG,GYALSHING,MANGAN,NAMCHI,SORENG',
    'West Bengal': 'DARJEELING,KALIMPONG,JALPAIGURI,ALIPURDUAR',
    'Odisha': 'KEONJHAR,MAYURBHANJ,SUNDARGARH,KANDHAMAL,GAJAPATI,RAYAGADA,KORAPUT,MALKANGIRI,NABARANGPUR,KALAHANDI,DEOGARH,ANGUL,DHENKANAL,BOLANGIR',
    'Andhra Pradesh': 'ALLURI SITHARAMA RAJU,PARVATHIPURAM MANYAM,VISAKHAPATNAM,ANAKAPALLI,EAST GODAVARI',
    'Arunachal Pradesh': 'TAWANG,WEST KAMENG,EAST KAMENG,PAPUM PARE,LOWER SUBANSIRI,UPPER SUBANSIRI,WEST SIANG,EAST SIANG,LOWER DIBANG VALLEY,DIBANG VALLEY,ANJAW,LOHIT,CHANGlang'.upper() + ',KURUNG KUMEY,KRA DAADI,SHI YOMI,LEPARADA,SIANG,UPPER SIANG,NAMSAI',
    'Nagaland': 'KOHIMA,DIMAPUR,MOKOKCHUNG,TUENSANG,MON,PHEK,WOKHA,ZUNHEBOTO,PEREN,KIPHIRE,LONGLENG,NOKLAK',
    'Mizoram': 'AIZAWL,LUNGLEI,CHAMPHAI,KOLASIB,SERCHHIP,LAWNGTLAI,MAMIT,SAITUAL,HNAHTHIAL,KHAWZAWL',
    'Manipur': 'SENAPATI,TAMENGLONG,CHURACHANDPUR,UKHRUL,CHANDEL,KANGPOKPI,NONEY,KAMJONG,TENGNOUPAL,PHERZAWL',
    'Meghalaya': 'EAST KHASI HILLS,WEST KHASI HILLS,RI BHOI,EAST JAINTIA HILLS,WEST JAINTIA HILLS,EAST GARO HILLS,WEST GARO HILLS,SOUTH GARO HILLS,NORTH GARO HILLS,SOUTH WEST KHASI HILLS,EASTERN WEST KHASI HILLS',
    'Assam': 'DIMA HASAO,KARBI ANGLONG,WEST KARBI ANGLONG,CACHAR,HAILAKANDI,KARIMGANJ,KAMRUP METRO,KAMRUP,GOALPARA,DHUBRI',
    'Tripura': 'NORTH TRIPURA,DHALAI,KHOWAI,WEST TRIPURA,SOUTH TRIPURA,GOMATI,SEPAHIJALA,UNAKOTI',
    'Chhattisgarh': 'BASTAR,DANTEWADA,SUKMA,BIJAPUR,NARAYANPUR,KONDAGAON,KANKER',
    'Jharkhand': 'RANCHI,HAZARIBAGH,KHUNTI,GUMLA,SIMDEGA,LOHARDAGA,LATEHAR,PALAMU,GARHWA,CHATRA,KODERMA,GIRIDIH',
    'Madhya Pradesh': 'BETUL,CHHINDWARA,SEONI,BALAGHAT,MANDLA,DINDORI,ANUPPUR,UMARIA,SHAHDOL,BURHANPUR,KHANDWA,KHARGONE,ALIRAJPUR,JHABUA',
}.items():
    for _n in _names.split(','):
        PRONE_DISTRICTS[_n.strip()] = _state

RAIN_LEVELS = {17: ('extreme', 3), 16: ('very-heavy', 2), 2: ('heavy', 1)}


def fetch_imd_warnings():
    raw = fetch(IMD_WFS, timeout=150)
    return json.loads(raw.decode('utf-8', errors='replace'))


def polygon_centroid(geom):
    """BBox centre of the largest polygon ring — a stable district marker."""
    try:
        polys = geom['coordinates'] if geom['type'] == 'Polygon' else geom['coordinates']
        best, best_area = None, -1
        rings = [p[0] for p in polys] if geom['type'] == 'MultiPolygon' else [polys[0]]
        for ring in rings:
            xs = [c[0] for c in ring]
            ys = [c[1] for c in ring]
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            if area > best_area:
                best_area = area
                best = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        return best
    except Exception:
        return None


def imd_codes(raw):
    """IMD phenomenon codes come comma-separated ('17,4,8'). Return a set."""
    out = set()
    for part in str(raw or '').split(','):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def rain_zones_from_wfs(geo):
    """Return (entries, feed_date) from the IMD district-warnings GeoJSON."""
    feats = geo.get('features') or []
    if not feats:
        return None, None
    feed_date = None
    entries = []
    for f in feats:
        p = f.get('properties') or {}
        if not feed_date and p.get('Date'):
            feed_date = str(p['Date'])[:10]
        name = clean_ws(str(p.get('District') or '')).upper()
        if name not in PRONE_DISTRICTS:
            continue
        best = None
        days = []
        for day_key in ('Day_1', 'Day_2'):
            codes = imd_codes(p.get(day_key))
            for code in codes:
                if code in RAIN_LEVELS:
                    lvl, sev = RAIN_LEVELS[code]
                    days.append(day_key)
                    if best is None or sev > best[1]:
                        best = (lvl, sev)
        if not best:
            continue
        cen = polygon_centroid(f.get('geometry') or {})
        if not cen:
            continue
        state = PRONE_DISTRICTS[name]
        disp = name.title()
        if days == ['Day_1']:
            daytxt = 'aaj'
        elif days == ['Day_2']:
            daytxt = 'kal'
        else:
            daytxt = 'aaj aur kal'
        if best[0] == 'extreme':
            note = (f"IMD chetaavni: {disp} me {daytxt} atyant bhari baarish (24 ghante me 20 cm se zyada) ka anumaan hai — "
                    f"pahadi ilaakon me landslip ka khatra sabse zyada. Ghaat raaston par khaas savdhaani, bina zaroorat yatra taal do.")
        elif best[0] == 'very-heavy':
            note = (f"IMD chetaavni: {disp} me {daytxt} bahut bhari baarish (12–20 cm) ka anumaan hai — "
                    f"pahadi raaston aur bheegi dhalanon par landslip ka khatra bana rahega. Savdhaani se chalo.")
        else:
            note = (f"IMD chetaavni: {disp} me {daytxt} bhari baarish (6–12 cm) ka anumaan hai — "
                    f"bheegi dhalanon par chhote landslip sambhav hain, khaas kar sadak-kinaare wale hisso me.")
        entries.append({'name': disp, 'place': f'{disp}, {state}', 'state': state,
                        'lat': round(cen[1], 2), 'lon': round(cen[0], 2),
                        'level': best[0], 'note': note, '_sev': best[1]})
    entries.sort(key=lambda e: (-e['_sev'], e['name']))
    for e in entries:
        e.pop('_sev', None)
    return entries[:8], feed_date


def update_landslide():
    site = 'landslide'
    try:
        geo = fetch_imd_warnings()
    except Exception as e:
        log(site, f'IMD warnings feed failed: {e} — skipped')
        return False
    entries, feed_date = rain_zones_from_wfs(geo)
    if entries is None:
        log(site, 'IMD feed empty — skipped')
        return False
    today = datetime.now(IST).date()
    if feed_date:
        try:
            fd = datetime.strptime(feed_date, '%Y-%m-%d').date()
            if abs((today - fd).days) > 1:
                log(site, f'IMD feed date {feed_date} is stale — skipped')
                return False
        except ValueError:
            pass
    path, html = load_page(site)
    mm, s4 = find_b64_script(html, 'window.LNRAIN')
    if mm is None:
        log(site, 'LNRAIN script not found — skipped')
        return False
    new_s4 = replace_js_assignment(s4, r'window\.LNRAIN\s*=', js_json(entries))
    dstr = fmt_date(datetime.now(IST))
    new_s4 = re.sub(r'dated snapshot, \d{1,2} [A-Z][a-z]{2} \d{4}', f'dated snapshot, {dstr}', new_s4)
    if new_s4 == s4:
        log(site, 'no change')
        return False
    html = replace_b64_script(html, mm, new_s4)
    save_page(path, html)
    log(site, f"updated: {len(entries)} IMD rain-trigger zone(s), feed {feed_date}")
    return True


# ---------------------------------------------------------------------------
# COLDWAVE — Open-Meteo minima + IMD cold-wave warning count -> FORECAST
# ---------------------------------------------------------------------------

PLAINS_COORDS = {'Amritsar': (31.64, 74.87), 'Chandigarh': (30.73, 76.78),
                 'Delhi': (28.61, 77.21), 'Jaipur': (26.91, 75.79),
                 'Lucknow': (26.85, 80.91), 'Patna': (25.59, 85.14)}

SPOT_FACTS = {
    'Keylong': 'Lahaul ghaati me paani jamne wali thand ke liye jaani jaati jagah. Pahadi ilaaka hai — yahan IMD ka pahadi cold-wave threshold 0°C se neeche wala hai.',
    'Leh': 'Ladakh me unchai (3,414 m) wali thand — mausam ka aam hissa, lekin bina taiyaari ke musafir ke liye khatarnak.',
    'Kargil': 'Dras (duniya ke sabse thande inhabited ilaakon me se ek) isi jile me hai.',
    'Tawang': 'Purvi Himalaya ki kadaki thand — Sela pass corridor par barf/kaala barf (black ice) ka dhyaan rakho.',
    'Reckong Peo': 'Kinnaur ki pahadi thand — seb ke bagh aur pahadi gaon dono ke liye pala (frost) wala samay nazdeek aata hai.',
    'Srinagar': 'Kashmir ghaati ki raat ki thand — Chilla-i-Kalan (sabse kadake 40 din) ke aas-paas thand aur pakad banati hai.',
    'Shimla': 'Pahadi sheher ki thandi raat — maidanon jaisi halat bilkul nahi.',
    'Gopeshwar': 'Chamoli me halki-madhyam thand — unche gaon (Joshimath/Auli ki taraf) isse kai degree thande hote hain.',
    'Gangtok': 'Sikkim ki thandi raat — aam taur par garam kapde kaafi hain.',
    'Darjeeling': 'Unchai ke hisaab se aam thand — aam taur par koi chetaavni wali sthiti nahi.',
    'Almora': 'Kumaon ki pahadiyon me suhaani-thandi raat — khatre se bahut door.',
    'Kohima': 'Is list ki sabse halki entry — sirf tulna ke liye hai; aam taur par yahan thand ka koi khatra nahi.',
}


def open_meteo_minima(locations):
    """locations: list of (lat, lon). Returns list of tomorrow's minima."""
    lats = ','.join(str(l[0]) for l in locations)
    lons = ','.join(str(l[1]) for l in locations)
    url = (f'https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}'
           f'&daily=temperature_2m_min&timezone=Asia%2FKolkata&forecast_days=3')
    data = json.loads(fetch(url, timeout=60).decode('utf-8', errors='replace'))
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        times = item['daily']['time']
        vals = item['daily']['temperature_2m_min']
        tomorrow = (datetime.now(IST).date() + timedelta(days=1)).isoformat()
        idx = times.index(tomorrow) if tomorrow in times else 1
        out.append(round(float(vals[idx]), 1))
    return out


def update_coldwave():
    site = 'coldwave'
    path, html = load_page(site)
    m = re.search(r'var FORECAST\s*=\s*', html)
    if not m:
        log(site, 'FORECAST not found — skipped')
        return False
    old_fc, _ = extract_js_object(html, m.end())
    old = json.loads(old_fc)
    plains_names = [p['name'] for p in old['plains']]
    if any(n not in PLAINS_COORDS for n in plains_names):
        log(site, 'unknown plains city in FORECAST — skipped')
        return False
    spots_old = old['spots']
    locations = [PLAINS_COORDS[n] for n in plains_names] + [(s['lat'], s['lon']) for s in spots_old]
    try:
        minima = open_meteo_minima(locations)
    except Exception as e:
        log(site, f'Open-Meteo failed: {e} — skipped')
        return False
    if len(minima) != len(locations) or any(v < -45 or v > 30 for v in minima):
        log(site, 'Open-Meteo values failed validation — skipped')
        return False
    try:
        geo = fetch_imd_warnings()
        feats = geo.get('features') or []
        cw_districts = []
        for f in feats:
            p = f.get('properties') or {}
            if 12 in imd_codes(p.get('Day_1')):
                cw_districts.append(clean_ws(str(p.get('District') or '')).title())
        warnings = len(cw_districts)
    except Exception as e:
        log(site, f'IMD warnings feed failed: {e} — skipped')
        return False
    now = datetime.now(IST)
    d1, d2 = now.date(), now.date() + timedelta(days=1)
    if d1.month == d2.month:
        night = f'{d1.day}–{d2.day} {MONTHS[d1.month - 1]} {d1.year}'
    else:
        night = f'{d1.day} {MONTHS[d1.month - 1]} – {d2.day} {MONTHS[d2.month - 1]} {d2.year}'
    dstr = fmt_date(now)
    plains = [{'name': n, 'tmin': minima[i]} for i, n in enumerate(plains_names)]
    spots = []
    for k, s in enumerate(spots_old):
        tmin = minima[len(plains_names) + k]
        fact = SPOT_FACTS.get(s['name'], '')
        spots.append({'elev': s['elev'], 'lat': s['lat'], 'lon': s['lon'], 'name': s['name'],
                      'note': fact, 'place': s.get('place', ''), 'state': s.get('state', ''), 'tmin': tmin})
    coldest_spot = min(spots, key=lambda s: s['tmin'])
    coldest_plains = min(plains, key=lambda p: p['tmin'])
    for s in spots:
        prefix = 'Desh ka sabse thanda anumaan aaj raat — ' if s['name'] == coldest_spot['name'] else ''
        s['note'] = f"{prefix}{s['note']} Aaj raat ka anumaan: {s['tmin']}°C.".strip()
    if warnings == 0:
        summary = (f"Sabse pehle imaandari wali baat: abhi India me koi cold wave SAKRIYA NAHI hai — {dstr} ko IMD ke district warnings "
                   f"dobara check kiye gaye: kisi bhi jile me cold-wave chetaavni sakriya nahi mili. Isiliye is page par koi LIVE cold-wave "
                   f"record nahi hai — neeche ke 34 record sirf itihaas (archive) hain. Aaj raat ({night}) ke anumaan: sabse thanda maidani "
                   f"sheher {coldest_plains['name']} {coldest_plains['tmin']}°C, aur pahadon me sabse thandi jagah {coldest_spot['name']} "
                   f"{coldest_spot['tmin']}°C tak. Ek aur saaf baat: ye saare ankde Open-Meteo ke MODEL FORECAST hain ({dstr} ko liye gaye), "
                   f"naape gaye (observed) tapmaan nahi — pahadi jilon me model ka ankda asal se 2–3 degree idhar-udhar ho sakta hai.")
        checked = f'{dstr} · IMD cold-wave jaanch: koi chetaavni sakriya nahi'
    else:
        names_txt = ', '.join(sorted(set(cw_districts))[:8])
        summary = (f"IMAANDARI WALI BAAT: abhi India ke {warnings} jilon me IMD ki cold-wave chetaavni SAKRIYA hai — {names_txt}. "
                   f"Un ilaakon ke liye ye LIVE chetaavni hai: khaas kar bujurgon, bachon aur bahar sone walon ka dhyaan rakho. "
                   f"Aaj raat ({night}) ke anumaan: sabse thanda maidani sheher {coldest_plains['name']} {coldest_plains['tmin']}°C, aur "
                   f"pahadon me sabse thandi jagah {coldest_spot['name']} {coldest_spot['tmin']}°C tak. Ye tapmaan Open-Meteo ke MODEL "
                   f"FORECAST hain ({dstr} ko liye gaye), naape gaye (observed) nahi — asal tapmaan 2–3 degree idhar-udhar ho sakta hai.")
        checked = f'{dstr} · IMD cold-wave jaanch: {warnings} jilon me chetaavni sakriya'
    new_fc = {'label': f'Aaj raat · {night}', 'night': night, 'plains': plains,
              'sources': old.get('sources', []), 'spots': spots, 'summary': summary,
              'warnings': warnings, 'checked': checked}
    new_html = replace_js_assignment(html, r'var FORECAST\s*=\s*', js_json(new_fc))
    new_html = proximity_replace(new_html, '25 Oct 2026', dstr, ('Open-Meteo', 'Live layer', 'IMD'))
    if new_html == html:
        log(site, 'no change')
        return False
    save_page(path, new_html)
    log(site, f"updated: night {night}, warnings {warnings}, coldest plains {coldest_plains['name']} {coldest_plains['tmin']}C, coldest spot {coldest_spot['name']} {coldest_spot['tmin']}C")
    return True


# ---------------------------------------------------------------------------
# Maker credit (Keshav's name on every page) + sitemap freshness
# ---------------------------------------------------------------------------

CREDIT_HTML = ('<p class="maker-credit" style="text-align:center;font-size:13.5px;line-height:1.6;'
               'opacity:.88;padding:12px 14px;margin:0">Is website ko <b>Keshav Yadav</b> ne banaya hai · '
               'Created by <b>Keshav Yadav</b> · EarthPulse India</p>')


def ensure_credit():
    changed = False
    targets = ['index.html'] + [os.path.join(s, 'index.html') for s in
                                 ('fire', 'flood', 'cyclone', 'landslide', 'coldwave')]
    for rel in targets:
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding='utf-8') as f:
                html = f.read()
        except OSError:
            continue
        if 'maker-credit' in html:
            continue
        if '</footer>' in html:
            html = html.replace('</footer>', CREDIT_HTML + '\n</footer>', 1)
        elif '</body>' in html:
            html = html.replace('</body>', CREDIT_HTML + '\n</body>', 1)
        else:
            continue
        save_page(path, html)
        changed = True
        log('credit', f'maker credit added: {rel}')
    return changed


def update_sitemap(changed_sites):
    path = os.path.join(ROOT, 'sitemap.xml')
    try:
        with open(path, encoding='utf-8') as f:
            xml = f.read()
    except OSError:
        return
    today = datetime.now(IST).date().isoformat()
    new_xml = xml
    for block in re.finditer(r'<url>\s*<loc>([^<]+)</loc>(.*?)</url>', xml, re.S):
        loc, rest = block.group(1), block.group(2)
        hit = any(loc.rstrip('/').endswith('/' + s) for s in changed_sites) or \
            (loc.rstrip('/').endswith('earthpulse-india') and not changed_sites)
        if not hit:
            continue
        if '<lastmod>' in rest:
            new_rest = re.sub(r'<lastmod>[^<]*</lastmod>', f'<lastmod>{today}</lastmod>', rest)
        else:
            new_rest = f'\n    <lastmod>{today}</lastmod>' + rest
        new_xml = new_xml.replace(block.group(0), f'<url><loc>{loc}</loc>{new_rest}</url>', 1)
    if new_xml != xml:
        save_page(path, new_xml)
        log('sitemap', f'lastmod bumped to {today} for {changed_sites}')


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

UPDATERS = {'fire': update_fire, 'cyclone': update_cyclone, 'flood': update_flood,
            'landslide': update_landslide, 'coldwave': update_coldwave}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else 'all'
    names = list(UPDATERS) if which == 'all' else [which]
    changed = []
    for n in names:
        try:
            if UPDATERS[n]():
                changed.append(n)
        except Exception as e:
            log(n, f'ERROR: {e} — site skipped, run continues')
    try:
        if ensure_credit():
            changed.append('credit')
    except Exception as e:
        log('credit', f'ERROR: {e}')
    site_changes = [c for c in changed if c in UPDATERS]
    if site_changes:
        try:
            update_sitemap(site_changes)
        except Exception as e:
            log('sitemap', f'ERROR: {e}')
    print('SUMMARY changed:', changed if changed else 'nothing')
    return 0


if __name__ == '__main__':
    sys.exit(main())
