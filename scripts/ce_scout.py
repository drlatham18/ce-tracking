#!/usr/bin/env python3
"""
CE Scout — weekly continuing education finder for TN LPC/MHSP + CO LPC.

Required GitHub Secrets (Settings → Secrets and variables → Actions → New repository secret):
  ANTHROPIC_API_KEY   console.anthropic.com → API Keys
  BRAVE_API_KEY       api.search.brave.com  (free tier: 2,000 queries/month)
  GMAIL_USER          your Gmail address (used as sender)
  GMAIL_APP_PASSWORD  Google Account → Security → App passwords → create one
  RECIPIENT_EMAIL     where to deliver the weekly report
  TRACKER_URL         https://drlatham18.github.io/ce-tracking  (or leave unset)

Personalized gap analysis (optional but recommended):
  Export JSON from the tracker app → rename the file to ce-data.json →
  commit it to the repo root. The script will load it automatically.
"""

import json
import os
import re
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlencode

import anthropic
import requests

# ── Config ────────────────────────────────────────────────────────────────────────────────────

ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
BRAVE_KEY      = os.environ.get('BRAVE_API_KEY', '')
GMAIL_USER     = os.environ.get('GMAIL_USER', '')
GMAIL_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
RECIPIENT      = os.environ.get('RECIPIENT_EMAIL', GMAIL_USER)
TRACKER_BASE   = os.environ.get('TRACKER_URL', 'https://drlatham18.github.io/ce-tracking').rstrip('/')

REPO_ROOT    = os.path.join(os.path.dirname(__file__), '..')
CE_DATA_PATH = os.path.join(REPO_ROOT, 'ce-data.json')

# ── Requirements (mirrors index.html) ─────────────────────────────────────────────────────────────────────────

TN_START = '2026-08-01'
TN_END   = '2028-08-31'
CO_START = '2025-07-01'
CO_END   = '2027-06-30'

CATEGORIES = ['coursework', 'ethics', 'suicide-prevention', 'presenting', 'consultation', 'other']
CAT_LABELS = {
    'coursework': 'Coursework', 'ethics': 'Ethics',
    'suicide-prevention': 'Suicide Prevention', 'presenting': 'Presenting',
    'consultation': 'Consultation', 'other': 'Other',
}

# ── CE Data & Gap Computation ────────────────────────────────────────────────────────────────────────

def load_ce_data():
    if not os.path.exists(CE_DATA_PATH):
        return None
    with open(CE_DATA_PATH) as f:
        data = json.load(f)
    return data if data.get('entries') else None

def compute_gaps(data):
    entries = data['entries']
    def s(lst): return sum(e['hours'] for e in lst)

    tn = [e for e in entries if 'TN' in e.get('states', []) and TN_START <= e['date'] <= TN_END]
    co = [e for e in entries if 'CO' in e.get('states', []) and CO_START <= e['date'] <= CO_END]

    tn_total   = s(tn)
    tn_ethics  = s([e for e in tn if e['category'] == 'ethics'])
    tn_suicide = s([e for e in tn if e['category'] == 'suicide-prevention'])
    tn_live    = s([e for e in tn if e['format'] in ('live-inperson', 'live-virtual')])
    tn_by_year = {y: s([e for e in tn if e['date'].startswith(str(y))]) for y in [2026, 2027, 2028]}

    co_total  = s(co)
    co_ethics = s([e for e in co if e['category'] == 'ethics'])
    co_by_cat = {cat: s([e for e in co if e['category'] == cat]) for cat in CATEGORIES}

    return {
        'TN': {
            'total': tn_total,   'total_gap': max(0, 20 - tn_total),
            'ethics': tn_ethics, 'ethics_gap': max(0, 3 - tn_ethics),
            'suicide': tn_suicide, 'suicide_gap': max(0, 2 - tn_suicide),
            'live': tn_live,     'live_gap': max(0, 10 - tn_live),
            'by_year': tn_by_year,
            'year_gaps': {y: max(0, 10 - h) for y, h in tn_by_year.items()},
        },
        'CO': {
            'total': co_total,   'total_gap': max(0, 40 - co_total),
            'ethics': co_ethics, 'ethics_gap': max(0, 6 - co_ethics),
            'by_cat': co_by_cat,
            'cap_warnings': {cat: hrs for cat, hrs in co_by_cat.items() if hrs >= 15},
        },
    }

# ── Web Search ──────────────────────────────────────────────────────────────────────────────────

def brave_search(query, count=8):
    try:
        r = requests.get(
            'https://api.search.brave.com/res/v1/web/search',
            headers={'Accept': 'application/json', 'X-Subscription-Token': BRAVE_KEY},
            params={'q': query, 'count': count, 'freshness': 'py'},
            timeout=12,
        )
        r.raise_for_status()
        return [
            {'title': x.get('title',''), 'url': x.get('url',''), 'description': x.get('description','')}
            for x in r.json().get('web', {}).get('results', [])
        ]
    except Exception as e:
        print(f'Search error [{query[:50]}]: {e}', file=sys.stderr)
        return []

def build_search_queries(gaps):
    queries = {
        'ethics_live':    'NBCC approved ethics CEU live webinar online counselor therapist LPC 2026 2027 register',
        'ethics_self':    'NBCC approved ethics continuing education self-paced online LPC counseling affordable',
        'suicide_live':   'suicide prevention assessment intervention CEU live webinar LPC NBCC approved 2026 2027',
        'live_workshops': 'NBCC approved live virtual workshop counseling CEU interactive 2026 2027 therapist',
        'inperson_tn':    'in-person counseling workshop CEU Tennessee Nashville Memphis LPC NBCC approved 2026 2027',
        'inperson_co':    'in-person counseling workshop CEU Colorado Denver Boulder LPC NBCC approved 2026 2027',
        'self_paced':     'counseling CEU self-paced NBCC approved online affordable therapist 2026',
        'state_conf':     'Tennessee Colorado counseling association conference workshop CE 2026 2027',
    }
    if gaps:
        co, tn = gaps['CO'], gaps['TN']
        if co['total_gap'] > 15:
            queries['co_bundles'] = 'counseling CEU bundle package 10+ hours NBCC approved online therapist LPC'
        if tn['suicide_gap'] > 0:
            queries['suicide_extra'] = 'suicide safe messaging counseling CEU NBCC approved online 2026'
        if co['ethics_gap'] > 0 or tn['ethics_gap'] > 0:
            queries['ethics_extra'] = 'professional ethics counseling CEU 3 6 hours NBCC approved online LPC'
    return queries

# ── Claude Analysis ─────────────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a CE compliance specialist for a licensed therapist in Tennessee (LPC/MHSP) and Colorado (LPC).

TENNESSEE LPC/MHSP (cycle Aug 1 2026–Aug 31 2028):
- 20 total hours; ≥10 per calendar year (2026, 2027, 2028); ≥3 ethics; ≥2 suicide prevention; ≥10 live/interactive
- Live/interactive = live in-person OR live virtual
- Accepted: NBCC-approved CEs; TN board-approved CEs

COLORADO LPC (cycle Jul 1 2025–Jun 30 2027):
- 40 total hours; ≥6 ethics; no category >20 hours
- Categories: coursework, ethics, suicide-prevention, presenting, consultation, other
- Accepted: NBCC-approved CEs; CO board-approved CEs

From the web search results provided, identify REAL CE course listings with registration available.
DISCARD: general articles about CE requirements, provider homepages without a specific course, expired events.
INCLUDE: specific courses, upcoming webinars, workshop series, course catalogs with clear hours and topics.

For each valid course, return one JSON object with EXACTLY these fields:
{
  "title": string,
  "provider": string,
  "format": "live-inperson" | "live-virtual" | "self-paced",
  "location": string or "",
  "date_info": string,
  "hours": number or 0,
  "category": "ethics"|"suicide-prevention"|"coursework"|"presenting"|"consultation"|"other",
  "cost": string,
  "url": string,
  "approval": string,
  "fills_tn": [strings — which TN requirements this fills, e.g. "ethics", "live hours", "total hours"],
  "fills_co": [strings — which CO requirements this fills],
  "counts_both": boolean,
  "priority": "critical" | "high" | "standard",
  "compliance_note": string
}

Priority:
- "critical": fills a gap in both states, OR CO deadline within 6 months and CO is behind
- "high": fills a gap in at least one state, OR is live interactive
- "standard": adds hours but fills no specific gap

Return ONLY a JSON array. No markdown fences, no prose. Empty array [] if no valid courses found.
"""

def analyze_with_claude(search_results, gaps, today_str):
    results_block = ''
    for section, items in search_results.items():
        if not items:
            continue
        results_block += f'\n=== {section.upper().replace("_"," ")} ===\n'
        for r in items:
            results_block += f'Title: {r["title"]}\nURL: {r["url"]}\nSnippet: {r["description"]}\n\n'

    gap_block = ''
    if gaps:
        tn, co = gaps['TN'], gaps['CO']
        gap_block = (
            f'CURRENT GAPS:\n'
            f'TN: total_gap={tn["total_gap"]}, ethics_gap={tn["ethics_gap"]}, '
            f'suicide_gap={tn["suicide_gap"]}, live_gap={tn["live_gap"]}\n'
            f'TN year gaps: 2026={tn["year_gaps"][2026]}, 2027={tn["year_gaps"][2027]}, 2028={tn["year_gaps"][2028]}\n'
            f'CO: total_gap={co["total_gap"]}, ethics_gap={co["ethics_gap"]}\n'
            f'CO cap warnings (>=15 hrs): {co["cap_warnings"] or "none"}\n'
        )
    else:
        gap_block = 'No CE tracker data — treat all requirement areas as potential gaps.\n'

    user_msg = f'Today: {today_str}\n{gap_block}\nSEARCH RESULTS:\n{results_block}'

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    # max_tokens must cover thinking output plus the JSON answer, or the
    # response gets truncated mid-thought and contains no text block at all
    msg = client.messages.create(
        model='claude-sonnet-5',
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_msg}],
    )
    if msg.stop_reason == 'max_tokens':
        print('Warning: Claude response hit max_tokens and may be truncated', file=sys.stderr)
    # content may include ThinkingBlock(s); join every text block present
    raw = ''.join(
        block.text for block in msg.content
        if getattr(block, 'type', '') == 'text'
    ).strip()
    if not raw:
        print(f'Claude returned no text blocks (stop_reason={msg.stop_reason})', file=sys.stderr)
        return []
    # strip markdown fences if present
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    # extract just the JSON array — guards against surrounding prose or truncated output
    start = raw.find('[')
    end   = raw.rfind(']')
    if start == -1 or end == -1:
        print(f'No JSON array in Claude response:\n{raw[:500]}', file=sys.stderr)
        return []
    raw = raw[start:end + 1]
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'JSON parse error: {e}\nExtracted: {raw[:500]}', file=sys.stderr)
        return []
    return result if isinstance(result, list) else []

# ── Email Builder ────────────────────────────────────────────────────────────────────────────────────

def log_url(c):
    params = {
        'log': '1',
        'title': c.get('title', ''),
        'provider': c.get('provider', ''),
        'hours': str(c.get('hours', '')),
        'format': c.get('format', 'live-virtual'),
        'category': c.get('category', 'coursework'),
        'approval': c.get('approval', ''),
    }
    return TRACKER_BASE + '/?' + urlencode({k: v for k, v in params.items() if v and v != '0'})

def pbar_row(label, cur, req):
    pct = min(100, round(cur / req * 100)) if req else 100
    met = cur >= req
    col = '#16a34a' if met else ('#2563eb' if pct >= 50 else '#dc2626')
    tick = ' ✓' if met else f' ({req - cur} needed)'
    return (
        f'<tr>'
        f'<td style="font-size:11px;color:#555;padding:3px 6px 3px 0;white-space:nowrap">{label}</td>'
        f'<td style="padding:3px 4px;width:100px">'
        f'<div style="background:#e5e7eb;border-radius:3px;height:5px">'
        f'<div style="background:{col};width:{pct}%;height:5px;border-radius:3px"></div>'
        f'</div></td>'
        f'<td style="font-size:11px;font-weight:600;color:{col};padding:3px 0 3px 6px;white-space:nowrap">'
        f'{cur}/{req}{tick}</td></tr>'
    )

def course_card(c):
    priority = c.get('priority', 'standard')
    p_bg  = {'critical': '#dc2626', 'high': '#d97706', 'standard': '#6b7280'}[priority]
    p_lbl = {'critical': '🔥 CRITICAL GAP', 'high': '⭐ HIGH VALUE', 'standard': '📚 STANDARD'}[priority]

    fmt      = c.get('format', '')
    fmt_icon = {'live-inperson': '📍', 'live-virtual': '💻', 'self-paced': '🕐'}.get(fmt, '📄')
    fmt_lbl  = {'live-inperson': 'Live In-Person', 'live-virtual': 'Live Virtual', 'self-paced': 'Self-Paced'}.get(fmt, fmt)

    loc      = c.get('location', '')
    loc_str  = f' &nbsp;&middot;&nbsp; {escape(loc)}' if loc else ''
    hours    = c.get('hours', 0)
    hrs_str  = f'{hours} hr{"s" if hours != 1 else ""}' if hours else 'hrs TBD'

    fills_tn = ', '.join(c.get('fills_tn', []))
    fills_co = ', '.join(c.get('fills_co', []))
    note     = c.get('compliance_note', '')
    both     = c.get('counts_both', False)

    both_badge = (
        '<span style="display:inline-block;background:#7c3aed;color:#fff;font-size:10px;'
        'font-weight:700;padding:2px 8px;border-radius:10px;margin-left:4px">COUNTS BOTH STATES</span>'
    ) if both else ''

    fills_html = ''
    if fills_tn:
        fills_html += f'<div style="font-size:11px;color:#166534;margin-top:3px">✅ TN: {escape(fills_tn)}</div>'
    if fills_co:
        fills_html += f'<div style="font-size:11px;color:#1e40af;margin-top:2px">✅ CO: {escape(fills_co)}</div>'

    note_html = ''
    if note:
        note_html = (f'<div style="font-size:10px;background:#fffbeb;color:#92400e;'
                     f'padding:4px 8px;border-radius:4px;margin-top:6px">{escape(note)}</div>')

    reg_url  = escape(c.get('url', '#'))
    log_link = escape(log_url(c))

    return f"""
<div style="border:1px solid #e0e3e8;border-radius:8px;padding:14px;margin-bottom:10px;background:#ffffff">
  <div style="margin-bottom:6px">
    <span style="display:inline-block;background:{p_bg};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;letter-spacing:.04em">{p_lbl}</span>{both_badge}
  </div>
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="vertical-align:top">
        <div style="font-size:14px;font-weight:700;color:#111;line-height:1.3">{escape(c.get('title',''))}</div>
        <div style="font-size:12px;color:#555;margin-top:2px">{escape(c.get('provider',''))}</div>
      </td>
      <td style="vertical-align:top;text-align:right;white-space:nowrap;padding-left:12px">
        <div style="font-size:18px;font-weight:800;color:#111">{hrs_str}</div>
      </td>
    </tr>
  </table>
  <table cellpadding="0" cellspacing="0" style="margin-top:8px">
    <tr>
      <td style="font-size:11px;color:#555;padding-right:14px;padding-bottom:3px">{fmt_icon} {fmt_lbl}{loc_str}</td>
      <td style="font-size:11px;color:#555;padding-bottom:3px">📅 {escape(c.get('date_info','See provider'))}</td>
    </tr>
    <tr>
      <td style="font-size:11px;color:#555;padding-right:14px">💰 {escape(c.get('cost','unknown'))}</td>
      <td style="font-size:11px;color:#555">✔ {escape(c.get('approval','unknown'))}</td>
    </tr>
  </table>
  {fills_html}
  {note_html}
  <div style="margin-top:10px">
    <a href="{reg_url}" style="display:inline-block;background:#2563eb;color:#ffffff;font-size:12px;font-weight:600;padding:6px 14px;border-radius:6px;text-decoration:none;margin-right:8px">Register →</a>
    <a href="{log_link}" style="display:inline-block;background:#f4f5f7;color:#1a1d23;font-size:12px;font-weight:600;padding:6px 14px;border-radius:6px;text-decoration:none;border:1px solid #e0e3e8">+ Log in Tracker</a>
  </div>
</div>"""

def build_email(courses, gaps, today_str):
    today_fmt = datetime.strptime(today_str, '%Y-%m-%d').strftime('%B %d, %Y')

    porder = {'critical': 0, 'high': 1, 'standard': 2}
    forder = {'live-inperson': 0, 'live-virtual': 1, 'self-paced': 2}
    sorted_courses = sorted(courses, key=lambda c: (porder.get(c.get('priority'), 2), forder.get(c.get('format'), 2)))
    online_c   = [c for c in sorted_courses if c.get('format') != 'live-inperson']
    inperson_c = [c for c in sorted_courses if c.get('format') == 'live-inperson']

    # Progress section
    if gaps:
        tn, co = gaps['TN'], gaps['CO']
        tn_bars = (
            pbar_row('Total', tn['total'], 20) +
            pbar_row('Ethics', tn['ethics'], 3) +
            pbar_row('Suicide Prev.', tn['suicide'], 2) +
            pbar_row('Live/Interactive', tn['live'], 10)
        )
        tn_year_row = (
            f'<tr><td colspan="3" style="font-size:10px;color:#555;padding:4px 0 0">'
            f'Per year (need ≥10 each) — '
            f'2026: {tn["by_year"][2026]}h &nbsp;&middot;&nbsp; '
            f'2027: {tn["by_year"][2027]}h &nbsp;&middot;&nbsp; '
            f'2028: {tn["by_year"][2028]}h</td></tr>'
        )
        co_bars = pbar_row('Total', co['total'], 40) + pbar_row('Ethics', co['ethics'], 6)
        co_cap_html = ''.join(
            f'<div style="font-size:10px;color:#92400e;margin-top:3px">⚠ {CAT_LABELS.get(cat,cat)}: {hrs}/20 hrs cap</div>'
            for cat, hrs in co.get('cap_warnings', {}).items()
        )
        gap_section = f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px">
<tr>
  <td width="50%" style="padding-right:8px;vertical-align:top">
    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#15803d;margin-bottom:8px">Tennessee LPC/MHSP</div>
      <table cellpadding="0" cellspacing="0">{tn_bars}{tn_year_row}</table>
    </div>
  </td>
  <td width="50%" style="padding-left:8px;vertical-align:top">
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:12px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#1d4ed8;margin-bottom:8px">Colorado LPC</div>
      <table cellpadding="0" cellspacing="0">{co_bars}</table>
      {co_cap_html}
      <div style="font-size:10px;color:#dc2626;font-weight:600;margin-top:4px">Cycle ends Jun 30, 2027</div>
    </div>
  </td>
</tr>
</table>"""
    else:
        gap_section = """
<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px;margin-bottom:20px;font-size:12px;color:#92400e">
<strong>No gap data this week</strong> — export tracker JSON → rename <code>ce-data.json</code> → commit to repo for personalized analysis.
</div>"""

    both_count = sum(1 for c in courses if c.get('counts_both'))
    counts_both_note = ''
    if both_count:
        counts_both_note = (
            f'<p style="font-size:11px;color:#7c3aed;margin-bottom:16px">'
            f'📌 {both_count} course{"s" if both_count>1 else ""} count toward <strong>both states simultaneously</strong> — '
            f'register once to fill TN and CO requirements at the same time.</p>'
        )

    online_html   = ''.join(course_card(c) for c in online_c) or '<p style="color:#6b7280;font-size:13px;padding:8px 0">No online results this week — check back next Monday.</p>'
    inperson_html = ''.join(course_card(c) for c in inperson_c)
    inperson_section = ''
    if inperson_html:
        inperson_section = f"""
<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin:20px 0 10px">📍 In-Person Opportunities</div>
{inperson_html}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif">
<div style="max-width:620px;margin:20px auto;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">

  <div style="background:#1e293b;padding:18px 24px">
    <div style="font-size:17px;font-weight:700;color:#ffffff">📚 Weekly CE Opportunities</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:2px">{today_fmt} &nbsp;&middot;&nbsp; TN LPC/MHSP + CO LPC</div>
  </div>

  <div style="padding:20px 24px">

    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin-bottom:10px">Your Progress</div>
    {gap_section}

    {counts_both_note}

    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin-bottom:10px">💻 Online &amp; Virtual</div>
    {online_html}
    {inperson_section}

    <div style="border-top:1px solid #e0e3e8;margin-top:20px;padding-top:14px;font-size:11px;color:#9ca3af;line-height:1.7">
      <strong>Log a completed CE:</strong> Click "+ Log in Tracker" — your tracker opens with the form pre-filled.
      Enter the completion date, confirm which states apply, and save.<br>
      <strong>Update gap analysis:</strong> Export JSON from tracker → rename to <code>ce-data.json</code> →
      commit to the <a href="https://github.com/drlatham18/ce-tracking" style="color:#2563eb;text-decoration:none">ce-tracking repo</a>.<br>
      TN cycle ends Aug 2028 &nbsp;&middot;&nbsp; <strong style="color:#dc2626">CO cycle ends Jun 30, 2027</strong>
    </div>
  </div>
</div>
</body>
</html>"""

# ── Send Email ───────────────────────────────────────────────────────────────────────────────────

def send_email(html_body, subject):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_USER
    msg['To']      = RECIPIENT
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    print(f'Email sent to {RECIPIENT}')

def send_error_email(tb):
    try:
        msg = MIMEText(f'CE Scout failed this week.\n\n{tb}', 'plain')
        msg['Subject'] = 'CE Scout — Error'
        msg['From']    = GMAIL_USER
        msg['To']      = RECIPIENT
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    except Exception:
        pass

# ── Main ──────────────────────────────────────────────────────────────────────────────────────

def main():
    today_str = date.today().isoformat()
    print(f'CE Scout — {today_str}')

    missing = [k for k, v in [
        ('ANTHROPIC_API_KEY', ANTHROPIC_KEY), ('BRAVE_API_KEY', BRAVE_KEY),
        ('GMAIL_USER', GMAIL_USER), ('GMAIL_APP_PASSWORD', GMAIL_PASSWORD),
    ] if not v]
    if missing:
        raise RuntimeError(f'Missing secrets: {missing}')

    data = load_ce_data()
    gaps = compute_gaps(data) if data else None
    if gaps:
        print(f'Loaded {len(data["entries"])} entries — TN gap: {gaps["TN"]["total_gap"]}h, CO gap: {gaps["CO"]["total_gap"]}h')
    else:
        print('No ce-data.json — running without personalized gap analysis')

    print('Searching...')
    queries = build_search_queries(gaps)
    results = {name: brave_search(q) for name, q in queries.items()}
    print(f'Got {sum(len(v) for v in results.values())} results across {len(results)} queries')

    print('Analyzing with Claude...')
    courses = analyze_with_claude(results, gaps, today_str)
    print(f'Found {len(courses)} CE opportunities')

    print('Sending email...')
    html_body = build_email(courses, gaps, today_str)
    send_email(html_body, f'CE Opportunities — Week of {today_str}')
    print('Done.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        send_error_email(tb)
        sys.exit(1)
