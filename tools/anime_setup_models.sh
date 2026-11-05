#!/bin/bash
# Chuan bi thu muc model dang diffusers de ControlNetModel.from_pretrained doc
# duoc: can config.json + ten file CHINH LA diffusion_pytorch_model.safetensors
# (ten goi cua HF la mistoLine_rank256.safetensors nen khong the chi trỏ vao
# file roi ma phai dat lai ten).
set -u
M=/mnt/d/translate/models
D=$M/mistoLine
mkdir -p $D
echo "== cho tai xong =="
for i in $(seq 1 60); do
  a=$(stat -c%s $M/animagine-xl-4.0.safetensors 2>/dev/null || echo 0)
  c=$(stat -c%s $M/mistoLine_rank256.safetensors 2>/dev/null || echo 0)
  l=$(stat -c%s $M/lcm-lora-sdxl.safetensors 2>/dev/null || echo 0)
  echo "animagine $((a/1024/1024))MB / misto $((c/1024/1024))MB / lcm $((l/1024/1024))MB"
  [ $a -gt 6900000000 ] && [ $c -gt 700000000 ] && [ $l -gt 300000000 ] && break
  sleep 20
done
echo "== config.json cua MistoLine =="
curl -sL --fail -o $D/config.json \
  https://huggingface.co/TheMistoAI/MistoLine/resolve/main/config.json \
  && head -c 200 $D/config.json && echo
echo "== dat lai ten file trong =="
cp $M/mistoLine_rank256.safetensors $D/diffusion_pytorch_model.safetensors
ls -l --block-size=M $D $M | tail -12
