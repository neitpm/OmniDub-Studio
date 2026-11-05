"""Video & Audio Processing Microservice (FFmpeg & Timeline Muxing)."""

import os
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import soundfile as sf
import numpy as np

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared_data"))

app = FastAPI(title="Video Processing Service")


def run_ffmpeg(cmd: list[str]):
    """Run FFmpeg and surface stderr on failure instead of swallowing it."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", *cmd], capture_output=True, text=True)
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        tail = "\n".join(lines[-6:])
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg thất bại (exit {proc.returncode}):\n{tail[-1200:]}"
        )


def ffprobe_json(media_path: Path) -> dict:
    """Return parsed ffprobe metadata (format + streams) for a media file."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(media_path)
            ],
            capture_output=True, text=True, timeout=180
        )
    except FileNotFoundError:
        raise RuntimeError("Không tìm thấy ffprobe trong container video-service.")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-400:] or "ffprobe không trả về dữ liệu")
    if not proc.stdout.strip():
        raise RuntimeError("ffprobe trả về rỗng - file không phải video hợp lệ.")
    return json.loads(proc.stdout)


def measure_loudness(media_path: Path) -> dict:
    """Parse volumedetect output to catch silent / corrupted audio tracks early."""
    out = {"mean_volume_db": None, "max_volume_db": None}
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(media_path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600
        )
        blob = proc.stderr or ""
        m_mean = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", blob)
        m_max = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", blob)
        if m_mean:
            out["mean_volume_db"] = float(m_mean.group(1))
        if m_max:
            out["max_volume_db"] = float(m_max.group(1))
    except Exception as e:
        print(f"[!] volumedetect failed: {e}")
    return out


def parse_time_str(t_str: str) -> float:
    """Parse string like '01:23' or '00:01:23.5' or '83.5' into seconds."""
    t_str = str(t_str).strip()
    if ":" in t_str:
        parts = t_str.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    return float(t_str)


def get_video_duration(video_path: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path)],
        capture_output=True, text=True
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mn * 60 + s
    raise RuntimeError("Could not determine video duration.")


class ExtractAudioRequest(BaseModel):
    video_path: str


@app.post("/extract-audio")
def extract_audio(req: ExtractAudioRequest):
    video_abs = SHARED_DIR / req.video_path
    if not video_abs.exists():
        raise HTTPException(status_code=404, detail="Video file not found.")

    out_rel = Path("audio") / "source.wav"
    out_abs = SHARED_DIR / out_rel
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["-y", "-i", str(video_abs), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(out_abs)]
    run_ffmpeg(cmd)

    return {"audio_path": out_rel.as_posix()}


class ProbeRequest(BaseModel):
    video_path: str


@app.post("/probe")
def probe_video(req: ProbeRequest):
    """Validate a media file BEFORE the expensive STT/TTS pipeline runs.

    Returns facts only (duration, streams, loudness); the gateway decides policy.
    """
    video_abs = SHARED_DIR / req.video_path
    if not video_abs.exists():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy video: {req.video_path}")

    size = video_abs.stat().st_size
    if size < 10_000:
        raise HTTPException(
            status_code=422,
            detail=f"File chỉ nặng {size} bytes — gần như chắc chắn video tải về bị hỏng hoặc bị chặn giữa chừng."
        )

    try:
        meta = ffprobe_json(video_abs)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Không đọc được metadata (video hỏng?): {e}")

    fmt = meta.get("format") or {}
    streams = meta.get("streams") or []
    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]

    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for s in streams:
            try:
                duration = max(duration, float(s.get("duration") or 0.0))
            except (TypeError, ValueError):
                pass

    if not v_streams:
        raise HTTPException(status_code=422, detail="File không có luồng video (chỉ có audio hoặc rỗng).")
    if duration <= 0.5:
        raise HTTPException(status_code=422, detail=f"Thời lượng video vô lý ({duration:.2f}s) — file gần như chắc chắn đã bị cắt hỏng.")

    first_v, first_a = v_streams[0], (a_streams[0] if a_streams else {})
    loudness = measure_loudness(video_abs) if a_streams else {"mean_volume_db": None, "max_volume_db": None}

    return {
        "duration": round(duration, 3),
        "size_bytes": size,
        "has_video": True,
        "has_audio": bool(a_streams),
        "width": first_v.get("width"),
        "height": first_v.get("height"),
        "video_codec": first_v.get("codec_name"),
        "audio_codec": first_a.get("codec_name"),
        "sample_rate": first_a.get("sample_rate"),
        **loudness
    }


class MergeRequest(BaseModel):
    video_path: str
    segments: list[dict]
    enable_dubbing: bool = True
    enable_subtitle: bool = True


@app.post("/merge")
def merge_video(req: MergeRequest):
    video_abs = SHARED_DIR / req.video_path
    if not video_abs.exists():
        raise HTTPException(status_code=404, detail="Video file not found.")

    video_dur = get_video_duration(video_abs)
    sample_rate = 16000

    output_video = SHARED_DIR / "output" / "dubbed_video.mp4"
    output_video.parent.mkdir(parents=True, exist_ok=True)

    # 1. Build Subtitles if enabled
    srt_path = SHARED_DIR / "data" / "subtitle_vi.srt"
    if req.enable_subtitle:
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        def ts(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = int(t % 60)
            ms = int((t - int(t)) * 1000)
            return f"{h:02}:{m:02}:{s:02},{ms:03}"

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(req.segments, start=1):
                f.write(f"{i}\n{ts(seg['start'])} --> {ts(seg['end'])}\n{seg.get('translation', '')}\n\n")

        srt_str = str(srt_path.resolve()).replace("\\", "/")
        srt_esc = re.sub(r"^([A-Za-z]):", r"\1\\:", srt_str)
        vf_str = (
            f"subtitles='{srt_esc}':"
            "force_style='FontName=FreeSans,FontSize=22,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "Outline=2,Shadow=1,Alignment=2'"
        )
    else:
        vf_str = None

    # 2. Build Dubbed Audio if enabled
    dubbed_wav = SHARED_DIR / "output" / "dubbed_audio.wav"
    if req.enable_dubbing:
        # Align individual audio segments (atempo)
        for seg in req.segments:
            wav_rel = seg.get("audio")
            if not wav_rel:
                continue
            wav_path = SHARED_DIR / wav_rel
            if not wav_path.exists():
                continue
            target_dur = max(0.1, seg["end"] - seg["start"])
            f = sf.SoundFile(str(wav_path))
            cur_dur = len(f) / f.samplerate
            if abs(cur_dur - target_dur) > 0.15 and cur_dur > 0:
                ratio = max(0.85, min(cur_dur / target_dur, 1.15))
                tmp_path = wav_path.with_name(wav_path.stem + "_adj.wav")
                try:
                    run_ffmpeg(["-y", "-i", str(wav_path), "-filter:a", f"atempo={ratio}", str(tmp_path)])
                    tmp_path.replace(wav_path)
                except Exception:
                    pass

        # Build dubbed audio buffer
        total_samples = int(video_dur * sample_rate) + sample_rate
        track = np.zeros(total_samples, dtype=np.float32)

        for seg in req.segments:
            wav_rel = seg.get("audio")
            if not wav_rel:
                continue
            wav_path = SHARED_DIR / wav_rel
            if not wav_path.exists():
                continue
            data, sr = sf.read(str(wav_path), dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            start_sample = int(seg["start"] * sample_rate)
            end_sample = start_sample + len(data)
            if end_sample > len(track):
                data = data[:len(track) - start_sample]
                end_sample = len(track)
            track[start_sample:end_sample] += data

        peak = np.max(np.abs(track)) if len(track) > 0 else 0
        if peak > 1.0:
            track /= peak
        sf.write(str(dubbed_wav), track, samplerate=sample_rate)

    # 3. Assemble FFmpeg mux command according to user mode
    # Bat long tieng la BO TIENG GOC hoan toan. Code cu tron [0:a]volume=0.30
    # voi thuyet minh bang amix, nghia la tieng Trung + nhac nen van nam trong
    # file phanh hanh (chi nho hon) - van douc duoc dau am thanh.
    if req.enable_dubbing:
        cmd = [
            "-y", "-i", str(video_abs), "-i", str(dubbed_wav),
            "-map", "0:v:0", "-map", "1:a:0",
        ]
        if req.enable_subtitle:
            cmd += ["-vf", vf_str, "-c:v", "libx264", "-preset", "fast", "-crf", "18"]
        else:
            cmd += ["-c:v", "copy"]
        cmd += [
            "-af", "volume=2.50,aresample=44100,aformat=channel_layouts=stereo",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_dur:.3f}", "-movflags", "+faststart",
            str(output_video),
        ]

    # Mode C: Only Subtitles (giu tieng goc 100% - nguoi dung tu chon khong long)
    elif not req.enable_dubbing and req.enable_subtitle:
        cmd = [
            "-y", "-i", str(video_abs),
            "-vf", vf_str,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "copy",
            "-t", f"{video_dur:.3f}", "-movflags", "+faststart",
            str(output_video)
        ]

    # Mode D: Neither (simple copy)
    else:
        cmd = ["-y", "-i", str(video_abs), "-c", "copy", str(output_video)]

    run_ffmpeg(cmd)

    return {
        "output_video": output_video.relative_to(SHARED_DIR).as_posix(),
        "srt_path": srt_path.relative_to(SHARED_DIR).as_posix() if req.enable_subtitle else None
    }


class TrimRequest(BaseModel):
    video_path: str = "output/dubbed_video.mp4"
    start_time: str
    end_time: str
    output_filename: str = ""


@app.post("/trim")
def trim_video(req: TrimRequest):
    video_abs = SHARED_DIR / req.video_path
    if not video_abs.exists():
        raise HTTPException(status_code=404, detail=f"Video not found: {req.video_path}")

    start_sec = parse_time_str(req.start_time)
    end_sec = parse_time_str(req.end_time)

    if end_sec <= start_sec:
        raise HTTPException(status_code=400, detail="Thời gian kết thúc phải lớn hơn thời gian bắt đầu.")

    duration = end_sec - start_sec

    fname = req.output_filename.strip() or f"trimmed_{int(time.time())}.mp4"
    if not fname.endswith(".mp4"):
        fname += ".mp4"

    out_rel = Path("output") / fname
    out_abs = SHARED_DIR / out_rel

    cmd = [
        "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", str(video_abs),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_abs)
    ]
    run_ffmpeg(cmd)

    return {
        "trimmed_video": out_rel.as_posix(),
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration": duration
    }


anime_model = None
device = "cpu"

def check_cuda_usable():
    """Verify if CUDA can actually run tensor computations (sm_120 Blackwell check)."""
    import torch
    if not torch.cuda.is_available():
        return False
    try:
        t = torch.zeros(1, device="cuda")
        _ = t + 1
        torch.cuda.synchronize()
        return True
    except Exception as e:
        print(f"[!] CUDA detected but compute kernel unavailable ({e}). Falling back to optimized multi-thread CPU.")
        return False

def get_anime_model(weight_name: str = "paprika"):
    global anime_model, device
    import torch
    torch.set_num_threads(max(1, os.cpu_count() or 4))
    is_cuda = check_cuda_usable()
    device = "cuda" if is_cuda else "cpu"
    print(f"[i] Loading Neural AnimeGANv2 ({weight_name}) on {device.upper()}...")
    try:
        anime_model = torch.hub.load('bryandlee/animegan2-pytorch:main', 'generator', pretrained=weight_name, trust_repo=True)
        anime_model = anime_model.to(device)
        anime_model.eval()
        print(f"[OK] AnimeGANv2 ({weight_name}) loaded successfully on {device.upper()}.")
    except Exception as e:
        print(f"[!] Fallback to CPU due to: {e}")
        device = "cpu"
        anime_model = torch.hub.load('bryandlee/animegan2-pytorch:main', 'generator', pretrained=weight_name, trust_repo=True).to("cpu")
        anime_model.eval()
    return anime_model, device


# ═════════════════════════════════════════════════════════════════════════
# CEL-FLAT 2D — look đã được duyệt trên clip 12s (xử lý đủ 300/300 frame)
# và bộ tẩy chữ burn-in đo từ chính nguồn, không đoán tỷ lệ.
# ═════════════════════════════════════════════════════════════════════════

# Toạ độ đo bằng tools/text_probe.py: watermark bit.ly nằm ở y 12.3-19.1%
# (lệch hoàn toàn so với hộp 8-20% mà code cũ dùng), logo đỏ 牧神記 ở
# x 87.7-98.3% y 88.9-96.3%, phụ đề trắng ở y 90.0-94.7%.
_TEXT_ROIS = {
    # Do bang tools/probe_corner.py (chen luoi 1% len frame goc, doc bang mat).
    # Vi tri overlay co dinh, do tren 4 frame cach nhau 10s:
    #   bilibili logo  : x 84-96%, y 2-10%
    #   bit.ly / YanHK3D (xanh+do): x 70-92%, y 11-18%
    #   logo vang  : x 84-97%, y 86-96%
    # Code cu dat dong bit.ly o goc TREN TRAI (x 3-30%) nen khong frame nao
    # trong sub_out.mp4 mat duoc dong link (tools/audit_corners.py: 151/151
    # frame van con vung sang o goc phai).  Ke ca hop bi lech thi cung khong
    # xoa nham: _overlay_mask chi inpaint diem bao hoa/sang bat duoc.
    "wm_logo":   (0.82, 0.000, 0.98, 0.115),
    "wm_url":    (0.68, 0.095, 0.94, 0.195),
    "botright":  (0.835, 0.855, 1.00, 0.99),
    "subtitle":  (0.08, 0.872, 0.84, 0.968),
}
_WM_KEYS = ("wm_logo", "wm_url", "botright")


def _roi_box(roi, W, H):
    x1, y1, x2, y2 = roi
    return int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H)


def _norm_lum(frame, target=0.42):
    """Kéo mỗi frame về cùng dải sáng trước khi lượng tử màu.

    Không có bước này thì cảnh tối bị ép về gần một màu đen: bảng màu toàn
    clip quá ít tông tối, và Canny trên nền đen vẽ ra nhiễu thay vì nét.
    """
    import cv2
    m = float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()) / 255.0
    gamma = float(np.clip(np.log(target) / np.log(max(m, 0.02)), 0.45, 2.4))
    lut = (np.linspace(0, 1, 256) ** gamma * 255).astype(np.uint8)
    return cv2.LUT(frame, lut)  # OpenCV 5 đòi bảng 256 phần tử, 1 kênh


def _subtitle_glyphs(frame):
    """Tìm chữ trắng có viền đen trong dải phụ đề. MOI CHIU KICH PHAI DO THEO
    TY LE, khong do bang pixel tuyet doi.

    Vi sao: dong 776 resize frame ve (860x492) TRUOC khi render, nen bo loc
    dat o 1080p (h>=40px) vo hieu o 492p - dong chu chi con 23px, phu de khong
    bao gio bi xoa trong output that (do tai giay 90: van nguyen "龙姐...").

    Do truc tiep tren 860x492 va 1920x1080 cua cung tap:
      dong chu that : h/bh = 0.49-0.64  va  w/bw = 0.138-0.462
      diem sang nen : w/bw = 0.017-0.067 (ao sat, co, khuc go) -> duoi 0.08
      bai sang nen  : h/bh = 0.84-1.00  (tran day dai)  -> tren 0.75
    Vuong nhat la do RONG: chu that luon la ca dong (>=8% be ngang dai), diem
    sang nen khong bao gio rong den vay. Nen can 0.08*bw la cat duoc 100% diem
    sang nen o ca hai do phan giai, khong can den tuong phan nua.

    Tuong phan o 492p chi 15-42 (vi 1080p: 110-195) vi downscale tron viền den
    vao than chu -> giu nguong 12 chi de chong truong hop trang tren trang.

    TUONG PHAN PHAI DO CACH CHAN 2-4px, KHONG sat chan 1px. Do tren
    audio/sub_in.mp4 tai 896x512 (tools/diag_ring.py), 19 frame co chu:
      vong 1px   : 18-33, rieng frame 196-224 con -1..4 -> bi tu choi, va chinh
                   dong chu luc 7.8-8.9s van con nguyen trong sub_out.mp4
                   (tools/audit_bright.py: 69/151 frame con diem sang >200)
      vong 2-4px : 95-101 -> tach han, nguong 40
    Ly do: chu nay khong co vien den, chi co hau quang sang; vong 1px nam tron
    trong hau quang do. 19 frame khong co chu: 0 blob dat kich thuoc.
    Nguon goc do lech: dong 749 resize ve 896x512 (860x492 chi la ket qua cua
    crop 96% trong _finalize_mux), nen moi thuoc do deu phai lay theo ty le dai.
    """
    import cv2
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = _roi_box(_TEXT_ROIS["subtitle"], W, H)
    g = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    bh, bw = g.shape[:2]
    pad = max(3, int(round(0.06 * bh)))
    core = cv2.morphologyEx(cv2.inRange(g, 240, 255), cv2.MORPH_CLOSE, np.ones((5, 11), np.uint8))
    m = np.zeros_like(core)
    ri = max(2, int(round(2 * bh / 49)))
    ro = max(3, int(round(4 * bh / 49)))
    for c in cv2.findContours(core, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, w, h = cv2.boundingRect(c)
        if not (0.30 * bh <= h <= 0.80 * bh and 0.08 * bw <= w <= 0.92 * bw):
            continue
        comp = np.zeros_like(core)
        cv2.drawContours(comp, [c], -1, 255, -1)
        inner = cv2.dilate(comp, np.ones((2 * ri + 1, 2 * ri + 1), np.uint8))
        outer = cv2.dilate(comp, np.ones((2 * ro + 1, 2 * ro + 1), np.uint8))
        ring = outer & cv2.bitwise_not(inner)
        if float(g[comp > 0].mean()) - float(g[ring > 0].mean()) < 40:
            continue
        m[max(0, y - pad):min(bh, y + h + pad), max(0, x - pad):min(bw, x + w + pad)] = 255
    return m, (x1, y1)


def _overlay_mask(frame, key):
    """Watermark trong goc: tim theo mau bao hoa (bit.ly xanh+do, logo do 牧神記)
    hoac sang manh (bilibili trang).

    Dung thay cho 'xoac tron hop' vi ly do thuc nghiem: hop dac ~7.6% diem anh
    khiEN TELEA phai dien ca vùng lon (+80ms/frame) va xoa luon noi dung that
    phia sau. Neu khong bat duoc gi thi quay ve xoac ca hop de chac chan.
    """
    import cv2
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = _roi_box(_TEXT_ROIS[key], W, H)
    roi = frame[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    m = (cv2.inRange(hsv, (0, 110, 60), (180, 255, 255))
         | cv2.inRange(g, 185, 255))
    # Logo bilibili la net mong XAM (gray 150-180, bao hoa thap) nen hai nguong
    # tren bo lot: do bang tools/verify_wm.py - frame 196/250 van con nguyen
    # logo. Giai phap: tophat - lay anh sang tru mo cua no voi kernel 15x15,
    # giu lai chi net mong (chu/logo), loai vung sang lon (bau troi, toc).
    th = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, np.ones((15, 15), np.uint8))
    m = m | (cv2.inRange(th, 38, 255) & cv2.inRange(hsv[:, :, 1], 0, 70))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 13), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    keep = [c for c in cnts if cv2.boundingRect(c)[2] >= 8 and cv2.boundingRect(c)[3] >= 7]
    out = np.zeros_like(m)
    cv2.drawContours(out, keep, -1, 255, -1)
    if float(out.mean()) / 255 * 100 < 0.3:
        out[:] = 255
    return out, (x1, y1)


def text_erase_mask(frame):
    """Mask moi chu burn-in: 3 goc do theo mau, dai phu de tim tung glyph."""
    import cv2
    H, W = frame.shape[:2]
    m = np.zeros((H, W), np.uint8)
    parts = [(_overlay_mask(frame, k)) for k in _WM_KEYS]
    parts.append(_subtitle_glyphs(frame))
    for sub, (ox, oy) in parts:
        m[oy:oy + sub.shape[0], ox:ox + sub.shape[1]] = np.maximum(
            m[oy:oy + sub.shape[0], ox:ox + sub.shape[1]], sub)
    return cv2.dilate(m, np.ones((5, 5), np.uint8))


def _static_overlay(video_abs, total, tgt, n=48):
    """Mask chu burn-in KHONG can biet vi tri - phat hien do bat dong theo thoi gian.

    Vi sao phai lam khac _TEXT_ROIS: hop toa do do tay hom truoc dat o goc PHAI
    (x 68-94%) thi tap-106 nay lai dat `bit.ly/YanHK3D` o goc TRAI, nen 11.688
    frame xuat ra van con nguyen (tools/probe_text.sh, cot 2/4/6 cua
    CHK_TEXT.jpg). Ke tap khac doi nguoi dang thi lai lech tiep.

    Cach: lay mau ~48 frame roi khap CA tap va tim nhung pixel MA NET SANG cua
    no xuat hien o dung vi tri do gan nhu moi frame. Noi dung that (toc, dao,
    anh sang) di dong nen net cua no khong trung lap qua 75% frame. Gioi han 2
    dai tren/duoi de khong an nham vat tinh giua khung.

    SU THAT KHAC GIOI (do bang tools/diag_overlay.py tren chinh tap nay): overlay
    la PNG ban trong suot nam tren nen doi lien, nen std THEO THOI GIAN cua gray
    = 42-62 (khong phai ~0). Tieu chi `std gray < 6` chi bat duoc 1/3 logo.
    Do thu hai chieu: gia tri tuyet doi nhay theo nen, nhung TOPHAT (trừ mot
    mo 15x15) thi on dinh vi chu luc nao cung sang hon nen lang ngay no.
    Ty le px co p75(tophat)>25: bit.ly 41.2%, logo vang 32.3%, bilibili 19.0%,
    va 0.0% o vung doi chung (toc nhan vat) -> tach bach, nen dung no.
    """
    import cv2
    idxs = sorted({int(v) for v in np.linspace(0, max(total - 1, 0), min(n, max(total, 1)))})
    cap = cv2.VideoCapture(str(video_abs))
    ths, sats = [], []
    k = np.ones((15, 15), np.uint8)
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, fr = cap.read()
        if not ret:
            continue
        f = cv2.resize(fr, tgt, interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        ths.append(cv2.morphologyEx(g, cv2.MORPH_TOPHAT, k))
        sats.append(cv2.cvtColor(f, cv2.COLOR_BGR2HSV)[:, :, 1])
    cap.release()
    if len(ths) < 10:
        return None
    T = np.stack(ths).astype(np.int16)
    p75 = np.percentile(T, 75, axis=0)
    sat75 = np.percentile(np.stack(sats).astype(np.int16), 75, axis=0)
    # p75 cao = net mong sang xuat hien o DUNG PX nay tren >=25% so frame.
    # KHONG thu p25: do ra bit.ly chi hien ~40% so frame (logo chen theo
    # canh), nen p25>11 = 0.0% va no bi loai hoan toan. rieng p75>26 da tach
    # bach: bit.ly 40.8% | logo vang 31.7% | bilibili 18.9% | toc 0.0%.
    m = ((p75 > 26) & ((sat75 > 60) | (p75 > 45))).astype(np.uint8) * 255
    # Dai giu khung va GIUA DUOI de nguyen: do thu nghiem, cho duoi bat duoc
    # la vung 428x111 px = 15% khung, tuc la DAN PHU DE (chu trang o cung mot
    # y qua nhieu dong nen p75 cung cao). Phu de da co _subtitle_glyphs xu ly
    # rieng tung frame; o day giu lai 2 goc duoi noi logo ty le.
    keep = np.zeros(tgt[::-1], np.uint8)
    keep[:int(0.26 * tgt[1])] = 1
    keep[int(0.78 * tgt[1]):, :int(0.06 * tgt[0])] = 1
    keep[int(0.78 * tgt[1]):, int(0.80 * tgt[0]):] = 1
    m *= keep
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 13), np.uint8))
    m = cv2.dilate(m, np.ones((5, 5), np.uint8)) 
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(m)
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w >= 8 and h >= 6 and w * h >= 90:
            cv2.rectangle(out, (x, y), (x + w, y + h), 255, -1)
    return out if out.any() else None


def erase_text(frame, radius=4, return_mask=False, extra=None):
    import cv2
    m = text_erase_mask(frame)
    if extra is not None:
        # extra la mask TOAN TAP: vung logo co the trong khung hien khong co
        # chu (logo chen theo canh). Xoa ba la mot hop 190x48 cua canh cay
        # thanh TELEA boi nen mot hop nho = thay vung nhoe ro trong frame do.
        # Cong: chi xoa cho extra MA frame nay cung co net sang/bao hoa.
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        th = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, np.ones((15, 15), np.uint8))
        sat = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[:, :, 1]
        live = ((th > 12) | (sat > 80) | (g > 145)).astype(np.uint8) * 255
        live = cv2.dilate(live, np.ones((5, 5), np.uint8))
        m = cv2.max(m, cv2.bitwise_and(extra, live))
    out = cv2.inpaint(frame, m, radius, cv2.INPAINT_TELEA)
    return (out, m) if return_mask else out


class CelFlat2D:
    """Mean-shift phá kết cấu 3D + bảng màu KHOÁ CẢ VIDEO + nét hai độ dày.

    Bảng màu khoá là điểm ăn tiền nhất cho video dài: lượng tử màu độc lập
    từng frame thì màu nhảy liên tục theo thời gian (flicker thấy rõ khi xem
    contact sheet). Lấy mẫu ~90 frame rải khắp tập, chạy kmeans một lần, áp
    cho mọi frame: màu ổn định và nhanh hơn vì không phải kmeans lại.
    """

    def __init__(self, target_wh, seg_wh=None, k=16, mode="C", erase=True,
                 min_line=26, line_w=2):
        self.tgt = tuple(target_wh)
        self.seg = tuple(seg_wh or (target_wh[0] // 2, target_wh[1] // 2))
        self.k, self.mode, self.erase = k, mode, erase
        self.min_line, self.line_w = min_line, line_w
        self.cent = self.lut = None

    def seg_map(self, frame):
        import cv2
        return cv2.pyrMeanShiftFiltering(cv2.resize(frame, self.seg), 10, 22, maxLevel=2)

    def fit(self, samples):
        import cv2
        pix = np.vstack([s.reshape(-1, 3)[::53] for s in samples]).astype(np.float32)
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 25, 0.5)
        _, _, cent = cv2.kmeans(pix, self.k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
        self.cent = np.round(cent).astype(np.uint8)
        r = np.arange(16)
        bins = ((np.stack(np.meshgrid(r, r, r, indexing="ij"), -1).reshape(-1, 3)) * 17).astype(np.int32)
        d = ((bins[:, None, :] - self.cent.astype(np.int32)[None, :, :]) ** 2).sum(-1)
        # LUT 4096 ô: gán màu nhanh gấp ~8 lần so với broadcast HxWxKx3
        self.lut = d.argmin(-1).astype(np.uint8)
        return self

    def fit_from_video(self, video_abs, n=90):
        import cv2
        cap = cv2.VideoCapture(str(video_abs))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        idxs = sorted({int(v) for v in np.linspace(0, max(total - 1, 0), min(n, max(total, 1)))})
        samples = []
        for i in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, f = cap.read()
            if not ok:
                continue
            f = cv2.resize(f, self.tgt, interpolation=cv2.INTER_AREA)
            samples.append(self.seg_map(_norm_lum(erase_text(f) if self.erase else f)))
        cap.release()
        if not samples:
            raise HTTPException(status_code=500, detail="Không lấy được mẫu cho bảng màu 2D")
        return self.fit(samples)

    def render(self, frame):
        import cv2
        emask = None
        if self.erase:
            src, emask = erase_text(frame, return_mask=True)
        else:
            src = frame
        small = self.seg_map(_norm_lum(src))
        q = small >> 4
        idx = (q[:, :, 0].astype(np.uint16) << 8) | (q[:, :, 1].astype(np.uint16) << 4) | q[:, :, 2]
        lab = cv2.medianBlur(self.lut[idx], 5)  # dồn vùng màu cỏn hon 1% -> hết đốm muỗi
        flat = cv2.resize(self.cent[lab], self.tgt, interpolation=cv2.INTER_NEAREST)
        if self.mode == "A":
            flat = cv2.bilateralFilter(flat, 5, 18, 18)
        out = flat.copy()
        g = cv2.GaussianBlur(cv2.cvtColor(flat, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        if emask is not None:
            # KHONG ve net trong vuong chu vua xoa. Inpaint de lai vet ngang;
            # Canny bat vet do thanh net kin va 2D ve lai hinh giong het chu
            # ("bong ma" do tai giay 90 cua tap nay). Nen cua no chi la vot
            # mau phang, khong con dang chu nua.
            keep = cv2.dilate(cv2.resize(emask, self.tgt, interpolation=cv2.INTER_NEAREST),
                              np.ones((9, 9), np.uint8)) == 0
        if self.mode == "C":
            thin = cv2.Canny(g, 95, 180)
            if emask is not None:
                thin = np.where(keep, thin, 0)
            out[thin > 0] = (out[thin > 0] * 0.6).astype(np.uint8)
        cnts, _ = cv2.findContours(cv2.Canny(g, 45, 115), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        # Vẽ lại nét dài bằng drawContours: nét liền mạch deu tay, và drop
        # mọi nét ngắn (frame 260: 692 contour -> giu 338)
        lines = np.zeros((self.tgt[1], self.tgt[0]), np.uint8)
        cv2.drawContours(lines, [c for c in cnts if cv2.arcLength(c, False) >= self.min_line],
                         -1, 255, self.line_w)
        if emask is not None:
            lines = np.where(keep, lines, 0)
        out[lines > 0] = 14
        return out


def _neural_batch(model, dev, imgs, half):
    """AnimeGANv2 cho mot batch anh BGR -> danh sach BGR cung kich thuoc.

    Batch 8 o 896x512 fp16 do bang GPU: 51 ms/frame, gap 4.4 lan so voi chay
    tung frame (225 ms) va gap 7 lan cel-flat.
    """
    import cv2
    import torch
    x = torch.from_numpy(np.stack(
        [cv2.cvtColor(i, cv2.COLOR_BGR2RGB).transpose(2, 0, 1) for i in imgs])).float()
    x = x.to(dev, dtype=torch.float16 if half else torch.float32)
    x = x / 127.5 - 1.0
    with torch.inference_mode():
        y = model(x).clamp(-1, 1).float().cpu().numpy()
    return [cv2.cvtColor(((b + 1) * 127.5).astype(np.uint8).transpose(1, 2, 0),
                         cv2.COLOR_RGB2BGR) for b in y]


def _neural_grade(img, sat=1.10, val=1.03):
    """Nang mau nhe tay.

    Code cu nhan sat *1.35+15 va val *1.2+20 roi AND mask net Canny nen khung
    mat bi toi den va mau loe lo - do la ly do user goi la 'qua xau'. GAN da
    tu no cho mau dep, chi can cham.
    """
    import cv2
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * sat, 0, 255)
    hsv[:, :, 2] = np.clip(hsv[:, :, 2] * val, 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _cel_fit_gan(video_abs, total, tgt, weight, k=20, n=64):
    """Chon bang mau 2D tu ANH DA QUA GAN (khong phai tu video 3D goc).

    Ly do: neu luong tu hoa mau truc tiep tren anh 3D thi bang mau bi cac
    sac do 3D lam ban, va khi chay tung frame độc lap thi mau nhay lien tuc
    (flicker). Fit mot lan tren 64 frame GAN roi = ap cho ca tap: mau co
    dinh, khong loet chong.
    """
    import cv2
    model, dev = get_anime_model(weight)
    half = dev == "cuda"
    if half:
        model.half()
    idxs = sorted({int(v) for v in np.linspace(0, max(total - 1, 0), min(n, max(total, 1)))})
    cap = cv2.VideoCapture(str(video_abs))
    cel = CelFlat2D(tgt, k=max(6, int(k)), erase=False)
    samples, pos = [], iter(idxs)
    want = next(pos, None)
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i == want:
            f = cv2.resize(frame, tgt, interpolation=cv2.INTER_AREA)
            g = _neural_batch(model, dev, [f], half)[0]
            samples.append(cv2.resize(g, cel.seg, interpolation=cv2.INTER_AREA))
            want = next(pos, None)
        i += 1
    cap.release()
    if len(samples) < 8:
        raise HTTPException(status_code=500, detail="Khong lay duoc mau cho bang màu 2D")
    return cel.fit(samples)


def _cel_over(img, cel, detail=0.60, bt=32, ink=None, dark=0.55):
    """Phu lop 2D len anh GAN: mao bang phang + net muc, van giu chi tiet mat.

    So do lan dau (detail .40 / ink 70) ra anh TOT hon nhin thay: net mau
    do den (d > 70) bat ca bien mau cua toc-ao-nen nen 40% khung hinh bi
    to_den, mat ban. Luoi tham so do lai (tools/cel_grid.py) chot:
    detail .60 (giu 60% anh GAN de mat khong bi xoa), bt 32, k=20,
    Tat lop net day. % diem toi 27.0 (GAN) -> 29.0 thay vi 42.8.
    """
    import cv2
    h, w = img.shape[:2]
    small = cv2.bilateralFilter(cv2.resize(img, cel.seg, interpolation=cv2.INTER_AREA),
                                7, 40, 40)
    q = small >> 4
    idx = (q[:, :, 0].astype(np.uint16) << 8) | (q[:, :, 1].astype(np.uint16) << 4) | q[:, :, 2]
    lab = cv2.medianBlur(cel.lut[idx], 5)
    flat = cv2.resize(cel.cent[lab], (w, h), interpolation=cv2.INTER_NEAREST)
    out = cv2.addWeighted(flat, 1.0 - detail, img, detail, 0)
    o = out.astype(np.int16)
    d = np.zeros(o.shape[:2], np.int16)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            s = np.abs(o - np.roll(np.roll(o, dy, 0), dx, 1)).sum(2)
            d = np.maximum(d, s)
    d = np.ascontiguousarray(d.astype(np.uint8))
    d[0:1] = d[-1:] = 0
    d[:, 0:1] = d[:, -1:] = 0
    e = cv2.Canny(cv2.GaussianBlur(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY), (3, 3), 0), 55, 150)
    thin = (e > 0) & (d > bt)
    out[thin] = (out[thin] * dark).astype(np.uint8)
    if ink is not None:
        # Net day chi o bien THAT manh va co huong (d > ink 110), khong phai
        # moi choi mau doi la to den.
        strong = ((d > ink) & (e > 0))
        out[strong] = (out[strong] * 0.30).astype(np.uint8)
    return out


def _render_neural(cap, video_abs, out_abs, req, tgt):
    """Duong 2D bang neural. THU TU BAT BUOC: xoa chu TRUOC khi qua GAN.

    Do bang tools/gan_text.py: frame 196, dai phu de 3.33% diem sang -> sau
    erase 0.03% -> erase roi GAN van 0.00%. Neu GAN truoc roi moi erase thi
    GAN ta lai net chu nhan nhu chu that, bo loc khong nhan ra, con 0.83%.
    """
    import cv2
    import time
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    fps_div = max(1, int(req.fps_div))
    temp_video = SHARED_DIR / "output" / f"temp_visual_{int(time.time())}.mp4"
    out = cv2.VideoWriter(str(temp_video), cv2.VideoWriter_fourcc(*'mp4v'),
                          fps / fps_div, tgt)
    weight = "paprika" if ("paprika" in req.style.lower() or "ghibli" in req.style.lower()) \
        else "face_paint_512_v1"
    model, dev = get_anime_model(weight)
    half = dev == "cuda"
    if half:
        model.half()
    bs = 8 if dev == "cuda" else 2
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    keep_total = max(1, -(-total // fps_div))
    print(f"[neural] {weight} tren {dev.upper()} batch={bs} fp16={half} | "
          f"{keep_total} frame can ve", flush=True)
    cel = None
    ov = None
    if req.remove_watermark:
        t = time.perf_counter()
        ov = _static_overlay(video_abs, total, tgt)
        if ov is None:
            print("[neural] overlay bat dong: khong bat duoc gi (qua it mau)", flush=True)
        else:
            px = int((ov > 0).sum())
            print(f"[neural] overlay bat dong: {px} diem = "
                  f"{px / float(tgt[0] * tgt[1]) * 100:.2f}% khung, "
                  f"{time.perf_counter() - t:.0f}s", flush=True)
    if "cel" in req.style.lower():
        t = time.perf_counter()
        # Thanh "So mau bang mau 2D" tren gia dien phai dieu khien duoc ca
        # duong neural+cel (truoc day no chi anh huong duong cel_flat cu).
        cel = _cel_fit_gan(video_abs, total, tgt, weight, k=int(req.palette_k) or 20)
        print(f"[neural] bam mau 2D fit {time.perf_counter() - t:.0f}s (k={cel.k})",
              flush=True)

    def emit(imgs):
        for o in imgs:
            g = _neural_grade(o)
            out.write(_cel_over(g, cel) if cel is not None else g)
    t0 = time.perf_counter()
    buf, i, frame_count = [], 0, 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if i % fps_div == 0:
            f = cv2.resize(frame, tgt, interpolation=cv2.INTER_AREA)
            if req.remove_watermark:
                f = erase_text(f, extra=ov)
            buf.append(f)
            if len(buf) >= bs:
                emit(_neural_batch(model, dev, buf, half))
                frame_count += len(buf)
                buf = []
                el = time.perf_counter() - t0
                print(f"[neural] {frame_count}/{keep_total} | "
                      f"{el / max(1, frame_count) * 1000:.0f} ms/frame | con "
                      f"{el / max(1, frame_count) * (keep_total - frame_count) / 60:.1f} phut",
                      flush=True)
        i += 1
    for k in range(0, len(buf), bs):
        chunk = buf[k:k + bs]
        emit(_neural_batch(model, dev, chunk, half))
        frame_count += len(chunk)
    cap.release()
    out.release()
    el = time.perf_counter() - t0
    print(f"[neural] xong {frame_count} frame trong {el:.1f}s "
          f"({el / max(1, frame_count) * 1000:.0f} ms/frame)", flush=True)
    return _finalize_mux(temp_video, video_abs, out_abs, req, frame_count)


def _finalize_mux(temp_video: Path, video_abs: Path, out_abs: Path, req, frame_count: int):
    """Re-mux video da render. Mac dinh KHONG lay tieng goc cua nguon."""
    dub_src = SHARED_DIR / req.dub_audio_path if req.dub_audio_path.strip() else None
    using_dub = dub_src is not None and dub_src.exists()

    if using_dub:
        audio_source, audio_kind = dub_src, "thuyet minh"
    elif req.keep_original_audio:
        audio_source, audio_kind = video_abs, "goc"
    else:
        audio_source, audio_kind = None, "khong co"

    video_filter_chain, audio_filter_chain = [], []
    if req.anti_copyright:
        if req.flip_horizontal:
            video_filter_chain.append("hflip")
        video_filter_chain.append("crop=iw*0.96:ih*0.96:(iw-iw*0.96)/2:(ih-ih*0.96)/2,scale=iw:ih")
        video_filter_chain.append("setpts=PTS/1.03")
    elif req.flip_horizontal:
        video_filter_chain.append("hflip")

    if audio_source is not None:
        if req.anti_copyright:
            # setpts/1.03 lam video nhanh 3% nen audio cung phai nhanh 3% kip
            # duc. Voi dub thi KHONG duoc dung asetrate: dubbed_audio.wav noi
            # tai 16 kHz, ep asetrate=45864 la doc nham sample rate -> giong
            # thanh chipmunk. Pitch-shift chi la thu lan tra dau cua tieng goc
            # nguoi khac, ap len giong cua minh thi vo nghia.
            audio_filter_chain.append(
                "atempo=1.03" if using_dub else "asetrate=44100*1.04,atempo=1.03/1.04")
        if using_dub:
            audio_filter_chain.append("volume=2.50,aresample=44100,aformat=channel_layouts=stereo")

    cmd_mux = ["-y", "-i", str(temp_video)]
    if audio_source is not None:
        cmd_mux.extend(["-i", str(audio_source)])
    cmd_mux.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "18"])
    if video_filter_chain:
        cmd_mux.extend(["-filter:v", ",".join(video_filter_chain)])
    if audio_filter_chain:
        cmd_mux.extend(["-filter:a", ",".join(audio_filter_chain)])
    cmd_mux.extend(["-map", "0:v:0"])
    if audio_source is not None:
        cmd_mux.extend(["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-shortest"])
    else:
        cmd_mux.append("-an")
    cmd_mux.append(str(out_abs))
    try:
        run_ffmpeg(cmd_mux)
    except HTTPException as e:
        # run_ffmpeg nem HTTPException, tung bi `except Exception` nuot mat nen
        # loi FFmpeg that khong bao gio thoat ra ngoai va temp_visual_*.mp4 bi
        # bo roi day volume. Van giu ban khong-loc de khoi mat 40 phut render,
        # nhung phai in nguyen nhan ra log.
        print(f"[!] Mux that bai, xuat video khong co loc: {e.detail}", flush=True)
        try:
            shutil.copy(temp_video, out_abs)
        except OSError as ce:
            print(f"[!] Cung khong copy duoc: {ce}", flush=True)

    temp_video.unlink(missing_ok=True)
    return {
        "restyle_video": (Path("output") / out_abs.name).as_posix(),
        "frames_processed": frame_count,
        "style": req.style,
        "audio_kind": audio_kind,
    }


class RestyleRequest(BaseModel):
    video_path: str
    style: str = "animegan_2d"  # animegan_2d, anime_2d, comic_ink
    output_filename: str = ""
    remove_watermark: bool = True  # Tự động xóa logo bit.ly, Bilibili, chữ Trung Quốc
    anti_copyright: bool = True    # Bật lá chắn chống bản quyền: Zoom nhẹ 104%, Speed 1.03x, Audio Pitch-shift
    flip_horizontal: bool = False  # Lật gương hình ảnh ngang
    dub_audio_path: str = ""       # Duong dan file thuyet minh thay cho tieng goc
    keep_original_audio: bool = False  # Chi bat khi user co chu dich giu lai tieng goc
    fps_div: int = 1               # 2 = chay 12.5fps kieu anime "draw on twos", nhanh gap doi
    palette_k: int = 16            # Số màu của bảng màu khoá cho cả video (chỉ dùng cho cel_flat)


@app.post("/restyle-2d")
def restyle_video_2d(req: RestyleRequest):
    """Chuyen video 3D thanh 2D. Cel-Flat = phan vung mau va net duoc ve lai o
    tung frame; khong phai toi mau len frame 3D, va cung khong ve lai hinh hoc
    khuon mat (muon vay can diffusion)."""
    import cv2
    import torch

    video_abs = SHARED_DIR / req.video_path
    if not video_abs.exists():
        raise HTTPException(status_code=404, detail=f"Video not found: {req.video_path}")

    fname = req.output_filename.strip() or f"anime_2d_{int(time.time())}.mp4"
    if not fname.endswith(".mp4"):
        fname += ".mp4"

    out_rel = Path("output") / fname
    out_abs = SHARED_DIR / out_rel

    temp_video = SHARED_DIR / "output" / f"temp_visual_{int(time.time())}.mp4"

    cap = cv2.VideoCapture(str(video_abs))
    if not cap.isOpened():
        raise HTTPException(status_code=500, detail="Cannot read video stream.")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Scale to standard 720p 16:9 for clean neural transformation
    target_w, target_h = 896, 512
    # Duong neural (AnimeGANv2): tao writer cua rieng nen phe phai nam TRUOC
    # khi mo cv2.VideoWriter chung, neu khong se rot lai 1 file temp 0 frame.
    # "anime 2d" = GAN + lop mau phang 2D de len (cel), van di chung duong nay.
    stl = req.style.lower()
    if "neural" in stl or "animegan" in stl or "anime 2d" in stl:
        return _render_neural(cap, video_abs, out_abs, req, (target_w, target_h))
    cel_flat = "cel flat" in req.style.lower() or "cel_flat" in req.style.lower()
    # "draw on twos": 12.5fps vua la cach ve cua anime 2D, vua giam nua thoi gian render
    fps_div = max(1, int(req.fps_div)) if cel_flat else 1
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(str(temp_video), fourcc, fps / fps_div, (target_w, target_h))

    if cel_flat:
        t0 = time.perf_counter()
        renderer = CelFlat2D((target_w, target_h), k=max(6, int(req.palette_k)),
                             erase=req.remove_watermark).fit_from_video(video_abs)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        keep_total = max(1, -(-total // fps_div))
        print(f"[cel-flat] bang mau {renderer.k} mau, lan lay mau: "
              f"{time.perf_counter() - t0:.1f}s | {keep_total} frame can ve", flush=True)
        t0 = time.perf_counter()
        frame_count, i = 0, 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if i % fps_div == 0:
                out.write(renderer.render(cv2.resize(frame, (target_w, target_h),
                                                     interpolation=cv2.INTER_AREA)))
                frame_count += 1
                if frame_count % 500 == 0:
                    el = time.perf_counter() - t0
                    print(f"[cel-flat] {frame_count}/{keep_total} frame | "
                          f"{el / frame_count * 1000:.0f} ms/frame | con "
                          f"{el / frame_count * (keep_total - frame_count) / 60:.0f} phut", flush=True)
            i += 1
        el = time.perf_counter() - t0
        print(f"[cel-flat] xong {frame_count} frame trong {el:.1f}s "
              f"({el / max(1, frame_count) * 1000:.0f} ms/frame, chua doc+resize+ghi)", flush=True)
        cap.release()
        out.release()
        return _finalize_mux(temp_video, video_abs, out_abs, req, frame_count)

    # Pre-build watermark inpainting mask at target resolution
    wm_mask = None
    if req.remove_watermark:
        wm_mask = np.zeros((target_h, target_w), dtype=np.uint8)
        # Ba hop watermark: logo bilibili (x 82-98%, y 0-11.5%), dong bit.ly
        # (x 68-94%, y 9.5-19.5%), logo vang 牧神記 (x 83.5-100%, y 85.5-99%).
        # Toa do do lai bang tools/probe_corner.py - truoc day dat nham dong
        # bit.ly o goc tren trai nen duong nay khong bao gio che duoc link.
        for _key in _WM_KEYS:
            _x1, _y1, _x2, _y2 = _roi_box(_TEXT_ROIS[_key], target_w, target_h)
            wm_mask[_y1:_y2, _x1:_x2] = 255

    use_neural = "animegan" in req.style.lower() or "neural" in req.style.lower() or req.style == "animegan_2d" or "2d" in req.style.lower()
    weight_choice = "paprika" if "paprika" in req.style.lower() or "ghibli" in req.style.lower() or req.style == "animegan_2d" else "face_paint_512_v1"
    model, dev = get_anime_model(weight_choice) if use_neural else (None, "cpu")

    frame_count = 0
    batch_size = 12 if dev == "cuda" else 2

    def postprocess_frame(args):
        anime_bgr, frame_orig = args
        # 1. Clean Manga Canny Edge extraction
        gray = cv2.cvtColor(frame_orig, cv2.COLOR_BGR2GRAY)
        canny = cv2.Canny(cv2.GaussianBlur(gray, (7, 7), 0), 45, 115)
        kernel = np.ones((2, 2), np.uint8)
        canny = cv2.dilate(canny, kernel, iterations=1)
        edge_mask = cv2.bitwise_not(canny)
        edge_3ch = cv2.cvtColor(edge_mask, cv2.COLOR_GRAY2BGR)

        # 2. Composite with AnimeGAN 2D
        combined = cv2.bitwise_and(anime_bgr, edge_3ch)

        # 3. Boost saturation and contrast for hand-drawn anime cel look
        hsv = cv2.cvtColor(combined, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.35 + 15, 0, 255)
        hsv[:, :, 2] = np.clip(hsv[:, :, 2] * 1.2 + 20, 0, 255)
        return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    from concurrent.futures import ThreadPoolExecutor
    worker_threads = min(8, os.cpu_count() or 4)

    with torch.no_grad(), ThreadPoolExecutor(max_workers=worker_threads) as executor:
        batch_frames = []
        batch_orig = []

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_resized = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)

            # Inpaint watermarks / logos if enabled
            if wm_mask is not None:
                frame_resized = cv2.inpaint(frame_resized, wm_mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
                # Dai phu de chua chan nhan vat nen khong the dung mask tinh:
                # tim glyph tung frame roi moi toa.
                sub, (ox, oy) = _subtitle_glyphs(frame_resized)
                if sub.any():
                    band = frame_resized[oy:oy + sub.shape[0], ox:ox + sub.shape[1]]
                    frame_resized[oy:oy + sub.shape[0], ox:ox + sub.shape[1]] = cv2.inpaint(
                        band, sub, 4, cv2.INPAINT_TELEA)

            if use_neural and model is not None:
                rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                batch_frames.append(rgb.transpose((2, 0, 1)))
                batch_orig.append(frame_resized)

                if len(batch_frames) >= batch_size:
                    # Parallel GPU Tensor inference on Batch of frames
                    t_in = torch.from_numpy(np.array(batch_frames)).float().to(dev) / 127.5 - 1.0
                    t_out = model(t_in)
                    anime_arr = ((t_out.clamp(-1, 1) + 1.0) * 127.5).byte().cpu().numpy().transpose((0, 2, 3, 1))

                    anime_bgr_list = [cv2.cvtColor(img, cv2.COLOR_RGB2BGR) for img in anime_arr]
                    # Multi-threaded edge & composition pipeline across CPU threads
                    final_frames = list(executor.map(postprocess_frame, zip(anime_bgr_list, batch_orig)))
                    for f in final_frames:
                        out.write(f)
                    frame_count += len(final_frames)
                    batch_frames.clear()
                    batch_orig.clear()
            else:
                div = 36
                lut = np.arange(256, dtype=np.uint8) // div * div + div // 2
                color_quant = cv2.LUT(frame_resized, lut)
                color_flat = cv2.bilateralFilter(color_quant, 9, 85, 85)
                gray = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2GRAY)
                edges = cv2.adaptiveThreshold(cv2.medianBlur(gray, 5), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 9)
                edges_3ch = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
                out.write(cv2.bitwise_and(color_flat, edges_3ch))
                frame_count += 1

        # Process remaining frames in batch
        if batch_frames and use_neural and model is not None:
            t_in = torch.from_numpy(np.array(batch_frames)).float().to(dev) / 127.5 - 1.0
            t_out = model(t_in)
            anime_arr = ((t_out.clamp(-1, 1) + 1.0) * 127.5).byte().cpu().numpy().transpose((0, 2, 3, 1))
            anime_bgr_list = [cv2.cvtColor(img, cv2.COLOR_RGB2BGR) for img in anime_arr]
            final_frames = list(executor.map(postprocess_frame, zip(anime_bgr_list, batch_orig)))
            for f in final_frames:
                out.write(f)
            frame_count += len(final_frames)

    cap.release()
    out.release()
    return _finalize_mux(temp_video, video_abs, out_abs, req, frame_count)


@app.get("/health")
def health():
    return {"status": "ok", "service": "video"}
