#!/bin/bash
# Ep nhanh 2x video 2D DA RENDER xong: setpts=PTS/2 (hinh) + atempo=2 (tieng).
# Tai sao khong can render lai: toan bo buoc ve/xoa chu o 896x512 da nam trong
# file mp4 roi; doi toc do chi la thay doi PTS + encode lai, ~2 phut.
# atempo=2.0 la gioi tren cua mot lan atempo (thang 0.5-2.0), dung mot phat.
set -u
S=/mnt/d/translate/shared_data
V=translate-video-service:latest
IN=${1:-anime_2d_khongchu.mp4}
OUT=${2:-${IN%.mp4}_x2.mp4}

dur() {
  docker run --rm -v $S:/shared_data --entrypoint ffprobe $V -v error \
    -show_entries format=duration -of default=nw=1:nk=1 "/shared_data/output/$1"
}
echo "vao : $IN  $(dur $IN) s"
T0=$(date +%s)
docker run --rm -v $S:/shared_data --entrypoint ffmpeg $V -y \
  -i "/shared_data/output/$IN" \
  -filter:v "setpts=PTS/2" -filter:a "atempo=2.0" \
  -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -movflags +faststart \
  "/shared_data/output/$OUT" 2>&1 | tail -3
echo "ra  : $OUT  $(dur $OUT) s   (lam trong $(( $(date +%s) - T0 ))s)"
ls -la $S/output/$OUT
