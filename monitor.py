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
import html
import urllib.parse
import urllib.request

BASE = "https://fe.xuanen.com.tw"
PT = "1"  # 場地類別代碼
LOGIN_URL = f"{BASE}/fe02.aspx?module=login_page&files=login"
CAL_URL = f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&PT={PT}"


# 一天分三個時段分頁（D2）：1=上午(06-12) 2=下午(12-18) 3=晚上(18-22)
SESSION_HOURS = {1: (6, 12), 2: (12, 18), 3: (18, 22)}
SESSION_NAME = {1: "上午", 2: "下午", 3: "晚上"}


def session_of(hour):
    for d2, (lo, hi) in SESSION_HOURS.items():
        if lo <= hour < hi:
            return d2
    return 1


def slot_url(d, d2):
    # d 形如 '2026/07/28'；d2 為時段（1 上午 / 2 下午 / 3 晚上）
    return f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&StepFlag=2&PT={PT}&D={d}&D2={d2}"


def active_sessions():
    """只抓和 WATCH 時段有重疊的分頁，省請求；預設全時段=三段全抓。"""
    out = [d2 for d2, (lo, hi) in SESSION_HOURS.items()
           if lo < WATCH_END_HOUR and hi > WATCH_START_HOUR]
    return out or [1, 2, 3]


# ---- 設定（從環境變數讀，GitHub Secrets / Variables 提供）----
ACCOUNT = os.environ.get("FE_ACCOUNT", "")
PASSWORD = os.environ.get("FE_PASSWORD", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# 每天固定時間（00:42）由排程設 RESET_BASELINE=1：只重建基準、不推播，
# 把「00:00 開放 + 00:40 未付款釋出」後的狀態當成當天的起點。
RESET_BASELINE = bool(os.environ.get("RESET_BASELINE", "").strip())

# 只想盯特定星期？填數字，用逗號分隔（週一=1 ... 週日=7），留空=全部
# 例如只要週六日： WATCH_DOWS = "6,7"
WATCH_DOWS = os.environ.get("WATCH_DOWS", "").strip()
# 只想盯特定時段？（24 小時制，含起、不含迄）留預設=全部
WATCH_START_HOUR = int(os.environ.get("WATCH_START_HOUR") or "0")
WATCH_END_HOUR = int(os.environ.get("WATCH_END_HOUR") or "24")

STATE_FILE = "state.json"
RESET_FILE = "reset_date.txt"  # 「今天已重設」的燈號：存最後一次重設的台灣日期
ALERT_FILE = "alert_date.txt"  # 「今天已警報過」燈號：登入失敗通知每天最多一次，避免洗版


class LoginError(Exception):
    """登入失敗（帳密錯/被鎖），跟一般連線逾時分開處理。"""

if not (ACCOUNT and PASSWORD and TG_BOT_TOKEN and TG_CHAT_ID):
    sys.exit("缺少設定：請確認 FE_ACCOUNT / FE_PASSWORD / TG_BOT_TOKEN / TG_CHAT_ID 都已設定。")


def tw_now():
    """台灣時間（固定 UTC+8，無日光節約），不依賴系統時區設定。"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def target_dates():
    """滾動開放約 8 天：今天到今天+7。跨月也沒問題。"""
    today = tw_now().date()
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
            raise LoginError("登入失敗（帳號密碼可能改了或帳號被鎖）。")

        # 找出目前開放（可點選）的日期
        open_dates = page.eval_on_selector_all(
            'img[src*="NewDataSelect"]',
            "els => els.map(e => { const m=(e.getAttribute('onclick')||'')"
            ".match(/GoToStep2\\('([^']+)'/); return m?m[1]:null; }).filter(Boolean)",
        )
        # 與計算出的滾動視窗取聯集，較保險
        dates = sorted(set(open_dates) | set(target_dates()))

        sessions = active_sessions()
        for d in dates:
            for d2 in sessions:
                goto(page, slot_url(d, d2))
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
                page.wait_for_timeout(500)  # 稍微放慢，別對網站太密集

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


def last_reset_date():
    """讀「今天已重設」燈號檔，回傳最後重設的日期字串（沒有就回空字串）。"""
    try:
        with open(RESET_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def mark_reset(date_str):
    """點亮燈號：把今天日期寫進燈號檔。"""
    with open(RESET_FILE, "w", encoding="utf-8") as f:
        f.write(date_str)


def alerted_today(today):
    """今天是否已經發過登入失敗警報（避免每 6 分鐘洗版）。"""
    try:
        with open(ALERT_FILE, encoding="utf-8") as f:
            return f.read().strip() == today
    except FileNotFoundError:
        return False


def mark_alerted(today):
    with open(ALERT_FILE, "w", encoding="utf-8") as f:
        f.write(today)


def push(msg):
    # 透過 Telegram Bot 送訊息。TG_CHAT_ID 可用逗號分隔多個人，會分別寄給每一位。
    # 用 HTML 模式，讓長網址藏在可點的短文字後面。
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
            print(f"推播到 {chat_id} 失敗：{e}")


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
        # 直達連結：把長網址藏在可點的短文字後面（Telegram HTML <a>）
        sessions = sorted({session_of(int(i["time"][:2])) for i in items if i["time"]})
        for d2 in sessions or [3]:
            url = html.escape(slot_url(d, d2), quote=True)
            label = html.escape(f"開啟 {int(mo)}/{int(da)} {SESSION_NAME[d2]}")
            lines.append(f'👉 <a href="{url}">{label}</a>')
    return html.escape("有新場地釋出！") + "\n" + "\n".join(lines)


def main():
    # 依台灣時間決定這次要做什麼（改用「自我接力」後，排程判斷放在程式裡）
    now = tw_now()
    hm = now.hour * 60 + now.minute
    # 台灣 00:00–00:42：安靜時段（開放搶＋未付款釋出，你自己現場搶），不查不通知
    if hm < 42:
        print(f"台灣 {now:%H:%M}：安靜時段（00:00–00:42），本次不查。")
        return
    # 燈號：今天（00:42 後）還沒重設過 → 這棒就是當天第一棒 → 重設
    today = now.strftime("%Y/%m/%d")
    daily_reset = last_reset_date() != today
    reset = RESET_BASELINE or daily_reset

    try:
        avail = [s for s in scrape() if in_window(s)]
    except LoginError as e:
        # 真的登入不進去（多半是改了密碼）：發一次 Telegram 警報，然後正常結束（不讓這棒算失敗）
        print("登入失敗：" + str(e))
        if not alerted_today(today):
            mark_alerted(today)
            try:
                push("⚠️ 場地監控登入失敗，可能是訂場密碼改了或帳號被鎖。請確認並更新設定，否則暫時收不到場地通知。")
            except Exception:
                pass
        return
    except Exception as e:
        # 偶發的連線逾時／限流等暫時性錯誤：安靜略過，下一棒再試（不讓這棒算失敗、不寄失敗信）
        print(f"本次檢查失敗（多半是連線逾時或被限流），略過，下一棒再試：{e!r}")
        return

    cur = {key(s): s for s in avail}
    cur_keys = set(cur)

    prev = load_prev()
    save_state(cur_keys)

    if reset:
        if daily_reset:
            mark_reset(today)  # 點亮燈號：今天已重設
        print(f"每日重設基準（目前可預約 {len(cur_keys)} 個），不推播。")
        return

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
