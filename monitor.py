# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import hmac
import html
import hashlib
import datetime
import urllib.parse
import urllib.request

BASE = "https://fe.xuanen.com.tw"
PT = "1"
LOGIN_URL = f"{BASE}/fe02.aspx?module=login_page&files=login"
CAL_URL = f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&PT={PT}"

SESSION_HOURS = {1: (6, 12), 2: (12, 18), 3: (18, 22)}
SESSION_NAME = {1: "上午", 2: "下午", 3: "晚上"}


def session_of(hour):
    for d2, (lo, hi) in SESSION_HOURS.items():
        if lo <= hour < hi:
            return d2
    return 1


def slot_url(d, d2):
    return f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&StepFlag=2&PT={PT}&D={d}&D2={d2}"


def active_sessions():
    out = [d2 for d2, (lo, hi) in SESSION_HOURS.items()
           if lo < WATCH_END_HOUR and hi > WATCH_START_HOUR]
    return out or [1, 2, 3]


ACCOUNT = os.environ.get("FE_ACCOUNT", "")
PASSWORD = os.environ.get("FE_PASSWORD", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
RESET_BASELINE = bool(os.environ.get("RESET_BASELINE", "").strip())
HASH_SALT = os.environ.get("HASH_SALT", "").encode("utf-8")

WATCH_DOWS = os.environ.get("WATCH_DOWS", "").strip()
WATCH_START_HOUR = int(os.environ.get("WATCH_START_HOUR") or "0")
WATCH_END_HOUR = int(os.environ.get("WATCH_END_HOUR") or "24")

STATE_FILE = "state.json"
RESET_FILE = "reset_date.txt"
ALERT_FILE = "alert_date.txt"


class LoginError(Exception):
    pass


if not (ACCOUNT and PASSWORD and TG_BOT_TOKEN and TG_CHAT_ID):
    sys.exit("missing config: FE_ACCOUNT / FE_PASSWORD / TG_BOT_TOKEN / TG_CHAT_ID")


def hkey(k):
    # state.json only stores opaque hashes; raw values never leave the run / Telegram
    if HASH_SALT:
        return hmac.new(HASH_SALT, k.encode("utf-8"), hashlib.sha256).hexdigest()[:20]
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:20]


def tw_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def target_dates():
    today = tw_now().date()
    return [(today + datetime.timedelta(days=i)).strftime("%Y/%m/%d") for i in range(8)]


def goto(page, url, tries=3):
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception:
            if i == tries - 1:
                raise
            page.wait_for_timeout(4000)


def scrape():
    from playwright.sync_api import sync_playwright

    available = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        page = browser.new_context(locale="zh-TW", user_agent=ua).new_page()

        goto(page, LOGIN_URL)
        page.fill("#ContentPlaceHolder1_loginid", ACCOUNT)
        page.fill("#loginpw", PASSWORD)
        page.eval_on_selector(
            "#login_but",
            "el => (typeof DoSubmit === 'function' ? DoSubmit() : el.click())",
        )
        page.wait_for_load_state("networkidle", timeout=60000)

        goto(page, CAL_URL)
        content = page.content()
        if 'id="loginpw"' in content or 'name="loginpw"' in content:
            browser.close()
            raise LoginError("login failed")

        open_dates = page.eval_on_selector_all(
            'img[src*="NewDataSelect"]',
            "els => els.map(e => { const m=(e.getAttribute('onclick')||'')"
            ".match(/GoToStep2\\('([^']+)'/); return m?m[1]:null; }).filter(Boolean)",
        )
        dates = sorted(set(open_dates) | set(target_dates()))

        sessions = active_sessions()
        for d in dates:
            for d2 in sessions:
                goto(page, slot_url(d, d2))
                names = page.eval_on_selector_all(
                    'img[src*="place01"]',
                    "els => els.map(e => { const oc=e.getAttribute('onclick')||'';"
                    " const m=oc.match(/「([^」]+)」/); return m?m[1]:null; }).filter(Boolean)",
                )
                for name in names:
                    tm = re.search(r"(\d{2}:\d{2}~\d{2}:\d{2})", name)
                    time_s = tm.group(1) if tm else ""
                    court = name.replace(time_s, "").strip()
                    available.append({"date": d, "time": time_s, "court": court})
                page.wait_for_timeout(500)

        browser.close()
    return available


def in_window(slot):
    y, m, dd = (int(x) for x in slot["date"].split("/"))
    dow = datetime.date(y, m, dd).weekday() + 1
    if WATCH_DOWS:
        allowed = {int(x) for x in WATCH_DOWS.split(",") if x.strip()}
        if dow not in allowed:
            return False
    if slot["time"]:
        sh = int(slot["time"][:2])
        if not (WATCH_START_HOUR <= sh < WATCH_END_HOUR):
            return False
    return True


def key(s):
    return f'{s["date"]} {s["time"]} {s["court"]}'


def load_prev():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    # old plaintext format -> treat as no baseline so we rebuild as hashes without pushing
    if any((" " in x or "/" in x) for x in data):
        return None
    return data


def save_state(keys):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f, ensure_ascii=False, indent=0)


def last_reset_date():
    try:
        with open(RESET_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def mark_reset(date_str):
    with open(RESET_FILE, "w", encoding="utf-8") as f:
        f.write(date_str)


def alerted_today(today):
    try:
        with open(ALERT_FILE, encoding="utf-8") as f:
            return f.read().strip() == today
    except FileNotFoundError:
        return False


def mark_alerted(today):
    with open(ALERT_FILE, "w", encoding="utf-8") as f:
        f.write(today)


def push(msg):
    chat_ids = [c.strip() for c in TG_CHAT_ID.split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    for chat_id in chat_ids:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except Exception as e:
            print(f"push to {chat_id} failed: {e}")


DOW_CH = "一二三四五六日"


def format_message(new_slots):
    by_date = {}
    for s in new_slots:
        by_date.setdefault(s["date"], []).append(s)
    lines = []
    for d in sorted(by_date):
        items = by_date[d]
        y, mo, da = d.split("/")
        dow = DOW_CH[datetime.date(int(y), int(mo), int(da)).weekday()]
        head = f"{int(mo)}/{int(da)}(週{dow})"
        if len(items) > 8:
            times = sorted({i["time"] for i in items if i["time"]})
            span = f"{times[0].split('~')[0]}～{times[-1].split('~')[1]}" if times else ""
            lines.append(html.escape(f"{head} 新開放 {len(items)} 個場地 {span}"))
        else:
            for i in sorted(items, key=lambda x: (x["time"], x["court"])):
                lines.append(html.escape(f"{head} {i['time']} {i['court']}"))
        sessions = sorted({session_of(int(i["time"][:2])) for i in items if i["time"]})
        for d2 in sessions or [3]:
            url = html.escape(slot_url(d, d2), quote=True)
            label = html.escape(f"開啟 {int(mo)}/{int(da)} {SESSION_NAME[d2]}")
            lines.append(f'👉 <a href="{url}">{label}</a>')
    return html.escape("有新場地釋出！") + "\n" + "\n".join(lines)


def main():
    now = tw_now()
    hm = now.hour * 60 + now.minute
    if hm < 42:
        print(f"TW {now:%H:%M}: quiet window, skip.")
        return
    today = now.strftime("%Y/%m/%d")
    daily_reset = last_reset_date() != today
    reset = RESET_BASELINE or daily_reset

    try:
        avail = [s for s in scrape() if in_window(s)]
    except LoginError as e:
        print("login failed: " + str(e))
        if not alerted_today(today):
            mark_alerted(today)
            try:
                push("⚠️ 場地監控登入失敗，可能是訂場密碼改了或帳號被鎖。請確認並更新設定，否則暫時收不到場地通知。")
            except Exception:
                pass
        return
    except Exception as e:
        print(f"check failed (likely timeout/rate-limit), skip: {e!r}")
        return

    cur = {key(s): s for s in avail}
    hcur = {hkey(k): s for k, s in cur.items()}
    cur_hashes = set(hcur)

    prev = load_prev()
    save_state(cur_hashes)

    if reset:
        if daily_reset:
            mark_reset(today)
        print(f"baseline reset ({len(cur_hashes)} open), no push.")
        return

    if prev is None:
        print(f"first run, baseline set ({len(cur_hashes)} open), no push.")
        return

    new_hashes = cur_hashes - prev
    if not new_hashes:
        print(f"no new. {len(cur_hashes)} open.")
        return

    new_slots = [hcur[h] for h in new_hashes]
    msg = format_message(new_slots)
    print("new detected, pushing.")
    push(msg)


if __name__ == "__main__":
    main()
