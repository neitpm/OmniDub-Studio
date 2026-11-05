#!/bin/bash
# Do lai vi tri chu burn-in tren CHINH TAP NAY (tap-106), khong phai tap da do
# hom truoc. In 6 frame nguon + 6 frame 2D ra preview de doc bang mat.
set -u
S=/mnt/d/translate/shared_data
P=$S/preview
V=translate-video-service:latest
mkdir -p $P
for t in 20 120 300 480 660 840; do
  docker run --rm -v $S:/shared_data --entrypoint ffmpeg $V -y -ss $t \
    -i /shared_data/audio/downloaded_video_cut.mp4 -frames:v 1 \
    -vf scale=896:512 /shared_data/preview/SRC_$t.jpg -loglevel error
  docker run --rm -v $S:/shared_data --entrypoint ffmpeg $V -y -ss $t \
    -i /shared_data/output/anime_2d_1793849890.mp4 -frames:v 1 \
    /shared_data/preview/D2_$t.jpg -loglevel error
done
# Sheet 2 cot: nguon | 2D, de so sanh cung thoi diem
docker run --rm -v $P:/p --entrypoint ffmpeg $V -y \
  -i /p/SRC_20.jpg -i /p/D2_20.jpg -i /p/SRC_300.jpg -i /p/D2_300.jpg \
  -i /p/SRC_660.jpg -i /p/D2_660.jpg \
  -filter_complex "[0]scale=896:512[a];[1]scale=896:512[b];[2]scale=896:512[c];[3]scale=896:512[d];[4]scale=896:512[e];[5]scale=896:512[f];[a][b][c][d][e][f]hstack=inputs=6" /p/CHK_TEXT.jpg -loglevel error
docker run --rm -v $P:/p --entrypoint ffprobe $V -v error -select_streams v -show_entries stream=width,height -of csv=p=0 /p/D2_20.jpg
ls -la $P/CHK_TEXT.jpg $P/D2_20.jpg
