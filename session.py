"""Login helper for the Paperless141 scheduler (ASP.NET WebForms)."""
import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE = "https://scheduler.squadron2.com/"
LOGIN_URL = BASE + "FCMS1.aspx"

load_dotenv()


def form_fields(soup):
    """All hidden/text inputs of the page's form, incl. __VIEWSTATE etc."""
    data = {}
    for inp in soup.select("input[name]"):
        t = (inp.get("type") or "text").lower()
        if t in ("submit", "image", "button", "checkbox"):
            continue
        data[inp["name"]] = inp.get("value", "")
    return data


def login():
    user, pw = os.environ.get("SCHED_USER"), os.environ.get("SCHED_PASS")
    if not user or not pw:
        raise SystemExit("Set SCHED_USER and SCHED_PASS in .env")

    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) squadron-watch"

    r = s.get(LOGIN_URL, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    # Cookie-consent "Accept" button, if it's showing
    if soup.find("input", {"name": "BtnAgree"}):
        data = form_fields(soup)
        data["BtnAgree"] = "Accept"
        r = s.post(LOGIN_URL, data=data, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")

    data = form_fields(soup)
    data.update({"txtUserName": user, "txtPassword": pw})
    # Guess at the submit button name; the real one is confirmed on first run.
    btn = soup.find("input", {"type": "submit", "value": lambda v: v and "log" in v.lower()})
    if btn is not None:
        data[btn["name"]] = btn["value"]
    r = s.post(LOGIN_URL, data=data, timeout=30)
    return s, r


if __name__ == "__main__":
    s, r = login()
    print(r.status_code, r.url)
    open("after_login.html", "w", encoding="utf-8").write(r.text)
    soup = BeautifulSoup(r.text, "html.parser")
    print("title:", soup.title.get_text(strip=True) if soup.title else None)
    print("links:")
    for a in soup.select("a[href]"):
        print("  ", a.get_text(strip=True)[:50], "->", a["href"])
    print("submit buttons:", [(i.get("name"), i.get("value")) for i in soup.select("input[type=submit]")])
