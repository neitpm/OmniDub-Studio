#!/usr/bin/env python3
"""Kiem nhanh: tu trong container gateway, hh3d co tra duoc player khong.
Chi GET 1 lan, khong retry (retry nhieu lan bi chan anti-bot - da hoc bay
gio truoc day)."""
import re
import urllib.request as u

URL = "https://www.hh3d.com/muc-than-ky/thuyet-minh/tap-106"
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120 Safari/537.36",
       "Referer": "https://www.hh3d.com/"}
try:
    p = u.urlopen(u.Request(URL, headers=HDR), timeout=30)
    h = p.read(600000).decode("utf-8", "ignore")
    print("HTTP", p.status, "| doc duoc", len(h), "ky tu")
    for pat in (r'https://[^"\'\s]+\.m3u8[^"\'\s]*', r'player[_-]?json', r'cid["\':= ]+\d+'):
        m = re.findall(pat, h, re.I)
        print(f"  {pat:<34} -> {len(m)} lan", (m[0][:110] if m else ""))
    print("  co chu 'login'/'dang nhap':", bool(re.search(r"dang ?nhap|login", h, re.I)))
except Exception as e:
    print("LOI", type(e).__name__, e)
