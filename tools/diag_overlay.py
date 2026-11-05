#!/usr/bin/env python3
"""Doi chieu 2 vung de chon tieu chi dung:
   A = bit.ly goc TRAI tren (bo sot), B = logo vang goc PHAI duoi (bat duoc).
In thong ke gray / tophat / bao hoa theo thoi gian. Neu overlay ban trong suot
thi gray nhay nen (std lon) nhung tophat (chu net sang hon nen lang ben canh)
van on dinh -> phai bat bang tophat, khong bang gray."""
import cv2
import numpy as np

VID = "/shared_data/audio/downloaded_video_cut.mp4"
TGT = (896, 512)
W, H = TGT
BOX = {"A_bitly_tra": (45, 235, 52, 100), "B_mukshenji": (765, 885, 450, 500),
       "C_bilibili": (775, 885, 25, 65), "D_doi_chung_toc": (240, 340, 40, 130)}

cap = cv2.VideoCapture(VID)
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
idxs = sorted({int(v) for v in np.linspace(0, total - 1, 48)})
k15 = np.ones((15, 15), np.uint8)
acc = {n: {"g": [], "t": [], "s": []} for n in BOX}
for i in idxs:
    cap.set(cv2.CAP_PROP_POS_FRAMES, i)
    ok, fr = cap.read()
    if not ok:
        continue
    f = cv2.resize(fr, TGT, interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    th = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, k15)
    sat = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)[:, :, 1]
    for n, (x1, x2, y1, y2) in BOX.items():
        acc[n]["g"].append(g[y1:y2, x1:x2])
        acc[n]["t"].append(th[y1:y2, x1:x2])
        acc[n]["s"].append(sat[y1:y2, x1:x2])
cap.release()

for n, d in acc.items():
    G = np.stack(d["g"]).astype(np.int16)
    T = np.stack(d["t"]).astype(np.int16)
    S = np.stack(d["s"]).astype(np.int16)
    p75 = np.percentile(T, 75, axis=0)
    p25 = np.percentile(T, 25, axis=0)
    s75 = np.percentile(S, 75, axis=0)
    print(f"\n{n}: {G.shape[1]}x{G.shape[2]} x {len(G)} frame")
    print(f"  p75(th)>26 : {float((p75 > 26).mean()) * 100:5.1f}%   "
          f"p25(th)>11 : {float((p25 > 11).mean()) * 100:5.1f}%")
    print(f"  sat75>60   : {float((s75 > 60).mean()) * 100:5.1f}%   "
          f"p75>45     : {float((p75 > 45).mean()) * 100:5.1f}%")
    print(f"  AND 3 dieu kien (mask that): "
          f"{float(((p75 > 26) & (p25 > 11) & ((s75 > 60) | (p75 > 45))).mean()) * 100:5.1f}%")
    for name, A in (("gray", G), ("tophat", T), ("sat", S)):
        p25 = np.percentile(A, 25, axis=0)
        p75 = np.percentile(A, 75, axis=0)
        print(f"  {name:<7} median {np.median(A, 0).mean():6.1f} | "
              f"std theo thoi gian {A.std(0).mean():6.1f} | "
              f"p75-p25 {np.mean(p75 - p25):6.1f} | "
              f"p75 {p75.mean():6.1f} | %px p75>25 {float((p75 > 25).mean()) * 100:5.1f}")
