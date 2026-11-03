#!/usr/bin/env python3
"""Графік погодинних відключень (Олександрійський район) -> календарі .ics.

Читає публічну веб-версію Telegram-каналу «Світло Олександрійщини»,
для кожної дати бере найсвіжіший пост і генерує по одному .ics на чергу.
Стан (розклади за минулі дні) зберігається в state.json, бо веб-версія
каналу показує лише ~20 останніх постів.
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CHANNEL_URL = "https://t.me/s/SvitloOleksandriyskohoRaionu"
KYIV = ZoneInfo("Europe/Kyiv")
QUEUES = [f"{a}.{b}" for a in range(1, 7) for b in (1, 2)]
KEEP_DAYS = 14

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
OUT = ROOT / "public"

POST_RE = re.compile(
    r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>.*?datetime="([^"]+)"', re.S
)
# "За розпорядженням НЕК "Укренерго" 03.11.2026 внесено зміни / буде діяти"
TARGET_RE = re.compile(r"Укренерго\W*\s+(\d{2})\.(\d{2})\.(\d{4})")
QUEUE_RE = re.compile(r"Черга\s+(\d\.\d)\s*:\s*([0-9:,\s\-–]+)")
SLOT_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def parse_posts(page):
    """-> {date 'YYYY-MM-DD': {'posted': iso, 'queues': {q: [[start, end], ...]}}}"""
    result = {}
    for m in POST_RE.finditer(page):
        text = re.sub(r"<br\s*/?>", "\n", m.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        if "графік" not in text.lower():
            continue
        t = TARGET_RE.search(text)
        if not t:
            continue
        day = f"{t.group(3)}-{t.group(2)}-{t.group(1)}"
        queues = {}
        for q, slots in QUEUE_RE.findall(text):
            queues[q] = [
                [f"{int(a):02d}:{b}", f"{int(c):02d}:{d}"]
                for a, b, c, d in SLOT_RE.findall(slots)
            ]
        posted = m.group(2)
        if day not in result or posted > result[day]["posted"]:
            result[day] = {"posted": posted, "queues": queues}
    return result


def merge(state, fresh):
    for day, sched in fresh.items():
        if day not in state or sched["posted"] >= state[day]["posted"]:
            state[day] = sched
    cutoff = (datetime.now(KYIV) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    return {d: s for d, s in sorted(state.items()) if d >= cutoff}


def to_utc(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    base = datetime.strptime(day, "%Y-%m-%d")
    local = (base + timedelta(hours=h, minutes=m)).replace(tzinfo=KYIV)  # 24:00 -> next day
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def merge_slots(slots):
    """Склеює суміжні інтервали (10:00-12:00 + 12:00-14:00 -> одна подія)."""
    out = []
    for s, e in sorted(slots):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def build_ics(queue, state):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//svitlo-calendar//UA",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:⚡ Відключення — черга {queue}",
        "X-WR-TIMEZONE:Europe/Kyiv",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]
    for day, sched in state.items():
        for start, end in merge_slots(sched["queues"].get(queue, [])):
            lines += [
                "BEGIN:VEVENT",
                f"UID:{day}-{start.replace(':', '')}-{queue}@svitlo-calendar",
                f"DTSTAMP:{now}",
                f"DTSTART:{to_utc(day, start)}",
                f"DTEND:{to_utc(day, end)}",
                f"SUMMARY:⚡ Немає світла {start}–{end}",
                f"DESCRIPTION:Черга {queue}. Графік від {sched['posted'][:16].replace('T', ' ')} UTC. {CHANNEL_URL}",
                "TRANSP:OPAQUE",
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "DESCRIPTION:Скоро вимкнуть світло",
                "TRIGGER:-PT10M",
                "END:VALARM",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    page = Path(sys.argv[1]).read_text("utf-8") if len(sys.argv) > 1 else fetch(CHANNEL_URL)
    fresh = parse_posts(page)
    if not fresh:
        sys.exit("Не знайдено жодного графіка — можливо, змінився формат сторінки")
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    state = merge(state, fresh)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    OUT.mkdir(exist_ok=True)
    for q in QUEUES:
        (OUT / f"cherha-{q}.ics").write_bytes(build_ics(q, state).encode("utf-8"))
    print(f"Дні: {', '.join(state)}; файли: {OUT}")


if __name__ == "__main__":
    main()
