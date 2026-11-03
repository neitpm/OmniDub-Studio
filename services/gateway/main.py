"""Gateway / Orchestrator service: Web UI (Gradio) & Workflow Controller."""

import os
import shutil
import time
from pathlib import Path
import requests
import yt_dlp
import gradio as gr

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared_data"))
STT_URL = os.environ.get("STT_URL", "http://stt-service:8001")
TRANS_URL = os.environ.get("TRANS_URL", "http://translation-service:8002")
TTS_URL = os.environ.get("TTS_URL", "http://tts-service:8003")
VIDEO_URL = os.environ.get("VIDEO_URL", "http://video-service:8004")

for d in ["audio", "data", "segments", "output", "preview"]:
    (SHARED_DIR / d).mkdir(parents=True, exist_ok=True)


def clean_shared_dir():
    for sub in ["segments", "audio", "data"]:
        p = SHARED_DIR / sub
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    (SHARED_DIR / "output").mkdir(parents=True, exist_ok=True)


def clean_video_url(url: str) -> str:
    """Normalize and clean URL for platforms like Bilibili and YouTube."""
    url = url.strip()
    import re
    bili_m = re.search(r"bilibili\.com/(?:[a-zA-Z0-9\-_]+/)?video/(BV[a-zA-Z0-9]+)", url)
    if bili_m:
        return f"https://www.bilibili.com/video/{bili_m.group(1)}/"
    return url


def download_youtube(url: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_url = clean_video_url(url)
    template = str(out_dir / "downloaded_video.%(ext)s")
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": template,
        "merge_output_format": "mp4",
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        fn = ydl.prepare_filename(info)
        p = Path(fn)
        if not p.exists():
            mp4_p = p.with_suffix(".mp4")
            if mp4_p.exists():
                return mp4_p
            cands = list(out_dir.glob("downloaded_video.*"))
            if cands:
                return cands[0]
        return p


VOICE_KEY_MAP = {
    "Nữ - Hoài My (Truyền cảm, tiêu chuẩn)": "female_standard",
    "Nữ - Trẻ trung, tươi sáng": "female_youth",
    "Nữ - Dịu dàng, trầm tĩnh": "female_calm",
    "Nữ - Chị Google (Google TTS)": "female_google",
    "Nam - Nam Minh (Ấm áp, tiêu chuẩn)": "male_standard",
    "Nam - Trầm ấm, phóng sự / phim tài liệu": "male_deep",
    "Nam - Năng động, tin tức nhanh": "male_dynamic",
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
    res.raise_for_status()
    data = res.json()
    wav_rel = data["segments"][0]["audio"]
    src_wav = SHARED_DIR / wav_rel
    
    out_preview = SHARED_DIR / "preview" / f"sample_{voice_key}.wav"
    shutil.copy(src_wav, out_preview)
    return str(out_preview)


def run_orchestration(video_local: str, video_url: str, lang_choice: str, mode_choices: list[str], voice_choice: str, multi_speaker: bool, progress=gr.Progress()) -> str:
    clean_shared_dir()
    progress(0.05, desc="Xử lý nguồn video...")

    enable_dub = "Lồng tiếng (Thuyết minh)" in mode_choices
    enable_sub = "Gắn phụ đề (Vietsub)" in mode_choices

    if not enable_dub and not enable_sub:
        raise ValueError("Vui lòng chọn ít nhất một tùy chọn: 'Lồng tiếng (Thuyết minh)' hoặc 'Gắn phụ đề (Vietsub)'.")

    # 1. Resolve video
    if video_url and video_url.strip():
        video_path = download_youtube(video_url.strip(), SHARED_DIR / "audio")
    elif video_local:
        src = Path(video_local)
        video_path = SHARED_DIR / "audio" / f"input{src.suffix}"
        shutil.copy(src, video_path)
    else:
        raise ValueError("Vui lòng cung cấp URL YouTube/Bilibili hoặc upload video local.")

    # Convert paths to relative to shared volume
    rel_video_path = video_path.relative_to(SHARED_DIR).as_posix()

    # 2. Extract Audio via Video Service
    progress(0.15, desc="Tách audio từ video...")
    res = requests.post(f"{VIDEO_URL}/extract-audio", json={"video_path": rel_video_path})
    res.raise_for_status()
    audio_path = res.json()["audio_path"]

    # 3. STT via STT Service (with Speaker Diarization)
    progress(0.30, desc="Nhận dạng giọng nói & Phân tích hội thoại (Whisper + Diarization)...")
    lang_code = "zh" if lang_choice == "Tiếng Trung" else ("en" if lang_choice == "Tiếng Anh" else "auto")
    res = requests.post(f"{STT_URL}/transcribe", json={"audio_path": audio_path, "language": lang_code, "enable_diarization": multi_speaker})
    res.raise_for_status()
    stt_data = res.json()
    segments = stt_data["segments"]
    detected_lang = stt_data.get("detected_lang", "zh")

    if not segments:
        raise RuntimeError("Không tìm thấy đoạn hội thoại nào trong video.")

    # 4. Translation via Translation Service
    progress(0.50, desc="Dịch sang tiếng Việt (Opus-MT)...")
    source_lang = "en" if (lang_code == "en" or (lang_code == "auto" and detected_lang == "en")) else "zh"
    res = requests.post(f"{TRANS_URL}/translate", json={"segments": segments, "source_lang": source_lang})
    res.raise_for_status()
    segments = res.json()["segments"]

    # 5. TTS via TTS Service
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
        res.raise_for_status()
        segments = res.json()["segments"]
    else:
        progress(0.70, desc="Bỏ qua lồng tiếng (chỉ làm Vietsub)...")

    # 6. Muxing via Video Service
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
    res.raise_for_status()
    final_rel = res.json()["output_video"]
    final_video = SHARED_DIR / final_rel

    progress(1.0, desc="Hoàn tất!")
    return str(final_video)


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
            shutil.copy(v_p, SHARED_DIR / rel_path)

    res = requests.post(
        f"{VIDEO_URL}/trim",
        json={
            "video_path": rel_path,
            "start_time": start_t,
            "end_time": end_t,
            "output_filename": out_name
        }
    )
    res.raise_for_status()
    data = res.json()
    out_file = SHARED_DIR / data["trimmed_video"]
    msg = f"Đã cắt thành công đoạn ({data['start_sec']}s -> {data['end_sec']}s, dài {data['duration']}s)!\nFile lưu tại: {out_file}"
    return str(out_file), msg


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
                        url_box = gr.Textbox(
                            label="1. Nhập URL YouTube / Bilibili",
                            placeholder="https://www.bilibili.com/video/... hoặc https://youtube.com/..."
                        )
                        file_box = gr.Video(label="Hoặc upload file video từ máy", sources=["upload"])
                        
                        lang_choice = gr.Radio(
                            choices=["Tự động nhận diện", "Tiếng Trung", "Tiếng Anh"],
                            value="Tự động nhận diện",
                            label="2. Ngôn ngữ nguồn của video"
                        )
                        
                        mode_choices = gr.CheckboxGroup(
                            choices=["Gắn phụ đề (Vietsub)", "Lồng tiếng (Thuyết minh)"],
                            value=["Gắn phụ đề (Vietsub)", "Lồng tiếng (Thuyết minh)"],
                            label="3. Chế độ đầu ra mong muốn"
                        )

                        multi_speaker_toggle = gr.Checkbox(
                            label="👥 Bật nhận diện hội thoại Đa Nhân Vật (Tự động phát hiện Nam/Nữ để lồng giọng tương ứng)",
                            value=False
                        )
                        
                        with gr.Group():
                            voice_choice = gr.Dropdown(
                                choices=list(VOICE_KEY_MAP.keys()),
                                value="Nữ - Hoài My (Truyền cảm, tiêu chuẩn)",
                                label="4. Chọn giọng đọc mặc định (dùng khi không bật Đa nhân vật)",
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

                def handle_pipeline(f, u, l, m, v, multi_spk):
                    try:
                        out = run_orchestration(f, u, l, m, v, multi_spk)
                        return out, f"Thành công! Video đã lưu tại:\n{out}"
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        return None, f"Lỗi: {e}"

                btn.click(
                    fn=handle_pipeline,
                    inputs=[file_box, url_box, lang_choice, mode_choices, voice_choice, multi_speaker_toggle],
                    outputs=[out_video, status]
                )

            # TAB 2: TRIM & CUT VIDEO STUDIO
            with gr.TabItem("✂️ 2. Chỉnh Sửa & Cắt Video"):
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
    demo.launch(server_name="0.0.0.0", server_port=port, allowed_paths=["/shared_data"])


if __name__ == "__main__":
    main()
