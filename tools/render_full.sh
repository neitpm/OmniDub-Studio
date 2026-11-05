#!/bin/bash
# Render CA TAP (15 phut) bang code da sua: xoa logo theo thoi gian + tieng goc.
# Chi build/recreate video-service; 4 service con lai khong cham.
set -u
cd /mnt/d/translate || exit 1

echo "=== 1. TRUOC ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep translate | sort

echo "=== 2. Syntax check + build ==="
V=translate-video-service:latest
docker run --rm -v /mnt/d/translate/services/video/main.py:/app/main.py:ro \
  --entrypoint python $V -c "compile(open('/app/main.py').read(),'main','exec'); print('python OK')" || exit 1
T0=$(date +%s)
docker compose build video-service 2>&1 | tail -2
docker compose up -d video-service 2>&1 | tail -1
sleep 15
echo "build+recreate: $(( $(date +%s) - T0 ))s"

echo "=== 3. Render (dong boi, ~35 phut) ==="
T0=$(date +%s)
curl -s --max-time 5400 -X POST http://localhost:8004/restyle-2d \
  -H 'Content-Type: application/json' -d '{
  "video_path":"audio/downloaded_video_cut.mp4",
  "style":"Anime 2D Cel-Shading (Net ve tay Manga, mau phang tuoi sang)",
  "output_filename":"anime_2d_khongchu.mp4",
  "remove_watermark":true,"anti_copyright":true,"flip_horizontal":false,
  "keep_original_audio":true,"dub_audio_path":"","fps_div":2,"palette_k":20}'
echo
echo "render: $(( $(date +%s) - T0 ))s"

echo "=== 4. Log ==="
docker logs --tail 400 translate-video 2>&1 | grep -E 'neural|overlay|mux|Error' | tail -12
ls -la /mnt/d/translate/shared_data/output/anime_2d_khongchu.mp4
echo "=== 5. SAU (4 service kia khong duoc doi) ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep translate | sort
