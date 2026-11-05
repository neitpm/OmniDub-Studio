#!/bin/bash
# Ghep audio GOC (thuyet minh + tieng nhan vat) vao video 2D DA RENDER xong.
# -c:v copy = KHONG ve lai frame nao, chi 15-20s thay vi 33 phut render lai.
# Audio phai kech 1.03x vi _finalize_mux da chay setpts=PTS/1.03 len hinh
# (anti_copyright), neu khong thi tieng lech mot luc ~45 giay cuoi tap.
set -u
S=/mnt/d/translate/shared_data
V=translate-video-service:latest
D2D=${1:-anime_2d_1793849890.mp4}
SRC=${2:-audio/downloaded_video_cut.mp4}
OUT="${D2D%.mp4}_co_tieng.mp4"

dur() {
  docker run --rm -v $S:/shared_data --entrypoint ffprobe $V -v error \
    -show_entries format=duration -of default=nw=1:nk=1 "/shared_data/$1"
}
echo "video 2D : $(dur output/$D2D) s"
echo "nguon    : $(dur $SRC) s"

docker run --rm -v $S:/shared_data --entrypoint ffmpeg $V -y \
  -i "/shared_data/output/$D2D" -i "/shared_data/$SRC" \
  -filter:a "atempo=1.03" \
  -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 160k -shortest \
  "/shared_data/output/$OUT" 2>&1 | tail -4

echo "=> $OUT : $(dur output/$OUT) s"
ls -la $S/output/$OUT
