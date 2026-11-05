#!/bin/bash
# Xac nhan chu/logo KHONG CON tren file 2D DA HOAN CHINH (khong phai anh test
# tach rieng): lay 6 frame roi rac, ke 1 cot doc, phong to goc tren + goc duoi.
set -u
S=/mnt/d/translate/shared_data
V=translate-video-service:latest
F=${1:-output/anime_2d_khongchu.mp4}
A=$S/preview
for t in 20 150 300 480 660 840; do
  docker run --rm -v $S:/shared_data --entrypoint ffmpeg $V -y -ss $t \
    -i "/shared_data/$F" -frames:v 1 -vf "crop=iw:ih*0.28:0:0" \
    /shared_data/preview/FIN_TOP_$t.jpg -loglevel error
  docker run --rm -v $S:/shared_data --entrypoint ffmpeg $V -y -ss $t \
    -i "/shared_data/$F" -frames:v 1 -vf "crop=iw*0.30:ih*0.20:iw*0.70:ih*0.79" \
    /shared_data/preview/FIN_BR_$t.jpg -loglevel error
done
docker run --rm -v $A:/p --entrypoint ffmpeg $V -y \
  -i /p/FIN_TOP_20.jpg -i /p/FIN_TOP_150.jpg -i /p/FIN_TOP_300.jpg \
  -i /p/FIN_TOP_480.jpg -i /p/FIN_TOP_660.jpg -i /p/FIN_TOP_840.jpg \
  -filter_complex "[0][1][2][3][4][5]vstack=inputs=6" /p/FIN_TOP.jpg -loglevel error
docker run --rm -v $A:/p --entrypoint ffmpeg $V -y \
  -i /p/FIN_BR_20.jpg -i /p/FIN_BR_300.jpg -i /p/FIN_BR_660.jpg \
  -filter_complex "[0][1][2]vstack=inputs=3" /p/FIN_BR.jpg -loglevel error
ls -la $A/FIN_TOP.jpg $A/FIN_BR.jpg
