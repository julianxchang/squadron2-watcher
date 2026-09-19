"""Squadron Watch: local web UI + background poller supporting multiple watches.

    python app.py            # then open http://127.0.0.1:8765
    python app.py --port 9000 --no-browser

Watches live in watches.json. Poll interval and aircraft type come from config.json.
Email/login secrets come from .env (see watcher.py's docstring).
"""
import argparse
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from session import login
from watcher import CONFIG_PATH, load_config, notify, parse_grid, send_email, fetch_day, slot_labels, SCHED_URL

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHES_PATH = os.path.join(HERE, "watches.json")
INDEX_PATH = os.path.join(HERE, "static", "index.html")
TIME_RE = re.compile(r"\d{2}:(00|30)")


def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def check_watch(d):
    """Validate a watch payload from the UI; returns the cleaned dict or raises ValueError."""
    date, start, end = str(d.get("date", "")), str(d.get("start", "")), str(d.get("end", ""))
    datetime.strptime(date, "%Y-%m-%d")
    for name, val in (("start", start), ("end", end)):
        if not TIME_RE.fullmatch(val) or minutes(val) > 24 * 60:
            raise ValueError(f"{name} must be HH:00 or HH:30")
    if not slot_labels(start, end):
        raise ValueError("start must be earlier than end")
    tails = d.get("tails")
    if (not isinstance(tails, list) or not tails
            or not all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9]{1,10}", t) for t in tails)):
        raise ValueError("pick at least one aircraft")
    return {"date": date, "start": start, "end": end, "tails": sorted(set(tails))}


def is_expired(w):
    ends = datetime.strptime(w["date"], "%Y-%m-%d") + timedelta(minutes=minutes(w["end"]))
    return datetime.now() > ends


class Store:
    """watches.json, guarded by a lock."""

    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        try:
            with open(path, encoding="utf-8") as f:
                self.watches = json.load(f)["watches"]
        except FileNotFoundError:
            self.watches = []

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"watches": self.watches}, f, indent=2)
        os.replace(tmp, self.path)

    def add(self, w):
        with self.lock:
            w = {"id": uuid.uuid4().hex[:8], "enabled": True, **w}
            self.watches.append(w)
            self.save()
            return w

    def remove(self, wid):
        with self.lock:
            n = len(self.watches)
            self.watches = [w for w in self.watches if w["id"] != wid]
            self.save()
            return len(self.watches) != n

    def toggle(self, wid):
        with self.lock:
            for w in self.watches:
                if w["id"] == wid:
                    w["enabled"] = not w["enabled"]
                    self.save()
                    return True
            return False


class Poller(threading.Thread):
    def __init__(self, store, interval, type_pat):
        super().__init__(daemon=True)
        self.store, self.interval, self.type_pat = store, interval, type_pat
        self.state = {}          # watch id -> {known, free, checked, last_alert}
        self.aircraft = []       # [{"reg","type"}] matching type_pat, from the last poll
        self.last_poll = None
        self.error = None
        self.log = deque(maxlen=40)
        self.wake = threading.Event()
        self.session = None

    def event(self, text):
        self.log.appendleft({"t": datetime.now().strftime("%m/%d %H:%M:%S"), "text": text})

    def reset(self, wid):
        self.state.pop(wid, None)

    def _login(self):
        try:
            self.session, _ = login()
        except SystemExit as e:  # login() exits when .env has no credentials
            raise RuntimeError(str(e) or "missing SCHED_USER/SCHED_PASS in .env")
        self.event("Logged in to scheduler")

    def _fetch(self, day):
        if self.session is None:
            self._login()
        try:
            return fetch_day(self.session, day)
        except PermissionError:
            self._login()
            try:
                return fetch_day(self.session, day)
            except PermissionError:
                raise RuntimeError("scheduler rejected the login; check SCHED_USER/SCHED_PASS in .env")

    def run(self):
        while True:
            try:
                self.poll()
                self.error = None
            except (requests.RequestException, RuntimeError) as e:
                self.error = str(e)
                self.event(f"Error: {e}")
            except Exception as e:  # keep the thread alive no matter what
                self.error = f"unexpected {type(e).__name__}: {e}"
                self.event(self.error)
            self.wake.wait(self.interval)
            self.wake.clear()

    def poll(self):
        with self.store.lock:
            active = [dict(w) for w in self.store.watches if w["enabled"] and not is_expired(w)]
            live_ids = {w["id"] for w in self.store.watches}
        for wid in list(self.state):
            if wid not in live_ids:
                del self.state[wid]

        # One page load per distinct date; with no active watches, still load today for the aircraft list.
        dates = sorted({w["date"] for w in active}) or [datetime.now().strftime("%Y-%m-%d")]
        grids = {d: parse_grid(self._fetch(d)) for d in dates}
        self.last_poll = datetime.now().strftime("%H:%M:%S")

        aircraft, _ = grids[dates[0]]
        self.aircraft = [{"reg": reg, "type": typ} for _, reg, typ, _ in aircraft
                         if re.search(self.type_pat, typ)]

        for w in active:
            aircraft, slots = grids[w["date"]]
            labels = slot_labels(w["start"], w["end"])
            now = {reg for col, reg, _, _ in aircraft
                   if reg in w["tails"] and all(t in slots and slots[t].get(col) for t in labels)}
            st = self.state.setdefault(w["id"], {"known": None, "free": [], "checked": None, "last_alert": None})
            new = now - (st["known"] or set())
            if new:
                now = self.alert(w, st, now, new)
            st.update(known=now, free=sorted(now), checked=self.last_poll)

    def alert(self, w, st, now, new):
        msg = f"{', '.join(sorted(new))} free {w['start']}-{w['end']} on {w['date']}"
        notify(msg)
        body = "\n".join([msg, "", f"Book it: {SCHED_URL}", f"(found {datetime.now():%Y-%m-%d %H:%M:%S})"])
        configured = all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
        sent = send_email(f"Cessna 172 open {w['start']}-{w['end']} on {w['date']}", body)
        st["last_alert"] = datetime.now().strftime("%m/%d %H:%M:%S")
        if sent:
            self.event(f"OPEN: {msg} (email sent)")
        elif configured:
            self.event(f"OPEN: {msg} (email FAILED, will retry)")
            return now - new  # not marked as seen, so the next poll retries
        else:
            self.event(f"OPEN: {msg} (email not configured)")
        return now

    def snapshot(self):
        with self.store.lock:
            watches = [dict(w) for w in self.store.watches]
        for w in watches:
            w["expired"] = is_expired(w)
            st = self.state.get(w["id"], {})
            w["free"], w["checked"], w["last_alert"] = st.get("free", []), st.get("checked"), st.get("last_alert")
        watches.sort(key=lambda w: (w["date"], w["start"]))
        return {
            "watches": watches,
            "aircraft": self.aircraft,
            "last_poll": self.last_poll,
            "interval": self.interval,
            "error": self.error,
            "email_configured": all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO")),
            "email_to": os.environ.get("EMAIL_TO", ""),
            "log": list(self.log),
        }


def make_handler(store, poller):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, mutating):
            # Localhost only, and JSON-only writes: blocks DNS-rebinding and cross-site form posts.
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "bad host"})
                return False
            if mutating and not self.headers.get("Content-Type", "").startswith("application/json"):
                self._send(415, {"error": "JSON only"})
                return False
            return True

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._guard(False):
                return
            if self.path == "/api/state":
                self._send(200, poller.snapshot())
            elif self.path in ("/", "/index.html"):
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard(True):
                return
            try:
                if self.path == "/api/watches":
                    w = store.add(check_watch(self._json_body()))
                    poller.wake.set()
                    return self._send(201, w)
                if self.path == "/api/poll":
                    poller.wake.set()
                    return self._send(200, {"ok": True})
                if self.path == "/api/test-email":
                    ok = send_email("squadron-watch test", "If you can read this, email alerts work.")
                    poller.event("Test email sent" if ok else "Test email FAILED (see console)")
                    return self._send(200 if ok else 502, {"ok": ok})
                m = re.fullmatch(r"/api/watches/(\w+)/toggle", self.path)
                if m:
                    ok = store.toggle(m.group(1))
                    poller.reset(m.group(1))
                    poller.wake.set()
                    return self._send(200 if ok else 404, {"ok": ok})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._guard(True):
                return
            m = re.fullmatch(r"/api/watches/(\w+)", self.path)
            if m and store.remove(m.group(1)):
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_config(CONFIG_PATH)
    interval = max(10, int(cfg["interval_seconds"]))
    store = Store(WATCHES_PATH)
    poller = Poller(store, interval, cfg["aircraft_type"])
    poller.start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, poller))
    url = f"http://127.0.0.1:{args.port}"
    print(f"Squadron Watch running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
