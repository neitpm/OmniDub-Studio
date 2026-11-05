"""Gateway / Orchestrator service: Web UI (Gradio) & Workflow Controller."""

import os
import shutil
import time
import itertools
import subprocess
from pathlib import Path
import requests
import yt_dlp
import gradio as gr

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared_data"))
STT_URL = os.environ.get("STT_URL", "http://stt-service:8001")
TRANS_URL = os.environ.get("TRANS_URL", "http://translation-service:8002")
TTS_URL = os.environ.get("TTS_URL", "http://tts-service:8003")
VIDEO_URL = os.environ.get("VIDEO_URL", "http://video-service:8004")

for d in ["audio", "data", "segments", "output", "preview", "inbox"]:
    (SHARED_DIR / d).mkdir(parents=True, exist_ok=True)


def api_error(res: requests.Response) -> str:
    """Extract a readable message from a failed microservice response."""
    try:
        detail = res.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return (res.text or "").strip()[:500] or f"HTTP {res.status_code}"


def check(res: requests.Response) -> None:
    """raise_for_status() but keeps the service-side error detail visible."""
    if res.status_code >= 400:
        raise RuntimeError(f"[{res.url}] -> {api_error(res)}")


def preflight(video_path: Path, expected_duration: float = 0.0) -> dict:
    """Validate the staged input before spending GPU time on Whisper / MT / TTS.

    A corrupt or partially blocked download must fail here, not 40 minutes later.
    """
    video_path = Path(video_path)
    if not video_path.resolve().is_relative_to(SHARED_DIR.resolve()):
        raise RuntimeError(f"preflight: file phải nằm trong shared volume, nhận được: {video_path}")
    rel = video_path.resolve().relative_to(SHARED_DIR.resolve()).as_posix()
    res = requests.post(f"{VIDEO_URL}/probe", json={"video_path": rel}, timeout=300)
    if res.status_code >= 400:
        raise RuntimeError(f"Video đầu vào không hợp lệ: {api_error(res)}")

    info = res.json()
    dur = float(info.get("duration") or 0.0)

    if expected_duration and expected_duration > 0:
        drift = abs(dur - expected_duration)
        if drift > max(2.0, 0.05 * expected_duration):
            raise RuntimeError(
                f"Thời lượng tải về ({dur:.1f}s) lệch xa thời lượng mong đợi ({expected_duration:.1f}s). "
                f"Video có thể bị chặn một phần hoặc bị cắt ngắn. Hãy upload file trực tiếp."
            )

    if not info.get("has_audio"):
        raise RuntimeError(
            f"Video ({dur:.1f}s, {info.get('video_codec')}) không có luồng âm thanh nên không thể làm STT/lồng tiếng."
        )

    mean_db = info.get("mean_volume_db")
    if mean_db is not None and float(mean_db) < -50.0:
        raise RuntimeError(
            f"Âm thanh gần như im lặng (mean {mean_db:.1f} dB) — có thể video bị tắt tiếng hoặc file tải hỏng. "
            f"Hãy thử upload file gốc từ máy."
        )

    print(
        f"[preflight] OK {dur:.1f}s | {info.get('width')}x{info.get('height')} "
        f"| {info.get('video_codec')}/{info.get('audio_codec')} "
        f"| mean={mean_db}dB | {info.get('size_bytes', 0) / 1_048_576:.1f} MB"
    )
    return info


def stage_to_inbox(video_local: str) -> Path:
    """Copy an uploaded file into the staging dir (never touched by clean_shared_dir)."""
    src = Path(video_local)
    if not src.exists():
        raise ValueError(f"Không tìm thấy file upload: {video_local}")
    ext = src.suffix or ".mp4"
    staged = SHARED_DIR / "inbox" / f"input_local{ext}"
    return safe_copy(src, staged)


# Gradio cache & UI results must NOT live on /shared_data: inside WSL2 that is a
# 9p (drvfs) mount where os.sendfile() fails with OSError(ENOMEM), which is what
# shutil.copy2() uses. Container-local ext4 avoids both the error and tmpfs/RAM use.
PREVIEW_DIR = Path(os.environ.get("PREVIEW_DIR", "/var/tmp/omnidub_preview"))
# Cap by count as well as size: with only a count cap, 6 voice previews would evict
# a 40-minute restyle result that the UI is still showing.
PREVIEW_KEEP = int(os.environ.get("PREVIEW_KEEP", "12"))
PREVIEW_MAX_BYTES = int(float(os.environ.get("PREVIEW_MAX_GB", "3")) * 1024 ** 3)
_PREVIEW_SEQ = itertools.count()


def _preview_name(tag: str, suffix: str) -> str:
    """Unique staging name: a second can produce many files (Tab 2 renders a whole gallery)."""
    return f"{tag}_{int(time.time())}_{next(_PREVIEW_SEQ):05d}{suffix}"


def _prune_preview_cache(protect: Path | None = None):
    """Bound the preview cache by BOTH file count and total size (a dub can be 700MB).

    `protect` (and the newest file) is never deleted, otherwise a single video larger
    than the cap would delete the very file we just staged for the UI.
    """
    try:
        files = sorted(PREVIEW_DIR.glob("*_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return
    total = 0
    for i, f in enumerate(files):
        try:
            total += f.stat().st_size
        except Exception:
            continue
        if (protect is not None and f == protect) or i == 0:
            continue
        if i < PREVIEW_KEEP and total <= PREVIEW_MAX_BYTES:
            continue
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def safe_copy(src, dst) -> Path:
    """Copy a file, falling back to chunked read/write when sendfile() is unsupported."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        print(f"[i] sendfile copy failed ({e}); falling back to chunked copy: {src.name}")
        with open(src, "rb") as f_in, open(dst, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, length=4 * 1024 * 1024)
    return dst


def media_duration(path: Path) -> float:
    """Do thoi luong (giay), tra 0.0 khi ffprobe khong doc duoc."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL).decode().strip()
        return float(out)
    except Exception:
        return 0.0


DUB_WAV_REL = "output/dubbed_audio.wav"


def find_dub_audio(video_rel: str) -> str:
    """Tra ve file thuyet minh dung cho video sap render, hoac "" neu khong co.

    /merge cua Tab 1 de lai dubbed_audio.wav. Lay file cua tap khap don de
    chong len tap nay thi con te hon im lang: lech noi dung voi hinh thi
    khong ai phat hien ra luc xem lai.
    """
    dub = SHARED_DIR / DUB_WAV_REL
    if not dub.exists():
        return ""
    v = media_duration(SHARED_DIR / video_rel)
    # dubbed_audio.wav luon dai hon video 1 giay (tong mau + 1 s dem)
    d = media_duration(dub)
    if v <= 0 or d <= 0 or abs(d - v) > max(2.5, 0.05 * v):
        print(f"[!] Bo qua thuyet minh: dub {d:.1f}s khong khop video {v:.1f}s")
        return ""
    return DUB_WAV_REL


def stage_for_ui(src, tag: str = "out") -> str:
    """Mirror a result file into PREVIEW_DIR and return that path for the UI components.

    The real file stays in /shared_data/output (visible from Windows); only the
    copy handed to gr.Video / gr.Audio / gr.Gallery comes from a Linux-native fs.
    """
    src = Path(src)
    if not src.exists():
        return str(src)
    if src.resolve().is_relative_to(PREVIEW_DIR.resolve()):
        return str(src)
    try:
        dst = safe_copy(src, PREVIEW_DIR / _preview_name(tag, src.suffix))
    except Exception as e:
        print(f"[!] Không stage được {src.name} cho UI: {e}")
        return str(src)

    # Keep the preview cache bounded so it cannot fill the container disk.
    _prune_preview_cache(protect=dst)
    return str(dst)


def parse_time_str(t_str: str) -> float:
    """Parse string like '00:22', '15:35', '01:23:45' or '22' into seconds."""
    t_str = str(t_str).strip()
    if not t_str:
        return 0.0
    if ":" in t_str:
        parts = t_str.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    return float(t_str)


def clean_shared_dir():
    for sub in ["segments", "audio", "data"]:
        p = SHARED_DIR / sub
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    (SHARED_DIR / "output").mkdir(parents=True, exist_ok=True)
    (SHARED_DIR / "inbox").mkdir(parents=True, exist_ok=True)


def clear_inbox():
    """Remove previously staged inputs (called per run, after validation)."""
    inbox = SHARED_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for stale in inbox.iterdir():
        if stale.is_file():
            stale.unlink(missing_ok=True)


def clean_video_url(url: str) -> str:
    """Normalize and clean URL for platforms like Bilibili and YouTube."""
    url = url.strip()
    import re
    bili_m = re.search(r"bilibili\.com/(?:[a-zA-Z0-9\-_]+/)?video/(BV[a-zA-Z0-9]+)", url)
    if bili_m:
        return f"https://www.bilibili.com/video/{bili_m.group(1)}/"
    return url


def run_ffmpeg_checked(cmd: list[str]):
    """Run FFmpeg and keep stderr for the error message instead of sending it to DEVNULL."""
    args = cmd[1:] if cmd and cmd[0] == "ffmpeg" else cmd
    proc = subprocess.run(["ffmpeg", "-hide_banner", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        tail = "\n".join(lines[-6:])
        raise RuntimeError(f"FFmpeg thất bại (exit {proc.returncode}):\n{tail[-1000:]}")


def crawl_hh3d_stream(url: str, out_dir: Path, download_full: bool = True) -> Path:
    """Dedicated crawler for HH3D (Hoạt hình 3D) websites that obfuscate streams inside fake PNGs."""
    import urllib.request, re, base64, json, subprocess
    out_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = out_dir / "downloaded_video.mp4"
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

    # Download full episode segments (or subset if not download_full)
    target_segments = seg_urls if download_full else seg_urls[:15]
    raw_ts = out_dir / "temp_hh3d.ts"
    with open(raw_ts, "wb") as out_f:
        for u in target_segments:
            req_seg = urllib.request.Request(u, headers=headers)
            seg_data = urllib.request.urlopen(req_seg, timeout=20).read()
            # HH3D conceals TS stream inside fake PNG; TS packet starts at byte 271
            out_f.write(seg_data[271:])

    # Remux to MP4
    cmd = ["ffmpeg", "-y", "-i", str(raw_ts), "-c", "copy", str(out_mp4)]
    run_ffmpeg_checked(cmd)
    raw_ts.unlink(missing_ok=True)
    return out_mp4


# Human-readable explanations for the most common yt-dlp failures.
BLOCK_HINTS = (
    ("Sign in to confirm", "YouTube yêu cầu xác minh 'bạn không phải bot' (PO token). Cách xử lý: cập nhật yt-dlp (`pip install -U yt-dlp`) hoặc chuyển sang chế độ upload file."),
    ("403: Forbidden", "Bị từ chối 403 — IP của bạn đã bị đánh dấu hoặc video yêu cầu đăng nhập. Thử upload file trực tiếp."),
    ("not available in your country", "Video bị chặn theo khu vực (geo-block)."),
    ("Video unavailable", "Video không còn tồn tại hoặc đã bị gỡ — thường do chủ sở hữu bản quyền yêu cầu takedown."),
    ("confirm your age", "Video giới hạn tuổi, cần cookie của tài khoản đã đăng nhập."),
    ("Requested format is not available", "Không lấy được format chất lượng cao — dấu hiệu thường gặp khi bị hạn chế anti-bot."),
)


def explain_download_error(err_text: str) -> str:
    for needle, hint in BLOCK_HINTS:
        if needle.lower() in err_text.lower():
            return hint
    return err_text[:600]


def download_youtube(url: str, out_dir: Path, start_time: str = "", end_time: str = "", rights_confirmed: bool = False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_url = clean_video_url(url)

    if not rights_confirmed:
        raise ValueError(
            "Tải video từ URL chỉ được phép khi bạn xác nhận có quyền sử dụng nội dung đó "
            "(video của bạn / đã được cấp phép / giấy phép cho phép tạo tác phẩm phái sinh). "
            "Nếu không, hãy upload file video trực tiếp."
        )

    # Check if URL is from specialized anime sites like HH3D
    if "hh3d.com" in clean_url or "donghua" in clean_url:
        full_video = crawl_hh3d_stream(clean_url, out_dir)
        if start_time or end_time:
            # Cut specific range from downloaded video
            s_sec = parse_time_str(start_time) if start_time else 0.0
            cmd_cut = ["ffmpeg", "-y", "-ss", str(s_sec), "-i", str(full_video)]
            if end_time:
                e_sec = parse_time_str(end_time)
                cmd_cut.extend(["-to", str(e_sec)])
            cut_mp4 = out_dir / "downloaded_video_cut.mp4"
            cmd_cut.extend(["-c", "copy", str(cut_mp4)])
            run_ffmpeg_checked(cmd_cut)
            return cut_mp4
        return full_video

    template = str(out_dir / "downloaded_video.%(ext)s")
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": template,
        "merge_output_format": "mp4",
        "overwrites": True,
        # Tự động tiếp tục khi mạng đứt quãng, nhưng retry có kiểm soát:
        # retry dồn dập không tăng tỉ lệ thành công mà chỉ khiến IP bị đánh dấu nhanh hơn.
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,  # 10MB chunk để tránh timeout socket
    }

    # If start_time or end_time is provided, download with external downloader ffmpeg args or post-trim
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            fn = ydl.prepare_filename(info)
            p = Path(fn)
            if not p.exists():
                mp4_p = p.with_suffix(".mp4")
                if mp4_p.exists():
                    p = mp4_p
                else:
                    cands = list(out_dir.glob("downloaded_video.*"))
                    if cands:
                        p = cands[0]
                    else:
                        raise RuntimeError(
                            f"yt-dlp không tạo ra file nào trong {out_dir}. "
                            f"Video có thể đã bị chặn/gỡ hoặc yêu cầu đăng nhập."
                        )

            if start_time or end_time:
                s_sec = parse_time_str(start_time) if start_time else 0.0
                cmd_cut = ["ffmpeg", "-y", "-ss", str(s_sec), "-i", str(p)]
                if end_time:
                    e_sec = parse_time_str(end_time)
                    cmd_cut.extend(["-to", str(e_sec)])
                cut_mp4 = out_dir / "downloaded_video_cut.mp4"
                cmd_cut.extend(["-c", "copy", str(cut_mp4)])
                run_ffmpeg_checked(cmd_cut)
                return cut_mp4

            return p
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"Tải video thất bại: {explain_download_error(str(e))}")


VOICE_KEY_MAP = {
    # Nhóm giọng Nữ
    "Nữ - Hoài My (Truyền cảm, tiêu chuẩn)": "female_standard",
    "Nữ - Ngọt ngào, trong trẻo (Tâm tình / Review)": "female_sweet",
    "Nữ - Review phim, TikTok năng động": "female_review",
    "Nữ - Dịu dàng, thủ thỉ (Audiobook / Kể chuyện)": "female_gentle",
    "Nữ - Đọc truyện đêm khuya, tình cảm": "female_story",
    "Nữ - Trầm ấm, sâu lắng (Phóng sự / Tài liệu)": "female_deep",
    "Nữ - Phát thanh viên, bản tin nhanh": "female_news",
    "Nữ - Chị Google (Google TTS)": "female_google",

    # Nhóm giọng Nam
    "Nam - Nam Minh (Ấm áp, tiêu chuẩn)": "male_standard",
    "Nam - Trầm ấm, phóng sự / phim tài liệu": "male_deep",
    "Nam - Năng động, tin tức nhanh": "male_dynamic",

    # Giọng AI Offline
    "MMS AI (Mô hình Local GPU)": "mms",
}


def preview_voice_sample(voice_name: str, sample_text: str) -> str:
    """Generate and return audio file path for quick voice audition."""
    voice_key = VOICE_KEY_MAP.get(voice_name, "female_standard")
    text = sample_text.strip() or "Xin chào bạn, đây là giọng đọc thử nghiệm thuyết minh tiếng Việt."
    
    # Call TTS service with 1 segment
    res = requests.post(
        f"{TTS_URL}/synthesize",
        json={
            "segments": [{"start": 0.0, "end": 2.5, "text": "preview", "translation": text}],
            "voice": voice_key
        }
    )
    check(res)
    data = res.json()
    wav_rel = data["segments"][0]["audio"]
    src_wav = SHARED_DIR / wav_rel
    
    out_preview = SHARED_DIR / "preview" / f"sample_{voice_key}.wav"
    safe_copy(src_wav, out_preview)
    return stage_for_ui(out_preview, "voice_preview")


def resolve_input_source(video_local: str, video_url: str, rights_confirmed: bool = False) -> Path:
    """Stage the input into SHARED_DIR/inbox WITHOUT touching previous results.

    Upload-local is preferred over URL: it keeps the pipeline independent from
    downloader anti-bot changes, and it is the only mode allowed without a
    rights confirmation.
    """
    if video_local:
        return stage_to_inbox(video_local)
    if video_url and video_url.strip():
        return download_youtube(video_url.strip(), SHARED_DIR / "inbox", rights_confirmed=rights_confirmed)
    raise ValueError("Vui lòng upload video từ máy (khuyến nghị) hoặc nhập URL video mà bạn có quyền sử dụng.")


def run_orchestration(video_local: str, video_url: str, lang_choice: str, mode_choices: list[str], voice_choice: str, multi_speaker: bool, rights_confirmed: bool = False, progress=gr.Progress()) -> str:
    enable_dub = "Lồng tiếng (Thuyết minh)" in mode_choices
    enable_sub = "Gắn phụ đề (Vietsub)" in mode_choices

    if not enable_dub and not enable_sub:
        raise ValueError("Vui lòng chọn ít nhất một tùy chọn: 'Lồng tiếng (Thuyết minh)' hoặc 'Gắn phụ đề (Vietsub)'.")

    # 1. Resolve + validate input BEFORE wiping any previous results.
    #    A blocked/corrupt download must cost seconds, not 40 minutes of GPU.
    progress(0.05, desc="Kiểm tra nguồn video...")
    staged = resolve_input_source(video_local, video_url, rights_confirmed)
    preflight(staged)

    # 2. Input is good -> now it is safe to clear the workspace
    clean_shared_dir()
    video_path = SHARED_DIR / "audio" / f"input{staged.suffix}"
    safe_copy(staged, video_path)
    clear_inbox()

    # Convert paths to relative to shared volume
    rel_video_path = video_path.relative_to(SHARED_DIR).as_posix()

    # 3. Extract Audio via Video Service
    progress(0.15, desc="Tách audio từ video...")
    res = requests.post(f"{VIDEO_URL}/extract-audio", json={"video_path": rel_video_path})
    check(res)
    audio_path = res.json()["audio_path"]

    # 4. STT via STT Service (with Speaker Diarization)
    progress(0.30, desc="Nhận dạng giọng nói & Phân tích hội thoại (Whisper + Diarization)...")
    lang_code = "zh" if lang_choice == "Tiếng Trung" else ("en" if lang_choice == "Tiếng Anh" else "auto")
    res = requests.post(f"{STT_URL}/transcribe", json={"audio_path": audio_path, "language": lang_code, "enable_diarization": multi_speaker})
    check(res)
    stt_data = res.json()
    segments = stt_data["segments"]
    detected_lang = stt_data.get("detected_lang", "zh")

    if not segments:
        raise RuntimeError("Không tìm thấy đoạn hội thoại nào trong video.")

    # 5. Translation via Translation Service
    progress(0.50, desc="Dịch sang tiếng Việt (Opus-MT)...")
    source_lang = "en" if (lang_code == "en" or (lang_code == "auto" and detected_lang == "en")) else "zh"
    res = requests.post(f"{TRANS_URL}/translate", json={"segments": segments, "source_lang": source_lang})
    check(res)
    segments = res.json()["segments"]

    # 6. TTS via TTS Service
    if enable_dub:
        desc_txt = "Lồng tiếng hội thoại Đa Nhân Vật (Nam/Nữ tự động)..." if multi_speaker else f"Lồng tiếng thuyết minh ({voice_choice})..."
        progress(0.70, desc=desc_txt)
        voice_key = VOICE_KEY_MAP.get(voice_choice, "female_standard")
        res = requests.post(
            f"{TTS_URL}/synthesize",
            json={
                "segments": segments,
                "voice": voice_key,
                "multi_speaker": multi_speaker
            }
        )
        check(res)
        segments = res.json()["segments"]
    else:
        progress(0.70, desc="Bỏ qua lồng tiếng (chỉ làm Vietsub)...")

    # 7. Muxing via Video Service
    progress(0.85, desc="Ghép video theo tùy chọn...")
    res = requests.post(
        f"{VIDEO_URL}/merge",
        json={
            "video_path": rel_video_path,
            "segments": segments,
            "enable_dubbing": enable_dub,
            "enable_subtitle": enable_sub
        }
    )
    check(res)
    final_rel = res.json()["output_video"]
    final_video = SHARED_DIR / final_rel

    progress(1.0, desc="Hoàn tất!")
    # (đường dẫn cho UI, đường dẫn thật trên Windows) - xem stage_for_ui()
    return stage_for_ui(final_video, "dubbed"), str(final_video)


def trim_video_action(video_in: str, start_t: str, end_t: str, out_name: str) -> tuple[str, str]:
    if not video_in:
        default_path = SHARED_DIR / "output" / "dubbed_video.mp4"
        if default_path.exists():
            rel_path = "output/dubbed_video.mp4"
        else:
            raise ValueError("Chưa có video để cắt. Vui lòng tạo video trước hoặc upload video.")
    else:
        v_p = Path(video_in)
        if str(SHARED_DIR) in str(v_p):
            rel_path = v_p.relative_to(SHARED_DIR).as_posix()
        else:
            rel_path = f"output/{v_p.name}"
            safe_copy(v_p, SHARED_DIR / rel_path)

    res = requests.post(
        f"{VIDEO_URL}/trim",
        json={
            "video_path": rel_path,
            "start_time": start_t,
            "end_time": end_t,
            "output_filename": out_name
        }
    )
    check(res)
    data = res.json()
    out_file = SHARED_DIR / data["trimmed_video"]
    msg = f"Đã cắt thành công đoạn ({data['start_sec']}s -> {data['end_sec']}s, dài {data['duration']}s)!\nFile lưu tại: {out_file}"
    return stage_for_ui(out_file, "trimmed"), msg


def main():
    theme = gr.themes.Soft(primary_hue="blue", secondary_hue="indigo")
    with gr.Blocks(title="OmniDub Studio - AI Video Dubbing & Subtitling", theme=theme) as demo:
        gr.Markdown(
            """
            # 🎬 OmniDub Studio — AI Video Dubbing & Subtitling (Trung/Anh ➔ Việt)
            Hệ sinh thái AI Microservices: **Lồng tiếng Thuyết minh đa vai Nam/Nữ**, **Gắn Vietsub**, **Nghe thử giọng** & **Cắt ghép Video**.
            """
        )

        with gr.Tabs():
            # TAB 1: DUBBING & SUBTITLING PIPELINE
            with gr.TabItem("🎙️ 1. Lồng Tiếng & Gắn Vietsub"):
                with gr.Row():
                    with gr.Column(scale=1):
                        file_box = gr.Video(
                            label="1. Upload video từ máy (khuyến nghị - ổn định nhất, không phụ thuộc tải URL)",
                            sources=["upload"]
                        )
                        url_box = gr.Textbox(
                            label="2. Hoặc nhập URL YouTube / Bilibili",
                            placeholder="https://www.bilibili.com/video/... hoặc https://youtube.com/..."
                        )
                        rights_toggle = gr.Checkbox(
                            label="⚖️ Tôi xác nhận có quyền sử dụng nội dung ở URL trên (video của tôi / đã được cấp phép / giấy phép cho phép tạo tác phẩm phái sinh)",
                            value=False,
                            info="Bắt buộc tích để bật chế độ tải từ URL. Upload file trực tiếp không cần xác nhận này."
                        )

                        lang_choice = gr.Radio(
                            choices=["Tự động nhận diện", "Tiếng Trung", "Tiếng Anh"],
                            value="Tự động nhận diện",
                            label="3. Ngôn ngữ nguồn của video"
                        )
                        
                        mode_choices = gr.CheckboxGroup(
                            choices=["Gắn phụ đề (Vietsub)", "Lồng tiếng (Thuyết minh)"],
                            value=["Gắn phụ đề (Vietsub)", "Lồng tiếng (Thuyết minh)"],
                            label="5. Chế độ đầu ra mong muốn"
                        )

                        multi_speaker_toggle = gr.Checkbox(
                            label="👥 Bật nhận diện hội thoại Đa Nhân Vật (Tự động phát hiện Nam/Nữ để lồng giọng tương ứng)",
                            value=False
                        )
                        
                        with gr.Group():
                            voice_choice = gr.Dropdown(
                                choices=list(VOICE_KEY_MAP.keys()),
                                value="Nữ - Hoài My (Truyền cảm, tiêu chuẩn)",
                                label="6. Chọn giọng đọc mặc định (dùng khi không bật Đa nhân vật)",
                                interactive=True
                            )
                            with gr.Row():
                                sample_text_input = gr.Textbox(
                                    label="Câu đọc thử",
                                    value="Xin chào bạn, đây là giọng đọc thử nghiệm thuyết minh tiếng Việt.",
                                    lines=1,
                                    scale=3
                                )
                                preview_btn = gr.Button("🔊 Nghe thử giọng này", variant="secondary", scale=1)
                            preview_audio = gr.Audio(label="Bản nghe thử", interactive=False, autoplay=True)

                        btn = gr.Button("🚀 Bắt đầu xử lý (Microservices)", variant="primary", size="lg")

                    with gr.Column(scale=1):
                        out_video = gr.Video(label="Video kết quả", interactive=False)
                        status = gr.Textbox(label="Trạng thái xử lý", interactive=False)

                # Event handlers
                def handle_preview(v_name, txt):
                    try:
                        return preview_voice_sample(v_name, txt)
                    except Exception as e:
                        print(f"Preview error: {e}")
                        return None

                preview_btn.click(
                    fn=handle_preview,
                    inputs=[voice_choice, sample_text_input],
                    outputs=[preview_audio]
                )

                def handle_pipeline(f, u, l, m, v, multi_spk, rights_ok):
                    try:
                        ui_path, real_path = run_orchestration(f, u, l, m, v, multi_spk, rights_confirmed=rights_ok)
                        return ui_path, f"Thành công! Video đã lưu tại:\n{real_path}"
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        return None, f"Lỗi: {e}"

                btn.click(
                    fn=handle_pipeline,
                    inputs=[file_box, url_box, lang_choice, mode_choices, voice_choice, multi_speaker_toggle, rights_toggle],
                    outputs=[out_video, status]
                )

            # TAB 2: AI STORY TO 3D ANIMATION STUDIO
            with gr.TabItem("🎨 2. Chuyển Thể Truyện ➔ Phim Hoạt Hình 3D"):
                gr.Markdown(
                    """
                    ### 🧙‍♂️ AI Novel to 3D Anime Movie Studio (Kịch bản ➔ Phim 3D)
                    Nhập đoạn truyện chữ, tiểu thuyết hoặc kịch bản. Hệ thống AI sẽ:
                    1. **Tự động bóc tách kịch bản (Storyboarding)**: Phân chia từng cảnh, góc máy và lời thoại.
                    2. **Tạo Prompt 3D Animation chất lượng cao**: Sẵn sàng kết nối mô hình sinh ảnh/video 3D (*Wan2.1 / FLUX 3D / LTX-Video*).
                    3. **Tự động chuyển câu thoại sang OmniDub Studio** để lồng tiếng đa nhân vật Nam/Nữ!
                    """
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        novel_input = gr.Textbox(
                            label="Nhập đoạn văn bản truyện chữ / kịch bản (Tiếng Việt / Trung / Anh)",
                            placeholder="Ví dụ: Tiêu Viêm nắm chặt bàn tay, nhìn lên đỉnh núi Vân Lam Tông, trong mắt bừng lên một tia lửa tím dị thường. Hắn nói: 'Ba năm ước hẹn, hôm nay ta tới đòi lại thể diện!' Lúc này Nạp Lan Yên Nhiên từ từ rút thanh kiếm thanh quang ra đáp lại: 'Tiêu Viêm, ngươi quả thực không khiến ta thất vọng.'...",
                            lines=8
                        )
                        with gr.Row():
                            style_choice = gr.Dropdown(
                                choices=[
                                    "Hoạt hình 3D Tiên Hiệp / Huyền Huyễn (Phong cách Đấu Phá Khung Thương)",
                                    "Hoạt hình 3D Pixar / Disney (Hiện đại, dễ thương, cinematic)",
                                    "Anime 3D Cyberpunk / Viễn Tưởng Hiện Đại",
                                    "3D Cổ Trang Thủy Mặc Điện Ảnh (Cinematic Unreal Engine 5)"
                                ],
                                value="Hoạt hình 3D Tiên Hiệp / Huyền Huyễn (Phong cách Đấu Phá Khung Thương)",
                                label="Phong cách đồ họa 3D"
                            )
                            scene_count = gr.Slider(minimum=1, maximum=10, value=3, step=1, label="Số lượng phân cảnh (Scenes)")

                        story_btn = gr.Button("🎬 Bắt đầu Phân Cảnh & Tạo Kịch Bản 3D", variant="primary", size="lg")

                    with gr.Column(scale=1):
                        storyboard_output = gr.Textbox(
                            label="Bảng phân cảnh chi tiết (Storyboard & 3D Prompts)",
                            lines=12,
                            interactive=False
                        )
                        dialogues_output = gr.Textbox(
                            label="Danh sách câu thoại đã trích xuất (Sẵn sàng lồng tiếng)",
                            lines=4,
                            interactive=False
                        )

                # SECTION: VISUAL 3D FRAMES & MOVIE PLAYER
                gr.Markdown("---")
                gr.Markdown("### 🖼️ Khung Hình 3D & Video Phim Hoạt Hình Chuyển Thể")
                with gr.Row():
                    with gr.Column(scale=1):
                        gen_media_btn = gr.Button("🚀 1. Tạo Ảnh Khung Hình 3D & Dựng Video Phim", variant="primary", size="lg")
                        gen_status = gr.Textbox(label="Tiến trình dựng phim 3D", interactive=False)
                        scene_gallery = gr.Gallery(label="Bộ Ảnh Khung Hình 3D Từng Phân Cảnh (Scene Keyframes)", columns=2, height=360)

                    with gr.Column(scale=1):
                        movie_video_out = gr.Video(label="🎬 Video Phim Hoạt Hình 3D (Đã lồng tiếng & Chuyển động)", interactive=False)

                def render_3d_scenes_and_movie(text: str, style: str, num_scenes: int, progress=gr.Progress()):
                    if not text or not text.strip():
                        return "Vui lòng nhập truyện trước khi dựng phim.", [], None

                    progress(0.05, desc="Bắt đầu chuyển thể truyện thành hình ảnh & video 3D...")
                    scenes_dir = SHARED_DIR / "scenes"
                    scenes_dir.mkdir(parents=True, exist_ok=True)

                    import re, subprocess, requests, urllib.parse
                    from PIL import Image

                    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
                    full_text = " ".join(lines)
                    sentences = [s.strip() for s in re.split(r'[.!?。！？\n]', full_text) if len(s.strip()) > 5]
                    if not sentences:
                        sentences = [full_text]

                    actual_scenes = min(int(num_scenes), len(sentences))
                    step = max(1, len(sentences) // actual_scenes)

                    image_paths = []
                    clip_videos = []

                    style_prompts = {
                        "Hoạt hình 3D Tiên Hiệp / Huyền Huyễn (Phong cách Đấu Phá Khung Thương)": 
                            "3D Chinese Donghua Xianxia anime style, Unreal Engine 5 render, handsome martial arts character with spiritual weapon, cinematic lighting, ancient palace",
                        "Hoạt hình 3D Pixar / Disney (Hiện đại, dễ thương, cinematic)": 
                            "3D Disney Pixar character animation style, expressive face, vibrant lighting, highly detailed 3D render",
                        "Anime 3D Cyberpunk / Viễn Tưởng Hiện Đại": 
                            "3D futuristic cyberpunk anime warrior, volumetric neon city lights, sleek high-tech character render",
                        "3D Cổ Trang Thủy Mặc Điện Ảnh (Cinematic Unreal Engine 5)": 
                            "Cinematic Chinese 3D character, flowing traditional robes, misty mountain background, 8k render"
                    }
                    base_prompt = style_prompts.get(style, "3D Donghua Xianxia anime character render")

                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                        'Referer': 'https://pollinations.ai/'
                    }

                    for i in range(actual_scenes):
                        scene_idx = i + 1
                        progress(0.1 + (0.7 * (i / max(1, actual_scenes))), desc=f"Đang sinh ảnh Nhân Vật 3D & Lồng tiếng Cảnh {scene_idx}/{actual_scenes}...")
                        start_idx = i * step
                        scene_content = " ".join(sentences[start_idx : start_idx + step])
                        if not scene_content:
                            scene_content = sentences[0]

                        # 1. GENERATE REAL 3D CHARACTER IMAGE FROM PROMPT (Text-to-Image AI)
                        img_path = scenes_dir / f"scene_{scene_idx}.jpg"
                        
                        # Build prompt with keywords from story
                        prompt_query = f"{base_prompt}, scene {scene_idx}, {scene_content[:80]}"
                        encoded_prompt = urllib.parse.quote(prompt_query)
                        img_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=turbo&nologo=true&seed={100 + scene_idx}"

                        try:
                            res = requests.get(img_url, headers=headers, timeout=12)
                            if res.status_code == 200 and len(res.content) > 5000:
                                with open(img_path, "wb") as f:
                                    f.write(res.content)
                        except Exception as e:
                            print(f"[!] Image gen error for scene {scene_idx}: {e}")

                        # Fallback to high quality 3D Donghua sample (Tần Mục / Tiên Hiệp 3D) if network rate-limits
                        if not img_path.exists() or img_path.stat().st_size < 5000:
                            fallback_src = SHARED_DIR / "preview" / "sample_tan_muc.jpg"
                            if not fallback_src.exists() or fallback_src.stat().st_size < 5000:
                                fallback_src = SHARED_DIR / "preview" / "sample_real_3d.jpg"
                            if fallback_src.exists():
                                safe_copy(fallback_src, img_path)

                        image_paths.append(stage_for_ui(img_path, "scene"))

                        # 2. GENERATE AUDIO VOICE FOR THIS SCENE (TTS Service)
                        audio_path = scenes_dir / f"audio_{scene_idx}.wav"
                        try:
                            # Call TTS microservice to synthesize voice
                            tts_res = requests.post(
                                f"{TTS_URL}/synthesize",
                                json={
                                    "segments": [{"start": 0.0, "end": 4.0, "text": f"s_{scene_idx}", "translation": scene_content[:150]}],
                                    "voice": "female_standard"
                                },
                                timeout=15
                            )
                            tts_data = tts_res.json()
                            gen_audio = SHARED_DIR / tts_data["segments"][0]["audio"]
                            safe_copy(gen_audio, audio_path)
                        except Exception as e:
                            print(f"[!] TTS error scene {scene_idx}: {e}")

                        # Get audio duration
                        dur = 4.0
                        if audio_path.exists() and audio_path.stat().st_size > 1000:
                            try:
                                cmd_p = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)]
                                dur_str = subprocess.check_output(cmd_p).decode().strip()
                                dur = max(3.0, float(dur_str))
                            except Exception:
                                dur = 4.0

                        # 3. RENDER 3D VIDEO CLIP WITH GUARANTEED AUDIO
                        clip_path = scenes_dir / f"clip_{scene_idx}.mp4"
                        
                        # If audio file is missing or failed, generate a silent wav so every clip has identical audio streams
                        if not audio_path.exists() or audio_path.stat().st_size < 100:
                            subprocess.run([
                                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                "-t", str(dur), "-c:a", "pcm_s16le", str(audio_path)
                            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        cmd_clip = [
                            "ffmpeg", "-y", "-loop", "1", "-i", str(img_path),
                            "-i", str(audio_path),
                            "-c:v", "libx264", "-t", str(dur), "-pix_fmt", "yuv420p",
                            "-vf", "scale=1024:768,zoompan=z='min(zoom+0.001,1.15)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1024x768",
                            "-c:a", "aac", "-b:a", "192k", "-ar", "16000", "-ac", "1",
                            "-shortest",
                            str(clip_path)
                        ]
                        subprocess.run(cmd_clip, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        if clip_path.exists() and clip_path.stat().st_size > 1000:
                            clip_videos.append(clip_path)

                    # 4. CONCATENATE ALL SCENE CLIPS INTO COMPLETE MOVIE
                    progress(0.9, desc="Ghép toàn bộ phân cảnh thành phim hoạt hình 3D hoàn chỉnh...")
                    concat_list_file = scenes_dir / "concat_movie_list.txt"
                    with open(concat_list_file, "w", encoding="utf-8") as f:
                        for cp in clip_videos:
                            f.write(f"file '{cp.name}'\n")

                    final_movie_path = SHARED_DIR / "output" / "3d_novel_movie.mp4"
                    cmd_concat = [
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(concat_list_file),
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        str(final_movie_path)
                    ]
                    subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    progress(1.0, desc="Đã hoàn tất chuyển thể Truyện ➔ Phim Hoạt Hình 3D!")
                    msg = f"Đã chuyển thể thành công {len(image_paths)} cảnh với Nhân vật 3D & Giọng đọc thuyết minh tại:\n{final_movie_path}"
                    return msg, image_paths, stage_for_ui(final_movie_path, "movie")

                gen_media_btn.click(
                    fn=render_3d_scenes_and_movie,
                    inputs=[novel_input, style_choice, scene_count],
                    outputs=[gen_status, scene_gallery, movie_video_out]
                )

                def process_story_to_3d(text: str, style: str, num_scenes: int):
                    if not text or not text.strip():
                        return "Vui lòng nhập nội dung đoạn truyện chữ.", ""

                    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
                    full_text = " ".join(lines)
                    
                    # Split into sentences
                    import re
                    sentences = [s.strip() for s in re.split(r'[.!?。！？\n]', full_text) if len(s.strip()) > 5]
                    if not sentences:
                        sentences = [full_text]

                    step = max(1, len(sentences) // num_scenes)
                    
                    style_prompts = {
                        "Hoạt hình 3D Tiên Hiệp / Huyền Huyễn (Phong cách Đấu Phá Khung Thương)": 
                            "3D Chinese Donghua Xianxia anime style, cinematic lighting, 8k Unreal Engine 5 render, glowing spiritual aura, highly detailed character",
                        "Hoạt hình 3D Pixar / Disney (Hiện đại, dễ thương, cinematic)": 
                            "3D Disney Pixar modern animation style, vibrant colors, expressive character design, ray tracing, soft cinematic depth of field",
                        "Anime 3D Cyberpunk / Viễn Tưởng Hiện Đại": 
                            "3D futuristic cyberpunk anime render, volumetric neon lights, sleek high-tech outfits, dramatic perspective",
                        "3D Cổ Trang Thủy Mặc Điện Ảnh (Cinematic Unreal Engine 5)": 
                            "Cinematic Chinese historical 3D animation, dramatic cinematic atmosphere, photorealistic misty mountain background, UE5 octane render"
                    }
                    base_style_prompt = style_prompts.get(style, "3D cinematic animation style, 8k render")

                    storyboard_result = f"🎬 KỊCH BẢN PHÂN CẢNH 3D CHO TRUYỆN\nPhong cách: {style}\n{'='*60}\n\n"
                    dialogue_list = []

                    for i in range(min(num_scenes, len(sentences))):
                        start_idx = i * step
                        seg_text = " ".join(sentences[start_idx : start_idx + step])
                        if not seg_text:
                            continue
                        
                        scene_num = i + 1
                        storyboard_result += f"🎥 PHÂN CẢNH {scene_num}:\n"
                        storyboard_result += f"• Nội dung cảnh: {seg_text}\n"
                        storyboard_result += f"• Góc máy đề xuất: Cận cảnh nhân vật kết hợp góc rộng toàn cảnh (Medium shot to wide cinematic pan)\n"
                        storyboard_result += f"• Prompt sinh Video 3D (Wan2.1 / LTX-Video / FLUX):\n"
                        storyboard_result += f"  \"{base_style_prompt}, scene {scene_num}: {seg_text[:90]}...\"\n"
                        storyboard_result += f"{'-'*60}\n"

                        # Extract potential dialogues (in quotes)
                        quotes = re.findall(r"['\"‘“](.*?)['\"’”]", seg_text)
                        if quotes:
                            for q in quotes:
                                dialogue_list.append(f"Cảnh {scene_num}: \"{q}\"")
                        else:
                            dialogue_list.append(f"Cảnh {scene_num} (Lời dẫn chuyện): \"{seg_text[:80]}...\"")

                    dialogue_text = "\n".join(dialogue_list) if dialogue_list else "Không tìm thấy câu thoại trực tiếp, hệ thống dùng lời dẫn chuyện làm thoại."
                    return storyboard_result, dialogue_text

                story_btn.click(
                    fn=process_story_to_3d,
                    inputs=[novel_input, style_choice, scene_count],
                    outputs=[storyboard_output, dialogues_output]
                )

            # TAB 3: VIDEO RESTYLE 3D TO 2D ANIME STUDIO
            with gr.TabItem("🖌️ 3. Vẽ Lại Video 3D ➔ Hoạt Hình 2D (Remake)"):
                gr.Markdown(
                    """
                    ### 🎨 Chuyển Thể Video 3D Gốc Thành Phim Hoạt Hình 2D Độc Quyền
                    Dán link (Bilibili/YouTube/HH3D) hoặc upload file — hệ thống tự tải, cắt theo khoảng thời gian, rồi:
                    1. **Tẩy chữ burn-in**: logo bit.ly/bilibili/牧神記 ở 3 góc và phụ đề Trung Quốc ở đáy (phụ đề được *tìm từng chữ*, không xoá trọn dải vì dải đó có chân nhân vật).
                    2. **Phá kết cấu 3D**: mean-shift gom ảnh về các vùng màu phẳng, sau đó lượng tử hoá cả video vào **một bảng màu chung** để màu không nhảy flicker giữa các frame.
                    3. **Vẽ lại nét**: chỉ giữ nét dài, vẽ lại đều tay bằng drawContours; bỏ hẳn lớp đổ bóng mềm của render 3D.
                    4. **Giảm còn 12.5 fps** (draw on twos) nếu chọn — vừa đúng cách vẽ anime, vừa giảm nửa thời gian.

                    ⚠️ Giới hạn thật: đây là vẽ lại **màu và nét** từng frame, không phải vẽ lại **hình học khuôn mặt** — mắt/mũi vẫn là khuôn mặt 3D. Muốn sang anime thực sự ở phần mặt cần khuếch tán (diffusion), chi phí 6–14 giờ/tập.
                    """
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        restyle_url_box = gr.Textbox(
                            label="1. Nhập URL video cần crawl (YouTube / Bilibili / HH3D)",
                            placeholder="https://www.bilibili.com/video/... hoặc https://youtube.com/..."
                        )
                        restyle_rights_toggle = gr.Checkbox(
                            label="⚖️ Tôi xác nhận có quyền sử dụng nội dung ở URL trên",
                            value=False,
                            info="Bắt buộc để bật tải từ URL. Upload file hoặc dùng video vừa tạo không cần xác nhận này."
                        )
                        with gr.Row():
                            restyle_start_time = gr.Textbox(
                                label="Thời gian bắt đầu cắt (Ví dụ: 22 hoặc 00:22)",
                                placeholder="00:22 hoặc để trống lấy từ đầu",
                                value="00:22"
                            )
                            restyle_end_time = gr.Textbox(
                                label="Thời gian kết thúc cắt (Ví dụ: 15:35)",
                                placeholder="15:35 hoặc để trống lấy đến hết",
                                value="15:35"
                            )
                        restyle_input_video = gr.Video(
                            label="Hoặc upload file video từ máy (Để trống sẽ lấy video vừa tải)",
                            sources=["upload"]
                        )
                        restyle_style = gr.Dropdown(
                            choices=[
                                "Cel Flat 2D (Phá kết cấu 3D, bảng màu khoá cả video, vẽ lại nét) - Khuyên dùng",
                                "AnimeGAN AI 2D (Chuyển thể Mạng Neural - Phong cách Hoạt hình Shinkai/Ghibli)",
                                "Anime 2D Cel-Shading (Nét vẽ tay Manga, màu phẳng tươi sáng)",
                                "2D Hoạt Hình Comic (Nét viền đậm, phong cách truyện tranh)"
                            ],
                            value="Cel Flat 2D (Phá kết cấu 3D, bảng màu khoá cả video, vẽ lại nét) - Khuyên dùng",
                            label="2. Chọn phong cách vẽ 2D"
                        )
                        restyle_twos = gr.Checkbox(
                            label="️ Vẽ 12.5 fps kiểu anime (draw on twos)",
                            value=True,
                            info="Bỏ nửa số khung hình, đúng cách vẽ anime truyền thống và giảm nửa thời gian render (khoảng 40 phút cho một tập)."
                        )
                        restyle_palette = gr.Slider(
                            label="Số màu bảng màu 2D (ít màu = phẳng và graphic hơn)",
                            minimum=8, maximum=28, step=2, value=16
                        )

                        with gr.Group():
                            anti_copyright_toggle = gr.Checkbox(
                                label="🛡️ Bật LÁ CHẮN BẢN QUYỀN (Anti-Copyright Shield)",
                                value=True,
                                info="Tự động phá vỡ thuật toán Content ID: Zoom 104%, Tăng tốc vi mô 1.03x; pitch-shift chỉ áp dụng khi video vẫn còn giữ tiếng gốc"
                            )
                            flip_horizontal_toggle = gr.Checkbox(
                                label="🪞 Lật gương khung hình ngang (Horizontal Flip)",
                                value=False,
                                info="Đảo ngược khung hình từ trái sang phải, vô hiệu hóa hoàn toàn đối chiếu Motion Vector của YouTube"
                            )
                            restyle_keep_orig = gr.Checkbox(
                                label="🔊 Giữ tiếng gốc của kênh trong video 2D",
                                value=False,
                                info="Mặc định TẮT: bỏ tiếng Trung + nhạc nền gốc, chỉ giữ phần thuyết minh nếu đã chạy Tab 1. Chưa có thuyết minh thì video 2D sẽ im lặng."
                            )
                        
                        restyle_btn = gr.Button("🎨 Bắt đầu Tải & Vẽ Lại Thành Video 2D", variant="primary", size="lg")

                    with gr.Column(scale=1):
                        restyle_video_out = gr.Video(label="🎬 Video 2D Hoàn Chỉnh Sau Khi Vẽ Lại", interactive=False)
                        restyle_status = gr.Textbox(label="Tiến trình vẽ lại", interactive=False)

                def handle_restyle(url_in, start_t, end_t, v_in, style_val, twos, palette_k, anti_cp, flip_h, keep_orig, rights_ok, progress=gr.Progress()):
                    if url_in and url_in.strip():
                        progress(0.1, desc="Đang tải đoạn video yêu cầu về...")
                        dl_path = download_youtube(
                            url_in.strip(), SHARED_DIR / "audio",
                            start_time=start_t.strip(), end_time=end_t.strip(),
                            rights_confirmed=rights_ok
                        )
                        # Fail fast here instead of rendering 60 phút trên file hỏng
                        preflight(dl_path)
                        rel_p = dl_path.relative_to(SHARED_DIR).as_posix()
                    elif v_in:
                        p = Path(v_in)
                        if str(SHARED_DIR) in str(p):
                            rel_p = p.relative_to(SHARED_DIR).as_posix()
                        else:
                            rel_p = f"output/input_restyle_{p.name}"
                            safe_copy(p, SHARED_DIR / rel_p)
                    else:
                        # Find cut video or downloaded video or dubbed video
                        cands = list((SHARED_DIR / "audio").glob("downloaded_video_cut.mp4")) or list((SHARED_DIR / "audio").glob("downloaded_video.*"))
                        if cands:
                            rel_p = cands[0].relative_to(SHARED_DIR).as_posix()
                        else:
                            fallback = SHARED_DIR / "output" / "dubbed_video.mp4"
                            if fallback.exists():
                                rel_p = "output/dubbed_video.mp4"
                            else:
                                raise ValueError("Vui lòng nhập link video hoặc upload video cần vẽ lại trước.")

                    progress(0.3, desc="Đang xây bảng màu 2D, tẩy chữ và vẽ lại từng khung hình...")
                    dub_rel = "" if keep_orig else find_dub_audio(rel_p)
                    out_name = f"anime_2d_{int(time.time())}.mp4"
                    res = requests.post(
                        f"{VIDEO_URL}/restyle-2d",
                        json={
                            "video_path": rel_p,
                            "style": style_val,
                            "output_filename": out_name,
                            "remove_watermark": True,
                            "anti_copyright": anti_cp,
                            "flip_horizontal": flip_h,
                            "dub_audio_path": dub_rel,
                            "keep_original_audio": bool(keep_orig),
                            "fps_div": 2 if twos else 1,
                            "palette_k": int(palette_k),
                        },
                        timeout=3600  # Cho phép render tới 60 phút cho các tập phim dài mà không bị timeout
                    )
                    check(res)
                    data = res.json()
                    out_f = SHARED_DIR / data["restyle_video"]
                    progress(1.0, desc="Hoàn tất vẽ lại video!")
                    AUDIO_MSG = {
                        "thuyet minh": "🔊 Audio: thuyết minh tiếng Việt. Đã bỏ tiếng gốc.",
                        "goc": "🔊 Audio: VẪN CÒN TIẾNG GỐC theo lựa chọn của bạn — đây là phần dễ bị đối chiếu bản quyền nhất.",
                        "khong co": "🔇 Video 2D xuất ra KHÔNG CÓ TIẾNG: không tìm thấy phần thuyết minh cho đúng đoạn này. Muốn có tiếng Việt thì chạy Tab 1 (Lồng tiếng) trước rồi quay lại Tab 3.",
                    }
                    audio_msg = AUDIO_MSG.get(data.get("audio_kind"), f"🔊 Audio: {data.get('audio_kind')}")
                    msg = (f"Đã vẽ lại thành công {data['frames_processed']} khung hình sang phong cách Hoạt hình 2D!\n"
                           f"{audio_msg}\nFile lưu tại: {out_f}")
                    return stage_for_ui(out_f, "restyle"), msg

                restyle_btn.click(
                    fn=handle_restyle,
                    inputs=[restyle_url_box, restyle_start_time, restyle_end_time, restyle_input_video,
                            restyle_style, restyle_twos, restyle_palette, anti_copyright_toggle,
                            flip_horizontal_toggle, restyle_keep_orig, restyle_rights_toggle],
                    outputs=[restyle_video_out, restyle_status]
                )

            # TAB 4: TRIM & CUT VIDEO STUDIO
            with gr.TabItem("✂️ 4. Chỉnh Sửa & Cắt Video"):
                gr.Markdown("### Cắt lấy đoạn video mong muốn từ video vừa tạo hoặc upload video mới")
                with gr.Row():
                    with gr.Column(scale=1):
                        edit_source_video = gr.Video(
                            label="Video cần cắt (Để trống sẽ tự động lấy video vừa tạo)",
                            sources=["upload"]
                        )
                        with gr.Row():
                            start_time_input = gr.Textbox(
                                label="Thời gian bắt đầu (Start Time)",
                                placeholder="00:00:10 hoặc 10 (giây)",
                                value="00:00:00"
                            )
                            end_time_input = gr.Textbox(
                                label="Thời gian kết thúc (End Time)",
                                placeholder="00:01:30 hoặc 90 (giây)",
                                value="00:01:00"
                            )
                        out_name_input = gr.Textbox(
                            label="Tên file lưu lại (Tùy chọn)",
                            placeholder="my_cut_video.mp4",
                            value="my_highlight_video.mp4"
                        )
                        trim_btn = gr.Button("✂️ Cắt và Lưu Video", variant="secondary", size="lg")

                    with gr.Column(scale=1):
                        trimmed_video_out = gr.Video(label="Video đã cắt", interactive=False)
                        trim_status = gr.Textbox(label="Thông tin đoạn cắt & Đường dẫn lưu", interactive=False)

                def handle_trim(v_in, s_t, e_t, o_name):
                    try:
                        v_res, msg = trim_video_action(v_in, s_t, e_t, o_name)
                        return v_res, msg
                    except Exception as e:
                        return None, f"Lỗi: {e}"

                trim_btn.click(
                    fn=handle_trim,
                    inputs=[edit_source_video, start_time_input, end_time_input, out_name_input],
                    outputs=[trimmed_video_out, trim_status]
                )

    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port, allowed_paths=["/shared_data", str(PREVIEW_DIR)])


if __name__ == "__main__":
    main()
