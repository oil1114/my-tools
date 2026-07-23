# -*- coding: utf-8 -*-
"""
場地釋出監控。

每次執行會：
  1. 用你的帳號登入線上訂場系統
  2. 掃描滾動開放的日期，找出「可預約」的場地
  3. 跟上一次的結果比對，只有在「新出現」可預約場地時，才透過 Telegram 推播到你手機
狀態存在 state.json，由 GitHub Actions 幫忙保存。
"""
import os
import re
import sys
import json
import datetime
import urllib.parse
import urllib.request

BASE = "https://fe.xuanen.com.tw"
PT = "1"  # 場地類別代碼
LOGIN_URL = f"{BASE}/fe02.aspx?module=login_page&files=login"
CAL_URL = f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&PT={PT}"


def slot_url(d):
    # d 形如 '2026/07/28'
    return f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&StepFlag=2&PT={PT}&D={d}&D2=1"


# ---- 設定（從環境變數讀，GitHub Secrets / Variables 提供）----
ACCOUNT = os.environ.get("FE_ACCOUNT", "")
PASSWORD = os.environ.get("FE_PASSWORD", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# 只想盯特定星期？填數字，用逗號分隔（週一=1 ... 週日=7），留空=全部
# 例如只要週六日： WATCH_DOWS = "6,7"
WATCH_DOWS = os.environ.get("WATCH_DOWS", "").strip()
# 只想盯特定時段？（24 小時制，含起、不含迄）留預設=全部
WATCH_START_HOUR = int(os.environ.get("WATCH_START_HOUR") or "0")
WATCH_END_HOUR = int(os.environ.get("WATCH_END_HOUR") or "24")

STATE_FILE = "state.json"

if not (ACCOUNT and PASSWORD and TG_BOT_TOKEN and TG_CHAT_ID):
    sys.exit("缺少設定：請確認 FE_ACCOUNT / FE_PASSWORD / TG_BOT_TOKEN / TG_CHAT_ID 都已設定。")


def target_dates():
    """滾動開放約 8 天：今天到今天+7。跨月也沒問題。"""
    today = datetime.date.today()
    return [(today + datetime.timedelta(days=i)).strftime("%Y/%m/%d") for i in range(8)]


def goto(page, url, tries=3):
    """載入頁面；遇到逾時就重試幾次，對付偶發的連線不穩／限流。"""
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception:
            if i == tries - 1:
                raise
            page.wait_for_timeout(4000)


def scrape():
    """登入並回傳所有可預約場地清單 [{date,time,court}, ...]。"""
    from playwright.sync_api import sync_playwright

    available = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        page = browser.new_context(locale="zh-TW", user_agent=ua).new_page()

        # --- 登入 ---
        goto(page, LOGIN_URL)
        page.fill("#ContentPlaceHolder1_loginid", ACCOUNT)
        page.fill("#loginpw", PASSWORD)
        # 登入鈕是背景圖按鈕，headless 下常被判定為不可見；直接觸發它的 onclick
        page.eval_on_selector(
            "#login_but",
            "el => (typeof DoSubmit === 'function' ? DoSubmit() : el.click())",
        )
        page.wait_for_load_state("networkidle", timeout=60000)

        # --- 確認登入成功 ---
        goto(page, CAL_URL)
        html = page.content()
        if 'id="loginpw"' in html or 'name="loginpw"' in html:
            browser.close()
            sys.exit("登入失敗，請檢查 FE_ACCOUNT / FE_PASSWORD 是否正確。")

        # 找出目前開放（可點選）的日期
        open_dates = page.eval_on_selector_all(
            'img[src*="NewDataSelect"]',
            "els => els.map(e => { const m=(e.getAttribute('onclick')||'')"
            ".match(/GoToStep2\\('([^']+)'/); return m?m[1]:null; }).filter(Boolean)",
        )
        # 與計算出的滾動視窗取聯集，較保險
        dates = sorted(set(open_dates) | set(target_dates()))

        for d in dates:
            goto(page, slot_url(d))
            # place01.png = 可預約；抓出 confirm 文字裡「場地 時間」
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

        browser.close()
    return available


def in_window(slot):
    y, m, dd = (int(x) for x in slot["date"].split("/"))
    dow = datetime.date(y, m, dd).weekday() + 1  # 週一=1 ... 週日=7
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
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # None = 第一次跑，沒有基準


def save_state(keys):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f, ensure_ascii=False, indent=0)


def push(msg):
    # 透過 Telegram Bot 送訊息，只會進到你自己的聊天室
    data = urllib.parse.urlencode(
        {"chat_id": TG_CHAT_ID, "text": msg, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


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
            lines.append(f"{head} 新開放 {len(items)} 個場地 {span}")
        else:
            for i in sorted(items, key=lambda x: (x["time"], x["court"])):
                lines.append(f"{head} {i['time']} {i['court']}")
    return "有新場地釋出！\n" + "\n".join(lines) + "\n\n訂場：https://fe.xuanen.com.tw/fe02.aspx"


def main():
    avail = [s for s in scrape() if in_window(s)]
    cur = {key(s): s for s in avail}
    cur_keys = set(cur)

    prev = load_prev()
    save_state(cur_keys)

    if prev is None:
        print(f"首次執行，建立基準（目前可預約 {len(cur_keys)} 個），不推播。")
        return

    new_keys = cur_keys - prev
    if not new_keys:
        print(f"沒有新釋出。目前可預約 {len(cur_keys)} 個。")
        return

    new_slots = [cur[k] for k in new_keys]
    msg = format_message(new_slots)
    print("偵測到新釋出，推播：\n" + msg)
    push(msg)


if __name__ == "__main__":
    main()
