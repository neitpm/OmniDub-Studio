# main.py – Monolith pipeline & Gradio UI for Video Dubbing & Subtitling (zh/en -> vi)
"""
Chinese/English -> Vietnamese dubbing & subbing application with Gradio UI & CLI.
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
from pathlib import Path
import re

# Add CUDA DLL directories to PATH
def _register_cuda12_dlls():
    try:
        import site
        for sp in site.getsitepackages():
            sp_path = Path(sp)
            for nvidia_bin in sp_path.glob("nvidia/*/bin"):
                if nvidia_bin.is_dir():
                    try:
                        os.add_dll_directory(str(nvidia_bin))
                    except Exception:
                        pass
                    os.environ["PATH"] = str(nvidia_bin) + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:
        print(f"[!] Warning registering CUDA DLL dirs: {e}")

_register_cuda12_dlls()

import torch
from faster_whisper import WhisperModel
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, VitsModel, AutoProcessor
import soundfile as sf
import numpy as np
import yt_dlp
import gradio as gr


def check_ffmpeg():
    """Verify ffmpeg binary is available."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise RuntimeError("FFmpeg not found in system PATH. Please install FFmpeg.") from e


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def run_ffmpeg(cmd: list[str]):
    """Run an ffmpeg command."""
    subprocess.run(["ffmpeg", *cmd], check=True)


def clean_workspace(workspace: Path):
    """Clean intermediate files to avoid reusing stale data."""
    for sub in ["segments", "audio", "data"]:
        p = workspace / sub
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    (workspace / "output").mkdir(parents=True, exist_ok=True)


def download_youtube_video(url: str, download_dir: Path) -> Path:
    """Download video from YouTube using yt-dlp."""
    ensure_dir(download_dir)
    out_template = str(download_dir / "downloaded_video.%(ext)s")
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "overwrites": True,
        "quiet": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        p = Path(filename)
        if not p.exists():
            mp4_p = p.with_suffix(".mp4")
            if mp4_p.exists():
                return mp4_p
            candidates = list(download_dir.glob("downloaded_video.*"))
            if candidates:
                return candidates[0]
        return p


def extract_audio(video_path: Path, out_wav: Path):
    """Extract audio track as 16kHz mono WAV for STT."""
    ensure_dir(out_wav.parent)
    cmd = ["-y", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(out_wav)]
    run_ffmpeg(cmd)
    print(f"[OK] Audio extracted to {out_wav}")


def load_whisper_model():
    """Load Whisper model with GPU fallback."""
    for device, compute in [("cuda", "int8_float16"), ("cuda", "int8"), ("cpu", "int8")]:
        try:
            model = WhisperModel("medium", device=device, compute_type=compute)
            print(f"[OK] Loaded Whisper medium ({compute}) on {device}")
            return model
        except Exception as e:
            print(f"[!] Whisper medium on {device}/{compute} failed: {e}")
    raise RuntimeError("Could not load Whisper model.")


def transcribe(audio_path: Path, model, source_lang: str = "auto") -> tuple[list[dict], str]:
    """Transcribe audio with timestamp alignment."""
    lang_param = None if source_lang == "auto" else source_lang
    segments_gen, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        language=lang_param,
    )
    detected_lang = info.language
    print(f"[i] Detected language: {detected_lang} (prob: {info.language_probability:.2f})")
    segments = []
    for seg in segments_gen:
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (seg.words or [])]
        })
    return segments, detected_lang


def load_translation_pipeline(lang: str):
    """Load translation model depending on source language."""
    if lang == "en":
        model_name = "Helsinki-NLP/opus-mt-en-vi"
    else:
        model_name = "Helsinki-NLP/opus-mt-zh-vi"
    print(f"[i] Loading translation model: {model_name}")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name).to(device)
    return tokenizer, model


def translate_segments(segments: list[dict], translation_bundle, batch_size: int = 16) -> list[dict]:
    """Batch translation into Vietnamese."""
    tokenizer, model = translation_bundle
    device = "cuda" if torch.cuda.is_available() else "cpu"
    texts = [s["text"] for s in segments]
    translations = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        inputs = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=512).to(device)
        with torch.no_grad():
            outputs = model.generate(**inputs, max_new_tokens=512)
        decoded = tokenizer.batch_decode(outputs, skip_special_tokens=True)
        translations.extend(decoded)
    for seg, trans in zip(segments, translations):
        seg["translation"] = trans.strip()
    return segments


def load_mms_tts():
    """Load Vietnamese MMS TTS model."""
    model_name = "facebook/mms-tts-vie"
    print(f"[i] Loading TTS model: {model_name}")
    processor = AutoProcessor.from_pretrained(model_name)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    vits = VitsModel.from_pretrained(model_name).to(device)
    return processor, vits


def synthesize_segment(text: str, processor, vits, output_path: Path):
    """Generate TTS audio for a single segment."""
    if not text.strip():
        # Empty text -> write tiny silent wav
        sf.write(str(output_path), np.zeros(1600, dtype=np.float32), samplerate=16000)
        return
    device = "cuda" if torch.cuda.is_available() else "cpu"
    inputs = processor(text=text, return_tensors="pt").to(device)
    with torch.no_grad():
        audio = vits(**inputs).waveform.squeeze().cpu().numpy()
    sf.write(str(output_path), audio, samplerate=16000)


def tts_segments(segments: list[dict], processor, vits, segments_dir: Path) -> list[dict]:
    """Synthesize speech for all segments."""
    ensure_dir(segments_dir)
    for idx, seg in enumerate(segments, start=1):
        wav_path = segments_dir / f"{idx:04d}.wav"
        synthesize_segment(seg["translation"], processor, vits, wav_path)
        seg["audio"] = str(wav_path)
    return segments


def get_wav_duration(path: Path) -> float:
    f = sf.SoundFile(str(path))
    return len(f) / f.samplerate


def align_timestamps(segments: list[dict]) -> list[Path]:
    """Light time-stretch (atempo 0.85-1.15) if duration differs."""
    aligned_files = []
    for seg in segments:
        wav_path = Path(seg["audio"])
        target_dur = max(0.1, seg["end"] - seg["start"])
        cur_dur = get_wav_duration(wav_path)
        if abs(cur_dur - target_dur) > 0.15 and cur_dur > 0:
            ratio = cur_dur / target_dur
            ratio = max(0.85, min(ratio, 1.15))
            tmp_path = wav_path.with_name(wav_path.stem + "_adj.wav")
            cmd = ["-y", "-i", str(wav_path), "-filter:a", f"atempo={ratio}", str(tmp_path)]
            try:
                run_ffmpeg(cmd)
                os.replace(tmp_path, wav_path)
            except Exception as e:
                print(f"[!] Warning atempo adjustment failed: {e}")
        aligned_files.append(wav_path)
    return aligned_files


def get_video_duration(video_path: Path) -> float:
    """Return video duration in seconds."""
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path)],
        capture_output=True, text=True
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mn * 60 + s
    raise RuntimeError("Could not determine video duration.")


def build_dubbed_audio(segments: list[dict], video_dur: float, out_wav: Path, sample_rate: int = 16000):
    """Assemble all speech segments onto a timeline using numpy overlay."""
    total_samples = int(video_dur * sample_rate) + sample_rate
    track = np.zeros(total_samples, dtype=np.float32)

    for seg in segments:
        wav_path = seg.get("audio")
        if not wav_path or not Path(wav_path).exists():
            continue
        data, sr = sf.read(str(wav_path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != sample_rate:
            continue
        start_sample = int(seg["start"] * sample_rate)
        end_sample = start_sample + len(data)
        if end_sample > len(track):
            data = data[:len(track) - start_sample]
            end_sample = len(track)
        track[start_sample:end_sample] += data

    # Normalize if needed
    peak = np.max(np.abs(track)) if len(track) > 0 else 0
    if peak > 1.0:
        track /= peak

    sf.write(str(out_wav), track, samplerate=sample_rate)
    print(f"[OK] Dubbed audio built: {out_wav} ({video_dur:.2f}s, {np.count_nonzero(track)} non-zero samples)")


def write_srt(segments: list[dict], srt_path: Path):
    """Write Vietnamese subtitle SRT file."""
    def ts(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start = seg["start"]
            end = seg["end"]
            text = seg.get("translation", "").strip()
            f.write(f"{i}\n{ts(start)} --> {ts(end)}\n{text}\n\n")
    print(f"[OK] Vietnamese subtitle written to {srt_path}")


def merge_final_video(video_path: Path, dubbed_wav: Path, srt_path: Path, output_video: Path):
    """
    Burn subtitles and mix:
    - Original background sound kept softly (30% volume)
    - Vietnamese dubbing speech clear and boosted (250% volume)
    """
    video_dur = get_video_duration(video_path)
    srt_abs = str(srt_path.resolve()).replace("\\", "/")
    srt_esc = re.sub(r"^([A-Za-z]):", r"\1\\:", srt_abs)

    vf = (
        f"subtitles='{srt_esc}':"
        "force_style='FontName=Arial,FontSize=22,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "Outline=2,Shadow=1,Alignment=2'"
    )

    # Check if original video has an audio stream
    probe = subprocess.run(["ffmpeg", "-i", str(video_path)], capture_output=True, text=True)
    has_orig_audio = "Audio:" in probe.stderr

    if has_orig_audio:
        filter_complex = (
            f"[0:v]{vf}[vout];"
            f"[0:a]volume=0.30,aformat=channel_layouts=stereo:sample_rates=44100[bg];"
            f"[1:a]volume=2.50,aformat=channel_layouts=stereo:sample_rates=44100[speech];"
            f"[bg][speech]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
        cmd = [
            "-y",
            "-i", str(video_path),
            "-i", str(dubbed_wav),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_dur:.3f}",
            "-movflags", "+faststart",
            str(output_video)
        ]
    else:
        cmd = [
            "-y",
            "-i", str(video_path),
            "-i", str(dubbed_wav),
            "-vf", vf,
            "-af", "volume=2.50,aformat=channel_layouts=stereo:sample_rates=44100",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_dur:.3f}",
            "-movflags", "+faststart",
            str(output_video)
        ]

    run_ffmpeg(cmd)
    print(f"[OK] Dubbed & Subtitled video saved to {output_video}")


def run_pipeline(video_input: str, url_input: str, lang_choice: str, progress=gr.Progress()) -> str:
    """Main orchestration pipeline from URL/local video to final dubbed & subbed MP4."""
    check_ffmpeg()
    workspace = Path("D:/translate")
    clean_workspace(workspace)

    data_dir = workspace / "data"
    audio_dir = workspace / "audio"
    seg_dir = workspace / "segments"
    out_dir = workspace / "output"
    output_video = out_dir / "dubbed_video.mp4"

    # Step 1: Resolve video source
    progress(0.05, desc="Chuẩn bị video...")
    if url_input and url_input.strip():
        print(f"[i] Downloading video from URL: {url_input}")
        video_path = download_youtube_video(url_input.strip(), audio_dir)
    elif video_input:
        video_path = Path(video_input)
    else:
        raise ValueError("Vui lòng cung cấp URL YouTube hoặc chọn file video local.")

    if not video_path.exists():
        raise FileNotFoundError(f"Không tìm thấy video tại: {video_path}")

    # Step 2: Extract audio
    progress(0.15, desc="Tách audio từ video...")
    source_wav = audio_dir / "source.wav"
    extract_audio(video_path, source_wav)

    # Step 3: STT
    progress(0.25, desc="Nhận dạng giọng nói (Whisper STT)...")
    whisper_model = load_whisper_model()
    source_lang_code = "zh" if lang_choice == "Tiếng Trung" else ("en" if lang_choice == "Tiếng Anh" else "auto")
    segments, detected_lang = transcribe(source_wav, whisper_model, source_lang=source_lang_code)

    if not segments:
        raise RuntimeError("Không nhận diện được giọng nói trong video.")

    with open(data_dir / "transcript.json", "w", encoding="utf-8") as f:
        json.dump({"segments": segments, "detected_lang": detected_lang}, f, ensure_ascii=False, indent=2)

    # Step 4: Translate
    progress(0.45, desc="Dịch sang tiếng Việt...")
    trans_lang = "en" if (source_lang_code == "en" or (source_lang_code == "auto" and detected_lang == "en")) else "zh"
    trans_bundle = load_translation_pipeline(trans_lang)
    segments = translate_segments(segments, trans_bundle)

    with open(data_dir / "translation.json", "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f, ensure_ascii=False, indent=2)

    srt_path = data_dir / "subtitle_vi.srt"
    write_srt(segments, srt_path)

    # Step 5: TTS
    progress(0.65, desc="Tổng hợp giọng nói tiếng Việt (MMS TTS)...")
    tts_processor, vits_model = load_mms_tts()
    segments = tts_segments(segments, tts_processor, vits_model, seg_dir)
    audio_paths = align_timestamps(segments)
    for seg, wav in zip(segments, audio_paths):
        seg["audio"] = str(wav)

    # Step 6: Assemble audio timeline
    progress(0.85, desc="Đồng bộ timeline âm thanh...")
    video_dur = get_video_duration(video_path)
    dubbed_wav = out_dir / "dubbed_audio.wav"
    build_dubbed_audio(segments, video_dur, dubbed_wav)

    # Step 7: Burn subtitle & Mux video
    progress(0.92, desc="Ghép audio thuyết minh & đốt Việt sub vào video...")
    merge_final_video(video_path, dubbed_wav, srt_path, output_video)

    progress(1.0, desc="Hoàn tất!")
    return str(output_video)


def create_gradio_ui():
    """Build the Gradio interface."""
    theme = gr.themes.Soft(primary_hue="blue", secondary_hue="indigo")
    with gr.Blocks(title="AI Video Dubbing & Subtitling (Trung/Anh -> Việt)") as demo:
        gr.Markdown(
            """
            # 🎬 AI Video Dubbing & Subtitle (Trung/Anh ➔ Tiếng Việt)
            Tự động tải video, nhận dạng giọng nói, dịch thuật, lồng tiếng thuyết minh và gắn Vietsub trực tiếp.
            """
        )

        with gr.Row():
            with gr.Column(scale=1):
                url_box = gr.Textbox(
                    label="1. Nhập URL YouTube (nếu có)",
                    placeholder="https://www.youtube.com/watch?v=...",
                    lines=1
                )
                local_video = gr.Video(
                    label="Hoặc chọn file Video từ máy",
                    sources=["upload"]
                )
                lang_choice = gr.Radio(
                    choices=["Tự động nhận diện", "Tiếng Trung", "Tiếng Anh"],
                    value="Tự động nhận diện",
                    label="2. Ngôn ngữ nguồn của video"
                )
                submit_btn = gr.Button("🚀 Tạo video thuyết minh + Việt sub", variant="primary", size="lg")

            with gr.Column(scale=1):
                out_video = gr.Video(label="Video kết quả (Có Vietsub & Thuyết minh)", interactive=False)
                status_box = gr.Textbox(label="Đường dẫn file kết quả", interactive=False)

        def process(local_vid, url, lang):
            try:
                res_path = run_pipeline(local_vid, url, lang)
                return res_path, f"Thành công! Video đã được lưu tại:\n{res_path}"
            except Exception as e:
                import traceback
                traceback.print_exc()
                return None, f"Lỗi: {str(e)}"

        submit_btn.click(
            fn=process,
            inputs=[local_video, url_box, lang_choice],
            outputs=[out_video, status_box]
        )

    return demo


def cli_main():
    parser = argparse.ArgumentParser(description="Chinese/English to Vietnamese Dubbing & Subtitling MVP")
    parser.add_argument("--video", help="Path to local input video")
    parser.add_argument("--url", help="YouTube URL to download & process")
    parser.add_argument("--lang", default="auto", choices=["auto", "zh", "en"], help="Source language")
    parser.add_argument("--ui", action="store_true", help="Launch Gradio web UI")
    parser.add_argument("--port", type=int, default=7860, help="Gradio UI port")
    parser.add_argument("--share", action="store_true", help="Create public Gradio link")
    args = parser.parse_args()

    # Default to UI mode if no command-line video/url is supplied
    if args.ui or (not args.video and not args.url):
        print(f"[i] Starting Gradio UI on port {args.port}...")
        ui = create_gradio_ui()
        ui.launch(server_name="127.0.0.1", server_port=args.port, share=args.share)
    else:
        lang_map = {"auto": "Tự động nhận diện", "zh": "Tiếng Trung", "en": "Tiếng Anh"}
        out = run_pipeline(args.video, args.url, lang_map.get(args.lang, "Tự động nhận diện"))
        print(f"[OK] Pipeline completed: {out}")


if __name__ == "__main__":
    cli_main()
