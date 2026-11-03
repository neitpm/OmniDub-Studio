# HƯỚNG DẪN KHỞI CHẠY HỆ THỐNG MICROSERVICES AI DUBBING BẰNG DOCKER

Hệ thống được chia nhỏ thành 5 microservices độc lập:

1. **`gateway` (Port 7860, 8000)**: Điều phối toàn bộ quy trình, Web UI Gradio, tích hợp tải YouTube qua yt-dlp.
2. **`stt-service` (Port 8001)**: Nhận dạng giọng nói tiếng Trung/Anh có timestamp (faster-whisper GPU).
3. **`translation-service` (Port 8002)**: Dịch thuật batch sang tiếng Việt (Helsinki-NLP Opus-MT GPU).
4. **`tts-service` (Port 8003)**: Tổng hợp giọng đọc tiếng Việt theo từng câu (MMS-TTS GPU).
5. **`video-service` (Port 8004)**: Tách audio, đồng bộ timeline, đốt Vietsub và mix âm thanh bằng FFmpeg.

---

## 1. Yêu cầu trước khi chạy
- Đã cài đặt **Docker Desktop** trên Windows và đảm bảo Docker Desktop đang bật (Running).
- Đã cài đặt **NVIDIA Container Toolkit** (nếu muốn tận dụng GPU cho Docker containers).

---

## 2. Các lệnh khởi chạy

### Khởi động toàn bộ hệ thống Microservices:
```powershell
cd D:\translate
docker compose up --build -d
```

### Xem log thời gian thực của toàn bộ hệ thống:
```powershell
docker compose logs -f
```

### Xem log của từng service riêng biệt:
```powershell
# Xem log gateway (Orchestrator & UI)
docker compose logs -f gateway

# Xem log STT
docker compose logs -f stt-service

# Xem log TTS
docker compose logs -f tts-service
```

### Dừng hệ thống:
```powershell
docker compose down
```

---

## 3. Truy cập sử dụng
- Mở trình duyệt web tại: `http://localhost:7860`
- Video kết quả sau khi xử lý sẽ tự động xuất hiện tại thư mục: `D:\translate\shared_data\output\dubbed_video.mp4`
