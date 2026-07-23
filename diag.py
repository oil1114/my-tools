# -*- coding: utf-8 -*-
"""一次性診斷：確認 D2 參數是不是『時段分頁（上午/下午/晚上）』。"""
import os
import re
from playwright.sync_api import sync_playwright

BASE = "https://fe.xuanen.com.tw"
PT = "1"
LOGIN_URL = f"{BASE}/fe02.aspx?module=login_page&files=login"
A = os.environ["FE_ACCOUNT"]
P = os.environ["FE_PASSWORD"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def slot_url(d, d2):
    return f"{BASE}/fe02.aspx?module=net_booking&files=booking_place&StepFlag=2&PT={PT}&D={d}&D2={d2}"


with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_context(locale="zh-TW", user_agent=UA).new_page()
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.fill("#ContentPlaceHolder1_loginid", A)
    page.fill("#loginpw", P)
    page.eval_on_selector(
        "#login_but",
        "el => (typeof DoSubmit === 'function' ? DoSubmit() : el.click())",
    )
    page.wait_for_load_state("networkidle", timeout=60000)

    d = "2026/07/30"
    for d2 in [1, 2, 3, 4]:
        try:
            page.goto(slot_url(d, d2), wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[D2={d2}] 載入失敗: {e}")
            continue
        html = page.content()
        avail = page.eval_on_selector_all('img[src*="place01"]', "els => els.length")
        times = sorted(set(re.findall(r"\d{2}:\d{2}~\d{2}:\d{2}", html)))
        lo = times[0] if times else "-"
        hi = times[-1] if times else "-"
        print(f"[D2={d2}] 可預約(place01)={avail}  頁面時段數={len(times)}  範圍={lo} ~ {hi}")

    hints = page.eval_on_selector_all(
        "a, input, img, span, div, li",
        "els => Array.from(new Set(els.map(e => {"
        " const oc=(e.getAttribute('onclick')||'')+' '+(e.getAttribute('href')||'');"
        " const t=(e.textContent||'').trim().slice(0,16);"
        " if (/D2=|上午|下午|晚上|夜間|時段/.test(oc+' '+t)) return (t?('['+t+']'):'')+' '+oc.trim();"
        " return null; }).filter(Boolean))).slice(0,40)",
    )
    print("時段切換線索：")
    for h in hints:
        print("  ", h)
    b.close()
