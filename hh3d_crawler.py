import urllib.request, re, base64, json, os, subprocess
from pathlib import Path

def crawl_hh3d_video(url: str, out_mp4_path: Path, max_segments: int = 15) -> Path:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    # 1. Fetch main page
    req = urllib.request.Request(url, headers=headers)
    html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
    
    # 2. Extract player wrapper URL
    m = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
    if not m:
        raise ValueError("Không tìm thấy link player trên trang HH3D.")
    
    player_url = m[0]
    headers["Referer"] = url
    headers["Origin"] = "https://www.hh3d.com"
    req_player = urllib.request.Request(player_url, headers=headers)
    player_html = urllib.request.urlopen(req_player, timeout=12).read().decode("utf-8", errors="ignore")
    
    # 3. Decode base64 obfuscated token
    m_obf = re.search(r'data-obf="([^"]+)"', player_html)
    if not m_obf:
        raise ValueError("Không tìm thấy token video mã hóa trên server HH3D.")
    
    token = m_obf.group(1)
    token += "=" * (-len(token) % 4)
    info = json.loads(base64.b64decode(token))
    stream_m3u8 = info.get("pU") or info.get("sU")
    
    # 4. Fetch real stream m3u8 playlist
    headers["Referer"] = player_url
    req_m3u8 = urllib.request.Request(stream_m3u8, headers=headers)
    m3u8_text = urllib.request.urlopen(req_m3u8, timeout=12).read().decode("utf-8", errors="ignore")
    
    seg_urls = [line.strip() for line in m3u8_text.split("\n") if line.strip().startswith("http")]
    if not seg_urls:
        raise ValueError("Không tìm thấy segment video trong playlist.")

    raw_ts = out_mp4_path.with_suffix(".ts")
    with open(raw_ts, "wb") as out_f:
        for u in seg_urls[:max_segments]:
            req_seg = urllib.request.Request(u, headers=headers)
            seg_data = urllib.request.urlopen(req_seg, timeout=20).read()
            # HH3D conceals TS stream inside fake PNG; TS packet starts at byte 271
            out_f.write(seg_data[271:])
            
    # Remux to MP4
    cmd = ["ffmpeg", "-y", "-i", str(raw_ts), "-c", "copy", str(out_mp4_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    raw_ts.unlink(missing_ok=True)
    return out_mp4_path

if __name__ == "__main__":
    out = Path("D:/translate/shared_data/preview/hh3d_test_success.mp4")
    crawl_hh3d_video("https://www.hh3d.com/muc-than-ky/thuyet-minh/tap-107", out, max_segments=3)
    print("Done! File size:", out.stat().st_size)
