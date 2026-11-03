"""Video & Audio Processing Microservice (FFmpeg & Timeline Muxing)."""

import os
import re
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
    subprocess.run(["ffmpeg", *cmd], check=True)


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

    probe = subprocess.run(["ffmpeg", "-i", str(video_abs)], capture_output=True, text=True)
    has_orig_audio = "Audio:" in probe.stderr

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
    # Mode A: Both Dubbing and Subtitles
    if req.enable_dubbing and req.enable_subtitle:
        if has_orig_audio:
            filter_complex = (
                f"[0:v]{vf_str}[vout];"
                f"[0:a]volume=0.30,aformat=channel_layouts=stereo:sample_rates=44100[bg];"
                f"[1:a]volume=2.50,aformat=channel_layouts=stereo:sample_rates=44100[speech];"
                f"[bg][speech]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
            cmd = [
                "-y", "-i", str(video_abs), "-i", str(dubbed_wav),
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{video_dur:.3f}", "-movflags", "+faststart",
                str(output_video)
            ]
        else:
            cmd = [
                "-y", "-i", str(video_abs), "-i", str(dubbed_wav),
                "-vf", vf_str,
                "-af", "volume=2.50,aformat=channel_layouts=stereo:sample_rates=44100",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{video_dur:.3f}", "-movflags", "+faststart",
                str(output_video)
            ]

    # Mode B: Only Dubbing (no vietsub burned)
    elif req.enable_dubbing and not req.enable_subtitle:
        if has_orig_audio:
            filter_complex = (
                f"[0:a]volume=0.30,aformat=channel_layouts=stereo:sample_rates=44100[bg];"
                f"[1:a]volume=2.50,aformat=channel_layouts=stereo:sample_rates=44100[speech];"
                f"[bg][speech]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
            cmd = [
                "-y", "-i", str(video_abs), "-i", str(dubbed_wav),
                "-filter_complex", filter_complex,
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{video_dur:.3f}", "-movflags", "+faststart",
                str(output_video)
            ]
        else:
            cmd = [
                "-y", "-i", str(video_abs), "-i", str(dubbed_wav),
                "-map", "0:v:0", "-map", "1:a:0",
                "-af", "volume=2.50,aformat=channel_layouts=stereo:sample_rates=44100",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{video_dur:.3f}", "-movflags", "+faststart",
                str(output_video)
            ]

    # Mode C: Only Subtitles (keep original audio 100%)
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

def get_anime_model():
    global anime_model, device
    if anime_model is None:
        import torch
        torch.set_num_threads(max(1, os.cpu_count() or 4))
        is_cuda = check_cuda_usable()
        device = "cuda" if is_cuda else "cpu"
        print(f"[i] Loading Neural AnimeGANv2 model on {device.upper()}...")
        try:
            anime_model = torch.hub.load('bryandlee/animegan2-pytorch:main', 'generator', pretrained='face_paint_512_v2', trust_repo=True)
            anime_model = anime_model.to(device)
            anime_model.eval()
            print(f"[OK] AnimeGANv2 loaded successfully on {device.upper()}.")
        except Exception as e:
            print(f"[!] Fallback to CPU due to: {e}")
            device = "cpu"
            anime_model = torch.hub.load('bryandlee/animegan2-pytorch:main', 'generator', pretrained='face_paint_512_v2', trust_repo=True).to("cpu")
            anime_model.eval()
    return anime_model, device


class RestyleRequest(BaseModel):
    video_path: str
    style: str = "animegan_2d"  # animegan_2d, anime_2d, comic_ink
    output_filename: str = ""


@app.post("/restyle-2d")
def restyle_video_2d(req: RestyleRequest):
    """Transform 3D video frames into genuine 2D hand-drawn anime using AnimeGANv2 Neural Network."""
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
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(str(temp_video), fourcc, fps, (target_w, target_h))

    use_neural = "animegan" in req.style.lower() or "neural" in req.style.lower() or req.style == "animegan_2d"
    model, dev = get_anime_model() if use_neural else (None, "cpu")

    frame_count = 0
    batch_size = 12 if dev == "cuda" else 2

    def postprocess_frame(args):
        anime_bgr, frame_orig = args
        gray = cv2.cvtColor(frame_orig, cv2.COLOR_BGR2GRAY)
        edges = cv2.adaptiveThreshold(cv2.medianBlur(gray, 5), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 8)
        edges_3ch = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
        return cv2.bitwise_and(anime_bgr, edges_3ch)

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

    # Re-mux with original audio using FFmpeg
    cmd_mux = [
        "-y",
        "-i", str(temp_video),
        "-i", str(video_abs),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_abs)
    ]
    try:
        run_ffmpeg(cmd_mux)
    except Exception as e:
        shutil.copy(temp_video, out_abs)

    temp_video.unlink(missing_ok=True)

    return {
        "restyle_video": out_rel.as_posix(),
        "frames_processed": frame_count,
        "style": req.style
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "video"}
