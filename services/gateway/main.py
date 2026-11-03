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
        # Tự động tiếp tục và thử lại khi mạng đứt quãng
        "retries": 15,
        "fragment_retries": 15,
        "continuedl": True,
        "file_access_retries": 10,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,  # 10MB chunk để tránh timeout socket
        "nocheckcertificate": True,
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

                    progress(0.1, desc="Khởi tạo phân cảnh 3D...")
                    scenes_dir = SHARED_DIR / "scenes"
                    scenes_dir.mkdir(parents=True, exist_ok=True)

                    import re, subprocess
                    import matplotlib.pyplot as plt
                    import numpy as np

                    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
                    full_text = " ".join(lines)
                    sentences = [s.strip() for s in re.split(r'[.!?。！？\n]', full_text) if len(s.strip()) > 5]
                    if not sentences:
                        sentences = [full_text]

                    actual_scenes = min(int(num_scenes), len(sentences))
                    step = max(1, len(sentences) // actual_scenes)

                    image_paths = []
                    clip_videos = []

                    cmaps = {
                        "Hoạt hình 3D Tiên Hiệp / Huyền Huyễn (Phong cách Đấu Phá Khung Thương)": "magma",
                        "Hoạt hình 3D Pixar / Disney (Hiện đại, dễ thương, cinematic)": "Spectral",
                        "Anime 3D Cyberpunk / Viễn Tưởng Hiện Đại": "cool",
                        "3D Cổ Trang Thủy Mặc Điện Ảnh (Cinematic Unreal Engine 5)": "bone"
                    }
                    chosen_cmap = cmaps.get(style, "plasma")

                    for i in range(actual_scenes):
                        progress(0.2 + (0.5 * (i / max(1, actual_scenes))), desc=f"Đang render Khung hình 3D Cảnh {i+1}...")
                        start_idx = i * step
                        scene_content = " ".join(sentences[start_idx : start_idx + step])
                        if not scene_content:
                            scene_content = sentences[0]

                        # Generate 3D procedural cinematic scene art
                        fig, ax = plt.subplots(figsize=(12.8, 7.2), dpi=100)
                        y, x = np.mgrid[0:720, 0:1280]
                        phase = i * 0.8
                        bg = np.sin((x + phase*100) / 280) * 0.25 + np.cos(y / 180) * 0.25 + (1 - y / 720) * 0.5
                        ax.imshow(bg, cmap=chosen_cmap, aspect='auto', extent=[0, 1280, 0, 720])
                        
                        # 3D Celestial sphere
                        orb_color = 'gold' if i % 2 == 0 else 'cyan'
                        ax.add_patch(plt.Circle((640, 430), 150, color=orb_color, alpha=0.35))
                        ax.add_patch(plt.Circle((640, 430), 90, color='white', alpha=0.7))

                        # Mountains / architecture silhouette
                        lx = np.linspace(0, 1280, 200)
                        ly = 130 + 40 * np.sin((lx + i*50) / 80) + 30 * np.cos(lx / 30)
                        ax.fill_between(lx, 0, ly, color='#0a0e1a', alpha=0.95)

                        # Subtitles & Scene Header on Frame
                        ax.text(640, 640, f"PHÂN CẢNH {i+1}: {style.split('(')[0].strip()}", color='#ffffff', fontsize=22, fontweight='bold', ha='center',
                                bbox=dict(boxstyle='round,pad=0.5', facecolor='#000000', alpha=0.75, edgecolor='cyan'))
                        
                        wrapped_text = "\n".join([scene_content[j:j+55] for j in range(0, min(165, len(scene_content)), 55)])
                        ax.text(640, 70, wrapped_text, color='#fff59d', fontsize=16, ha='center',
                                bbox=dict(boxstyle='round,pad=0.5', facecolor='#0d1117', alpha=0.88, edgecolor='gold'))

                        ax.set_xlim(0, 1280)
                        ax.set_ylim(0, 720)
                        ax.axis('off')
                        plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
                        
                        img_path = scenes_dir / f"frame_scene_{i+1}.png"
                        plt.savefig(str(img_path))
                        plt.close()
                        image_paths.append(str(img_path))

                        # Build 3D camera pan animation clip for this scene
                        clip_path = scenes_dir / f"clip_scene_{i+1}.mp4"
                        cmd = [
                            "ffmpeg", "-y", "-loop", "1", "-i", str(img_path),
                            "-c:v", "libx264", "-t", "4", "-pix_fmt", "yuv420p",
                            "-vf", "scale=1280:720", str(clip_path)
                        ]
                        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if clip_path.exists():
                            clip_videos.append(clip_path)

                    # Concatenate clips into final 3D animation movie
                    progress(0.8, desc="Ghép nối các cảnh thành phim 3D hoàn chỉnh...")
                    concat_list_file = scenes_dir / "concat_list.txt"
                    with open(concat_list_file, "w", encoding="utf-8") as f:
                        for cp in clip_videos:
                            f.write(f"file '{cp.name}'\n")

                    final_movie_path = SHARED_DIR / "output" / "3d_novel_movie.mp4"
                    cmd_concat = [
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(concat_list_file),
                        "-c", "copy", str(final_movie_path)
                    ]
                    subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    progress(1.0, desc="Đã hoàn thành dựng phim 3D!")
                    msg = f"Đã render thành công {len(image_paths)} khung hình 3D và xuất phim tại:\n{final_movie_path}"
                    return msg, image_paths, str(final_movie_path)

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

            # TAB 3: TRIM & CUT VIDEO STUDIO
            with gr.TabItem("✂️ 3. Chỉnh Sửa & Cắt Video"):
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
