#!/usr/bin/env python3
"""Графік погодинних відключень (Олександрійський район) -> календарі .ics.

Головне джерело — публічна веб-версія Telegram-каналу «Світло Олександрійщини»
(для кожної дати найсвіжіший пост). Якщо на сьогодні в Telegram графіка немає,
його бере із сайту kiroe.com.ua.
Генерує по одному .ics на чергу.
Стан (розклади за минулі дні) зберігається в state.json, бо веб-версія
каналу показує лише ~20 останніх постів.
"""
import html
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CHANNEL_URL = "https://t.me/s/SvitloOleksandriyskohoRaionu"
SITE_URL = "https://kiroe.com.ua/electricity-blackout"
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
# "08:00-10:00" або коротко "08-10"
SLOT_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def fetch_site():
    """Графік на сьогодні з kiroe.com.ua -> {date: {'posted', 'queues'}} або {}.

    Сайт віддає по пів години: "1" — світла немає, "0" — є, "10" — перемикання
    (сайт показує його як «є світло»), тому відключення — це суцільні "1".
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    page = opener.open(SITE_URL, timeout=30).read().decode("utf-8")
    token = re.search(r'ajaxToken:\s*"([0-9a-f]+)"', page).group(1)
    req = urllib.request.Request(
        f"{SITE_URL}/actual-gpv-graphs",
        data=urllib.parse.urlencode({"ajax": 1, "token": token}).encode(),
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": SITE_URL},
    )
    graphs = json.loads(opener.open(req, timeout=30).read().decode("utf-8"))["data"]
    queues = {}
    for q, g in graphs.items():
        slots, start = [], None
        for hhmm, val in sorted(g["times"].items()) + [("24:00", "0")]:
            if val == "1" and start is None:
                start = hhmm
            elif val != "1" and start is not None:
                slots.append([start, hhmm])
                start = None
        queues[q] = slots
    if not queues:
        return {}
    today = datetime.now(KYIV).strftime("%Y-%m-%d")
    posted = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return {today: {"posted": posted, "queues": queues, "source": "сайт"}}


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
                [f"{int(a):02d}:{b or '00'}", f"{int(c):02d}:{d or '00'}"]
                for a, b, c, d in SLOT_RE.findall(slots)
            ]
        if not queues:  # пост про ГОП тощо — без розкладу по чергах
            continue
        posted = m.group(2)
        if day not in result or posted > result[day]["posted"]:
            result[day] = {"posted": posted, "queues": queues, "source": "Telegram"}
    return result


def merge(state, fresh):
    """Telegram має пріоритет: сайт лише заповнює дні, яких у Telegram немає."""
    for day, sched in fresh.items():
        old = state.get(day)
        if (
            old is None
            or old.get("source") == "сайт"
            or (sched["source"] == "Telegram" and sched["posted"] >= old["posted"])
        ):
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
                f"DESCRIPTION:Черга {queue}. Джерело: {sched.get('source', 'Telegram')}\\, оновлено {sched['posted'][:16].replace('T', ' ')} UTC.",
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
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    found = 0
    for name, load in (("Telegram", lambda: parse_posts(fetch(CHANNEL_URL))), ("сайт", fetch_site)):
        try:
            fresh = load()
        except Exception as e:  # одне джерело впало — працюємо з іншим
            print(f"{name}: помилка {e!r}", file=sys.stderr)
            continue
        found += len(fresh)
        state = merge(state, fresh)
    if not found:
        sys.exit("Жодне джерело не дало графіка — можливо, змінився формат сторінок")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    OUT.mkdir(exist_ok=True)
    for q in QUEUES:
        (OUT / f"cherha-{q}.ics").write_bytes(build_ics(q, state).encode("utf-8"))
    print(f"Дні: {', '.join(state)}; файли: {OUT}")


if __name__ == "__main__":
    main()
