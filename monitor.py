# -*- coding: utf-8 -*-
import io
import os
import re
import sys
import json
import time
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
ORDERS_URL = f"{BASE}/fe02.aspx?Module=member&files=orderx_mt"

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
ORDERS_REPORT = bool(os.environ.get("ORDERS_REPORT", "").strip())
HASH_SALT = os.environ.get("HASH_SALT", "").encode("utf-8")

WATCH_DOWS = os.environ.get("WATCH_DOWS", "").strip()
WATCH_START_HOUR = int(os.environ.get("WATCH_START_HOUR") or "0")
WATCH_END_HOUR = int(os.environ.get("WATCH_END_HOUR") or "24")

STATE_FILE = "state.json"
RESET_FILE = "reset_date.txt"
ALERT_FILE = "alert_date.txt"
OFFSET_FILE = "tg_offset.txt"


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


def scrape(include_orders=False):
    from playwright.sync_api import sync_playwright

    available = []
    orders = []
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

        if include_orders:
            goto(page, ORDERS_URL)
            rows = page.eval_on_selector_all(
                "table tr",
                "els => els.map(tr => Array.from(tr.cells)"
                ".map(td => td.innerText.trim().replace(/\\s+/g, ' ')))"
                ".filter(r => r.length >= 9 && /^\\d{4}\\/\\d{2}\\/\\d{2}$/.test(r[0]))",
            )
            for r in rows:
                if re.match(r"^\d{4}-\d{2}-\d{2}$", r[5] or ""):
                    orders.append(
                        {"date": r[5], "hour": r[6], "court": r[3], "status": r[7]}
                    )
            page.wait_for_timeout(500)
            goto(page, CAL_URL)

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
    return available, orders


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


def all_chat_ids():
    return [c.strip() for c in TG_CHAT_ID.split(",") if c.strip()]


def push(msg, chat_ids=None):
    if chat_ids is None:
        chat_ids = all_chat_ids()
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


def load_offset():
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return None


def save_offset(v):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        f.write(str(v))


def poll_commands():
    # consume pending bot messages; return chat ids that asked for the order list
    offset = load_offset()
    params = {"timeout": "0", "allowed_updates": '["message"]'}
    if offset:
        params["offset"] = str(offset)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            updates = json.load(r).get("result", [])
    except Exception as e:
        print(f"getUpdates failed: {e}")
        return set()
    if not updates:
        return set()
    save_offset(max(u["update_id"] for u in updates) + 1)
    allowed = set(all_chat_ids())
    fresh_after = time.time() - 45 * 60
    asked = set()
    for u in updates:
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        text = (m.get("text") or "").strip().lower()
        if chat not in allowed:
            continue
        # no stored offset yet: skip stale backlog so old messages don't fire
        if offset is None and m.get("date", 0) < fresh_after:
            continue
        if text.startswith("/order") or text == "訂單":
            asked.add(chat)
    return asked


def in_report_window(o):
    try:
        y, m, d = (int(x) for x in o["date"].split("-"))
        day = datetime.date(y, m, d)
    except ValueError:
        return None
    today = tw_now().date()
    return day if today <= day <= today + datetime.timedelta(days=8) else None


def render_orders_png(orders):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    font_sets = [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", None),
        (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"),
    ]
    reg = bold = None
    for r_, b_ in font_sets:
        if os.path.exists(r_):
            reg = r_
            bold = b_ if b_ and os.path.exists(b_) else r_
            break
    if not reg:
        return None
    try:
        S = 2
        SURFACE, GRID, AXIS = "#fcfcfb", "#e1e0d9", "#c3c2b7"
        INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
        GOOD, GOOD_TEXT, GOOD_TINT = "#0ca30c", "#006300", "#d1ecd0"
        CANCEL_TINT, TODAY_WASH, TODAY_INK = "#f0efec", "#f3f7fc", "#2a78d6"

        today = tw_now().date()
        slots = {}
        for o in orders:
            day = in_report_window(o)
            digits = re.sub(r"\D", "", o["hour"] or "")
            if day is None or not digits:
                continue
            slots.setdefault((day, int(digits)), []).append(o)

        last = max([today + datetime.timedelta(days=7)] + [d for d, _ in slots])
        days = [today + datetime.timedelta(days=i) for i in range((last - today).days + 1)]
        hour_lo = max(0, min([18] + [h for _, h in slots]))
        hour_hi = min(24, max([22] + [h + 1 for _, h in slots]))
        n_rows = hour_hi - hour_lo

        def f(size, b=False):
            return ImageFont.truetype(bold if b else reg, size * S)

        def short_court(name):
            m = re.match(r"(\d+F).*?([A-Za-z]?\d+)$", name)
            return f"{m.group(1)} {m.group(2)}" if m else name

        M, W_g, W_c, H_t, H_h, H_r, H_l = 28, 56, 122, 64, 58, 64, 56
        W = M + W_g + len(days) * W_c + M
        H = M + H_t + H_h + n_rows * H_r + H_l + M
        img = Image.new("RGB", (W * S, H * S), SURFACE)
        dr = ImageDraw.Draw(img)

        def rect(x0, y0, x1, y1, **kw):
            dr.rectangle([x0 * S, y0 * S, x1 * S, y1 * S], **kw)

        def line(x0, y0, x1, y1, fill, w=1):
            dr.line([x0 * S, y0 * S, x1 * S, y1 * S], fill=fill, width=w * S // 2 or 1)

        def text(x, y, s, font, fill, anchor="la"):
            dr.text((x * S, y * S), s, font=font, fill=fill, anchor=anchor)

        gx, gy = M + W_g, M + H_t + H_h
        text(M, M + 2, "未來 8 天訂單", f(26, True), INK)
        now = tw_now()
        rng = f"{days[0].month}/{days[0].day} – {days[-1].month}/{days[-1].day}"
        text(W - M, M + 14, f"{rng}　產生於 {now.month}/{now.day} {now:%H:%M}", f(13), MUTED, anchor="ra")

        if today in days:
            ti = days.index(today)
            rect(gx + ti * W_c, M + H_t, gx + (ti + 1) * W_c, gy + n_rows * H_r, fill=TODAY_WASH)

        for i, day in enumerate(days):
            cx = gx + i * W_c + W_c // 2
            is_today = day == today
            text(cx, M + H_t + 8, f"{day.month}/{day.day}", f(16, is_today), INK if is_today else INK2, anchor="ma")
            sub = "今天" if is_today else f"週{DOW_CH[day.weekday()]}"
            text(cx, M + H_t + 32, sub, f(12, is_today), TODAY_INK if is_today else MUTED, anchor="ma")

        for r in range(n_rows + 1):
            y = gy + r * H_r
            text(gx - 8, y - 7 if r else y, f"{hour_lo + r:02d}", f(11), MUTED, anchor="ra")

        for (day, hour), items in slots.items():
            if day not in days or not (hour_lo <= hour < hour_hi):
                continue
            i, r = days.index(day), hour - hour_lo
            x0, y0 = gx + i * W_c, gy + r * H_r
            x1, y1 = x0 + W_c, y0 + H_r
            paid = [o for o in items if o["status"] == "繳費"]
            show = paid or items
            cancelled = not paid
            rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1, fill=CANCEL_TINT if cancelled else GOOD_TINT)
            if not cancelled:
                rect(x0 + 1, y0 + 1, x0 + 4, y1 - 1, fill=GOOD)
            label = "·".join(sorted({short_court(o["court"]) for o in show}))
            cx, cy = x0 + W_c // 2 + 6, y0 + H_r // 2
            font = f(14, not cancelled)
            text(cx, cy, label, font, MUTED if cancelled else GOOD_TEXT, anchor="mm")
            bb = dr.textbbox((cx * S, cy * S), label, font=font, anchor="mm")
            mx = (bb[0] // S) - 14
            if cancelled:
                line(mx, cy - 4, mx + 8, cy + 4, MUTED, 3)
                line(mx + 8, cy - 4, mx, cy + 4, MUTED, 3)
                dr.line([bb[0] - 2 * S, cy * S, bb[2] + 2 * S, cy * S], fill=MUTED, width=max(S, 2))
            else:
                line(mx, cy, mx + 3, cy + 4, GOOD, 3)
                line(mx + 3, cy + 4, mx + 9, cy - 4, GOOD, 3)

        for r in range(n_rows + 1):
            y = gy + r * H_r
            line(gx, y, gx + len(days) * W_c, y, GRID)
        for i in range(len(days) + 1):
            x = gx + i * W_c
            line(x, gy, x, gy + n_rows * H_r, GRID)
        line(gx, M + H_t, gx + len(days) * W_c, M + H_t, AXIS)
        bot_y = gy + n_rows * H_r
        dr.rectangle([gx * S, (M + H_t) * S, (gx + len(days) * W_c) * S, bot_y * S], outline=AXIS, width=S)

        ly = bot_y + 18
        rect(gx, ly, gx + 26, ly + 16, fill=GOOD_TINT)
        rect(gx, ly, gx + 3, ly + 16, fill=GOOD)
        text(gx + 34, ly + 1, "已繳費", f(13), INK2)
        lx2 = gx + 130
        rect(lx2, ly, lx2 + 26, ly + 16, fill=CANCEL_TINT, outline=GRID, width=S)
        text(lx2 + 34, ly + 1, "已取消", f(13), INK2)

        img = img.resize((int(W * 1.2), int(H * 1.2)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"render failed: {e!r}")
        return None


def push_photo(png, caption, chat_ids=None):
    if chat_ids is None:
        chat_ids = all_chat_ids()
    boundary = "----tg" + hashlib.sha256(os.urandom(8)).hexdigest()[:16]
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    ok = True
    for chat_id in chat_ids:
        parts = []
        for k, v in (("chat_id", chat_id), ("caption", caption), ("parse_mode", "HTML")):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
            )
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
             'filename="orders.png"\r\nContent-Type: image/png\r\n\r\n').encode("utf-8")
        )
        body = b"".join(parts) + png + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        except Exception as e:
            print(f"photo push to {chat_id} failed: {e}")
            ok = False
    return ok


STATUS_MARK = {"繳費": "✅ 已繳費", "取消": "❌ 已取消", "退費": "↩️ 已退費"}


def format_orders(orders):
    today = tw_now().date()
    end = today + datetime.timedelta(days=8)
    rows = []
    for o in orders:
        try:
            y, m, d = (int(x) for x in o["date"].split("-"))
            day = datetime.date(y, m, d)
        except ValueError:
            continue
        if today <= day <= end:
            rows.append((day, o))
    head = f"📋 未來 8 天訂單（{today.month}/{today.day}～{end.month}/{end.day}）"
    if not rows:
        return head + "\n目前沒有任何訂單。"

    def hour_val(o):
        digits = re.sub(r"\D", "", o["hour"] or "")
        return int(digits) if digits else 0

    lines = [head]
    for day, o in sorted(rows, key=lambda x: (x[0], hour_val(x[1]))):
        h = hour_val(o)
        t = f"{h:02d}:00~{h + 1:02d}:00" if h else o["hour"]
        mark = STATUS_MARK.get(o["status"], o["status"])
        lines.append(
            html.escape(f"{day.month}/{day.day}(週{DOW_CH[day.weekday()]}) {t} {o['court']} {mark}")
        )
    return "\n".join(lines)


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

    ask_chats = poll_commands()
    if ORDERS_REPORT:
        ask_chats = set(all_chat_ids())

    try:
        avail, orders = scrape(include_orders=bool(ask_chats))
        avail = [s for s in avail if in_window(s)]
    except LoginError as e:
        print("login failed: " + str(e))
        if ask_chats:
            push("⚠️ 登入失敗，暫時無法查詢訂單。", ask_chats)
        if not alerted_today(today):
            mark_alerted(today)
            try:
                push("⚠️ 場地監控登入失敗，可能是訂場密碼改了或帳號被鎖。請確認並更新設定，否則暫時收不到場地通知。")
            except Exception:
                pass
        return
    except Exception as e:
        print(f"check failed (likely timeout/rate-limit), skip: {e!r}")
        if ask_chats:
            push("⚠️ 訂單查詢失敗（網站暫時連不上），請稍後再傳一次 /orders。", ask_chats)
        return

    if ask_chats:
        msg = format_orders(orders)
        png = render_orders_png(orders)
        if not (png and push_photo(png, msg, ask_chats)):
            push(msg, ask_chats)

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
