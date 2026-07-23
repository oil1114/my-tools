# -*- coding: utf-8 -*-
"""唯讀診斷：看懂『可預約場地點下去』的訂位流程，但絕不真的下訂。
只讀取 DOM/onclick/script/form，不點擊、不呼叫任何訂位函式。"""
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

    # 用上午時段（一定有可預約的格子）來看點擊機制；不會真的點
    page.goto(slot_url("2026/07/30", 1), wait_until="domcontentloaded", timeout=60000)

    handlers = page.eval_on_selector_all(
        'img[src*="place01"]',
        "els => els.slice(0,3).map(e => e.getAttribute('onclick'))",
    )
    print("=== 可預約格子的 onclick（前 3 個）===")
    for h in handlers:
        print(repr(h))

    html = page.content()
    names = set()
    for h in handlers or []:
        for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", h or ""):
            names.add(m.group(1))
    print("\n=== onclick 內呼叫到的函式名 ===", sorted(names))
    for name in sorted(names):
        m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", html)
        if m:
            print(f"\n=== function {name}() 定義（前 800 字）===")
            print(html[m.start():m.start() + 800])

    forms = page.eval_on_selector_all(
        "form",
        "els => els.map(e => (e.id||'(no id)')+'  action='+(e.getAttribute('action')||'')+'  method='+(e.getAttribute('method')||''))",
    )
    print("\n=== 頁面 form ===")
    for f in forms:
        print(f)

    hints = page.eval_on_selector_all(
        "a, input, button",
        "els => Array.from(new Set(els.map(e => {"
        " const oc=e.getAttribute('onclick')||'';"
        " const v=(e.getAttribute('value')||e.textContent||'').trim().slice(0,20);"
        " if (/確認|送出|預約|確定|下一步|同意|付款|Submit|Step/i.test(oc+' '+v))"
        "   return v+' :: '+oc.slice(0,140);"
        " return null; }).filter(Boolean))).slice(0,30)",
    )
    print("\n=== 確認/送出/付款 相關元素 ===")
    for h in hints:
        print(h)

    b.close()
