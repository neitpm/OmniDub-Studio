#!/usr/bin/env python3
"""Kiem _static_overlay: no bat duoc nhung cho nao, co an nham noi dung khong.
3 cot = goc | hop phat hien | sau khi xoa."""
import sys

import cv2
import numpy as np

sys.path.insert(0, "/app")
from main import _static_overlay, erase_text

VID = "/shared_data/audio/downloaded_video_cut.mp4"
TGT = (896, 512)
W, H = TGT

cap = cv2.VideoCapture(VID)
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
fps = cap.get(cv2.CAP_PROP_FPS)
cap.release()
print(f"nguon: {total} frame {fps:.2f} fps = {total / fps / 60:.1f} phut")

m = _static_overlay(VID, total, TGT)
if m is None:
    print("MASK = None (khong bat duoc gi)")
    sys.exit(1)
print(f"mask: {int((m > 0).sum())} diem = {(m > 0).mean() * 100:.2f}% khung")
cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
print(f"{len(cnts)} vung:")
for c in sorted(cnts, key=cv2.boundingRect):
    x, y, w, h = cv2.boundingRect(c)
    side = "TRAI" if x + w / 2 < W / 2 else "PHAI"
    band = "tren" if y + h / 2 < H / 2 else "duoi"
    print(f"  {side:>5} {band:<5} x {x / W * 100:5.1f}-{(x + w) / W * 100:5.1f}%  "
          f"y {y / H * 100:5.1f}-{(y + h) / H * 100:5.1f}%  ({w}x{h})")

for t in (20, 300, 660):
    cap = cv2.VideoCapture(VID)
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, fr = cap.read()
    cap.release()
    if not ok:
        continue
    f = cv2.resize(fr, TGT, interpolation=cv2.INTER_AREA)
    mk = f.copy()
    cv2.drawContours(mk, cnts, -1, (0, 255, 0), 2)
    e, mm = erase_text(f, extra=m, return_mask=True)
    cv2.imwrite(f"/shared_data/preview/OV_{t}.jpg", np.hstack([f, mk, e]))
    print(f"  OV_{t}.jpg | mask dung thuc te {int((mm > 0).sum())} px = "
          f"{(mm > 0).mean() * 100:.2f}% khung | sang boc {f.mean():.0f} -> {e.mean():.0f}")
