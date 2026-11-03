# 🎬 OmniDub Studio (AI Video Dubbing & Subtitling Microservices)

> **OmniDub Studio** là hệ sinh thái AI Microservices hoàn chỉnh dùng để tự động hóa quy trình: **Tải video (YouTube/Bilibili) ➔ Tách tiếng ➔ Nhận dạng giọng nói (STT) ➔ Dịch thuật Trung/Anh sang Việt ➔ Phân đoạn & Nhận diện giới tính Nam/Nữ (Diarization & Pitch) ➔ Lồng tiếng thuyết minh đa nhân vật (TTS) ➔ Gắn Vietsub & Chỉnh sửa / Cắt ghép video**.

---

## 🏗️ Kiến Trúc Khối 3D Hệ Thống (3D Microservices Architecture)

![OmniDub Studio 3D Isometric Architecture](docs/images/architecture_3d.jpg)

### Sơ đồ luồng dữ liệu chi tiết giữa các dịch vụ:

```
       +-------------------------------------------------------------+
       |                  🌐 USERS / CLIENT BROWSERS                 |
       |                (http://localhost:7860 - Gradio)             |
       +------------------------------+------------------------------+
                                      |
                                      v
  ============================= [ DOCKER NETWORK ] =============================
  
     +-------------------------------------------------------------------+
     |                     📦 SERVICE 1: GATEWAY & UI                    |
     |                  [translate-gateway] (Port: 7860/8088)            |
     |  - Giao diện Web Studio (Gradio Blocks 6)                         |
     |  - Tải & Chuẩn hoá URL YouTube / Bilibili (yt-dlp)                |
     |  - Nghe thử giọng đọc trước khi xử lý (Voice Preview)             |
     |  - Điều phối luồng xử lý toàn bộ Pipeline (Workflow Orchestrator) |
     +-------+--------------------+--------------------+-----------------+
             |                    |                    |                 |
             v                    v                    v                 v
     +---------------+    +---------------+    +---------------+    +---------------+
     | 📦 SERVICE 2  |    | 📦 SERVICE 3  |    | 📦 SERVICE 4  |    | 📦 SERVICE 5  |
     |  STT & VOICE  |    |  TRANSLATION  |    | MULTI-TTS AI  |    | VIDEO & MUX   |
     | [translate-   |    | [translate-   |    | [translate-   |    | [translate-   |
     |  stt]         |    |  translation] |    |  tts]         |    |  video]       |
     | (Port: 8001)  |    | (Port: 8002)  |    | (Port: 8003)  |    | (Port: 8004)  |
     |---------------|    |---------------|    |---------------|    |---------------|
     | • Whisper STT |    | • Helsinki-   |    | • 8 Giọng Nam |    | • FFmpeg Core |
     |   (float16    |    |   NLP Opus-MT |    |   & Nữ Neural |    | • Tách audio  |
     |   medium)     |    | • Dịch Trung  |    | • Auto Gender |    |   từ video    |
     | • Speaker     |    |   ➔ Việt       |    |   Voice Sync  |    | • Burn sub srt|
     |   Diarization |    | • Dịch Anh    |    | • Google TTS  |    |   vào khung   |
     | • F0 Pitch    |    |   ➔ Việt       |    |   Fallback    |    | • Mix âm lồng |
     |   Gender Clf  |    | • Giữ nguyên  |    | • Local MMS   |    | • Cắt/Trim    |
     |   (Nam / Nữ)  |    |   timestamps  |    |   VITS Model  |    |   video studio|
     +-------+-------+    +-------+-------+    +-------+-------+    +-------+-------+
             |                    |                    |                    |
             +--------------------+--------------------+--------------------+
                                      |
                                      v
     +-------------------------------------------------------------------+
     |              💾 DOCKER SHARED VOLUME (/shared_data)               |
     |  - audio/     : Video gốc & File âm thanh đã trích xuất           |
     |  - data/      : srt vietsub, json hội thoại & dịch thuật          |
     |  - segments/  : Từng câu thoại TTS đã lồng tiếng độc lập          |
     |  - preview/   : File âm thanh nghe thử giọng mẫu                  |
     |  - output/    : Video Vietsub / Thuyết minh / Cắt ghép thành phẩm |
     +-------------------------------------------------------------------+
```

---

## 🧩 Chi Tiết Chức Năng Từng Microservice

### 1. `gateway-service` (Cổng Điều Phối & Giao Diện Studio)
* **Container:** `translate-gateway`
* **Cổng mở:** `7860` (Gradio Web UI) | `8088` (API Gateway)
* **Nhiệm vụ chính:**
  - Cung cấp giao diện trực quan cho người dùng cuối.
  - Phân tích và tải video từ YouTube hoặc Bilibili (tự động làm sạch định dạng URL `/zh-Hans/` và mã `BV...`).
  - **Voice Audition (Nghe thử giọng)**: Tạo ngay câu đọc mẫu theo giọng đã chọn mà không cần dịch cả video.
  - Điều phối lần lượt dữ liệu qua 4 service còn lại thông qua mạng nội bộ Docker.

---

### 2. `stt-service` (Nhận Dạng Giọng Nói, Tách Hội Thoại & Nhận Diện Giới Tính)
* **Container:** `translate-stt`
* **Cổng mở:** `8001`
* **Nhiệm vụ chính:**
  - **Speech-to-Text**: Tự động nghe và bóc tách từng câu thoại kèm mốc thời gian chính xác (start/end) bằng mô hình `faster-whisper medium` chạy trên **NVIDIA GPU (RTX 5060 Ti / CUDA float16)**.
  - **Speaker Diarization (Phân đoạn người nói)**: Nhận biết trong video có bao nhiêu người đang tham gia hội thoại (`SPEAKER_00`, `SPEAKER_01`) nhờ thuật toán trích xuất đặc trưng âm học MFCC + Spectral Centroid + Gom cụm thứ bậc (Agglomerative Clustering).
  - **Gender Classification (Nhận diện Nam / Nữ)**: Đo dải tần số cơ bản của dây thanh quản ($F_0$ Fundamental Frequency) qua thuật toán `librosa.pyin`:
    - Tần số $F_0 \ge 165\text{ Hz}$ ➔ Nhận diện **Giọng Nữ**.
    - Tần số $F_0 < 165\text{ Hz}$ ➔ Nhận diện **Giọng Nam**.

---

### 3. `translation-service` (Dịch Thuật Chuyên Sâu)
* **Container:** `translate-translation`
* **Cổng mở:** `8002`
* **Nhiệm vụ chính:**
  - Nhận danh sách các câu hội thoại kèm timestamp từ STT.
  - Dịch tự động từ **Tiếng Trung ➔ Tiếng Việt** (`Helsinki-NLP/opus-mt-zh-vi`) hoặc **Tiếng Anh ➔ Tiếng Việt** (`Helsinki-NLP/opus-mt-en-vi`).
  - Tự động sinh file phụ đề chuẩn định dạng `.srt` (Vietsub).

---

### 4. `tts-service` (Lồng Tiếng Thuyết Minh Đa Giọng & Đa Nhân Vật)
* **Container:** `translate-tts`
* **Cổng mở:** `8003`
* **Nhiệm vụ chính:**
  - Cung cấp **8 phong cách giọng đọc tiếng Việt**:
    - *Nữ*: Hoài My (Truyền cảm), Trẻ trung tươi sáng, Dịu dàng trầm tĩnh, Chị Google.
    - *Nam*: Nam Minh (Ấm áp), Trầm ấm phóng sự/tài liệu, Năng động tin tức.
    - *MMS AI*: Mô hình offline chạy cục bộ trên GPU.
  - **Chế độ Đa Nhân Vật (Multi-Speaker Auto-Dubbing)**: Tự động đổi luân phiên giữa giọng Nam và Nữ khi nhận diện được đoạn hội thoại qua lại.
  - **Cơ chế chống đổi giọng (Anti-Voice Switching)**: Thử lại 4 lần nếu mạng chập chờn; tự động chuyển dự phòng sang Google TTS cùng giới tính (không làm biến dạng giọng đọc giữa video).

---

### 5. `video-service` (Xử Lý Đồ Họa, Gắn Phụ Đề & Cắt Ghép Video)
* **Container:** `translate-video`
* **Cổng mở:** `8004`
* **Nhiệm vụ chính:**
  - **Trích xuất âm thanh**: Tách audio sạch 16kHz mono từ video gốc.
  - **Gắn phụ đề cứng (Hardsub)**: Render Vietsub tiếng Việt trực tiếp lên khung hình video bằng bộ lọc FFmpeg `subtitles`.
  - **Mix âm thanh thông minh**: Ghép các file thoại lồng tiếng vào đúng mốc thời gian, tự động hạ âm lượng video gốc (Audio Ducking) để giọng thuyết minh rõ ràng.
  - **Studio Cắt/Xén Video (Trim/Cut)**: Cho phép cắt lấy một đoạn ngắn bất kỳ (ví dụ từ `00:00:10` đến `00:01:00`) từ video vừa tạo để lưu lại hoặc đăng Reels/TikTok/Shorts.

---

## ⚡ Hướng Dẫn Vận Hành

### Khởi động hệ thống:
* **Cách 1**: Click đúp vào file `start_services.bat`.
* **Cách 2** (Dòng lệnh):
  ```powershell
  cd D:\translate
  docker compose up -d
  ```

### Truy cập Web Studio:
Mở trình duyệt bất kỳ: **`http://localhost:7860`**

### Tắt hệ thống:
* Click đúp file `stop_services.bat` hoặc gõ `docker compose down`.
