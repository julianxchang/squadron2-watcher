"""Poll the Paperless141 schedule and alert when a Cessna 172 is free for a whole block.

Usage:
    python watcher.py                 # settings from config.json
    python watcher.py --once          # single check
    python watcher.py --test-email    # verify email settings
    python watcher.py --date 2026-09-20 --start 08:00 --end 10:00   # flags override config.json

.env settings for email alerts (Gmail: use an App Password, not your login password):
    SMTP_USER=you@gmail.com           # account that sends the mail
    SMTP_PASS=xxxxxxxxxxxxxxxx
    EMAIL_TO=julianxchang@gmail.com
Optional: SMTP_HOST / SMTP_PORT (default smtp.gmail.com / 465), NTFY_TOPIC for ntfy.sh push.
"""
import argparse
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from session import BASE, login

load_dotenv()

SCHED_URL = BASE + "mstr7p.aspx"
DATE_FIELD = "ctl00$ContentPlaceHolder1$DropDate1"
GRID_ID = "ctl00_ContentPlaceHolder1_GridView2"
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def full_form(soup):
    """Everything a browser would submit: inputs, checked boxes, and selects."""
    data = {}
    for inp in soup.select("input[name]"):
        t = (inp.get("type") or "text").lower()
        if t in ("submit", "image", "button"):
            continue
        if t in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[inp["name"]] = inp.get("value", "on")
            continue
        data[inp["name"]] = inp.get("value", "")
    for sel in soup.select("select[name]"):
        opt = sel.find("option", selected=True) or sel.find("option")
        if opt is not None:
            data[sel["name"]] = opt.get("value", opt.get_text(strip=True))
    return data


def fetch_day(s, day):
    """Load the schedule page and switch it to `day` (YYYY-MM-DD). Returns soup."""
    r = s.get(SCHED_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    if soup.find("input", {"name": "txtPassword"}):
        raise PermissionError("session expired")
    cur = soup.find("input", {"name": DATE_FIELD})
    if cur is None:
        raise RuntimeError("date field not found; page layout changed?")
    if cur.get("value") != day:
        data = full_form(soup)
        data[DATE_FIELD] = day
        data["__EVENTTARGET"] = DATE_FIELD
        data["__EVENTARGUMENT"] = ""
        r = s.post(SCHED_URL, data=data, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        got = soup.find("input", {"name": DATE_FIELD})
        if got is None or got.get("value") != day:
            raise RuntimeError(f"failed to switch to {day} (page shows {got and got.get('value')})")
    return soup


def parse_grid(soup):
    """Return (aircraft, slots). aircraft = [(col, reg, type, loc)],
    slots = {hh:mm: {col: is_free}}."""
    grid = soup.select_one("#" + GRID_ID)
    if grid is None:
        raise RuntimeError("schedule grid not found")
    rows = grid.select("tr")
    regs = [c.get_text(strip=True) for c in rows[0].select("th,td")]
    types = [c.get_text(strip=True) for c in rows[1].select("th,td")]
    locs = [c.get_text(strip=True) for c in rows[2].select("th,td")]
    aircraft = [(i, regs[i], types[i], locs[i]) for i in range(1, len(regs))]

    slots = {}
    for r in rows[3:]:
        cells = r.select("td")
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        if not TIME_RE.match(label):
            continue
        free = {}
        for i, c in enumerate(cells):
            if i == 0:
                continue
            a = c.find("a")
            style = (c.get("style") or "").replace(" ", "").lower()
            free[i] = bool(
                a is not None
                and a.get("href")
                and "aspnetdisabled" not in (a.get("class") or [])
                and "background-color:white" in style
            )
        slots[label] = free
    return aircraft, slots


def open_slots(soup, start, end, type_pat):
    """[(time, reg, type)] free on 172s (types matching type_pat) in [start, end)."""
    aircraft, slots = parse_grid(soup)
    planes = [a for a in aircraft if re.search(type_pat, a[2])]
    found = []
    for t, free in slots.items():
        if not (start <= t < end):
            continue
        for col, reg, typ, _ in planes:
            if free.get(col):
                found.append((t, reg, typ))
    return planes, found


def slot_labels(start, end):
    """30-minute slot labels covering [start, end), e.g. 10:00..11:30."""
    def mins(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(mins(start), mins(end), 30)]


def open_blocks(soup, start, end, type_pat):
    """Regs of matching aircraft that are free for EVERY slot in [start, end)."""
    aircraft, slots = parse_grid(soup)
    planes = [a for a in aircraft if re.search(type_pat, a[2])]
    labels = slot_labels(start, end)
    found = []
    for col, reg, typ, _ in planes:
        if labels and all(t in slots and slots[t].get(col) for t in labels):
            found.append(reg)
    return planes, found


def send_email(subject, body):
    """Send via SMTP (Gmail by default). Returns True on success."""
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        print("email not configured (need SMTP_USER, SMTP_PASS, EMAIL_TO in .env)", file=sys.stderr)
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                              int(os.environ.get("SMTP_PORT", "465")), timeout=30) as smtp:
            smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        print(f"email failed: {e}", file=sys.stderr)
        return False


def notify(msg):
    print("\a*** " + msg, flush=True)
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 300)
    except Exception:
        pass
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
                          headers={"Title": "Squadron 2 slot open", "Priority": "high"}, timeout=10)
        except requests.RequestException as e:
            print("ntfy failed:", e)


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_DEFAULTS = {"date": "2026-09-20", "start": "10:00", "end": "11:00",
                   "interval_seconds": 60, "aircraft_type": "172"}


def load_config(path):
    """config.json merged over the defaults."""
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        print(f"{path} not found, using built-in defaults")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")
    return cfg


def validate(args):
    """Fail fast on values that would otherwise silently never match."""
    datetime.strptime(args.date, "%Y-%m-%d")
    for name in ("start", "end"):
        val = getattr(args, name)
        if not re.fullmatch(r"\d{2}:(00|30)", val) or int(val[:2]) > 24:
            raise ValueError(f"{name} '{val}' must be HH:00 or HH:30 (24-hour, e.g. 13:30)")
    if not slot_labels(args.start, args.end):
        raise ValueError(f"start {args.start} must be earlier than end {args.end}")
    if args.interval < 10:
        raise ValueError("interval_seconds must be at least 10 (be polite to the server)")
    re.compile(args.type)


def main():
    ap = argparse.ArgumentParser(description="Settings come from config.json; flags override it.")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--start", help="start of the block you want (HH:MM)")
    ap.add_argument("--end", help="end of the block; plane must be free for every slot before this")
    ap.add_argument("--type", help="regex matched against the aircraft Type row")
    ap.add_argument("--interval", type=int, help="seconds between checks")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = ap.parse_args()

    if args.test_email:
        ok = send_email("squadron-watch test", "If you can read this, email alerts work.")
        print("sent" if ok else "FAILED")
        return

    cfg = load_config(args.config)
    args.date = args.date or cfg["date"]
    args.start = args.start or cfg["start"]
    args.end = args.end or cfg["end"]
    args.type = args.type or cfg["aircraft_type"]
    args.interval = args.interval or cfg["interval_seconds"]
    try:
        validate(args)
    except ValueError as e:
        raise SystemExit(f"Bad setting: {e}")

    s, _ = login()
    known = None  # regs with the full block free at last poll
    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            soup = fetch_day(s, args.date)
            planes, found = open_blocks(soup, args.start, args.end, args.type)
            now = set(found)
            if args.once:
                print(f"{len(planes)} matching aircraft: " + ", ".join(f"{r}({t})" for _, r, t, _ in planes))
                print(f"free {args.start}-{args.end} on: {sorted(now) or 'none'}")
                return
            new = now - (known or set())
            if known is None:
                print(f"[{stamp}] watching {len(planes)} aircraft on {args.date} for a full "
                      f"{args.start}-{args.end} block; free right now: {sorted(now) or 'none'}", flush=True)
                new = now  # already-open blocks are worth an email too
            if new:
                msg = f"{', '.join(sorted(new))} free {args.start}-{args.end} on {args.date}"
                notify(msg)
                body = "\n".join([msg, "", f"Book it: {SCHED_URL}",
                                  f"(found {datetime.now():%Y-%m-%d %H:%M:%S})"])
                if not send_email(f"Cessna 172 open {args.start}-{args.end} on {args.date}", body):
                    now -= new  # email failed: treat as not-yet-seen so we retry next poll
            elif known is not None:
                print(f"[{stamp}] no change", flush=True)
            known = now
        except PermissionError:
            print(f"[{stamp}] session expired, logging in again", flush=True)
            s, _ = login()
            continue
        except (requests.RequestException, RuntimeError) as e:
            print(f"[{stamp}] error: {e}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
