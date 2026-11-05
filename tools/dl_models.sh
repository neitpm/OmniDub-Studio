#!/bin/bash
# Tai model anime that vao D:\translate\models (ngoai container de KHONG mat
# khi rebuild/recreate). Lenh co -C - nen chay lai duoc de tiep dien.
set -u
M=/mnt/d/translate/models
mkdir -p $M
dl() {  # dl <ten file> <url>
  local f=$1 u=$2
  if [ -s "$M/$f" ] && [ "$(stat -c%s "$M/$f")" -ge "$3" ]; then
    echo "== co san $f ($(du -h "$M/$f" | cut -f1))"; return 0
  fi
  echo "== tai $f"
  curl -L --fail --retry 4 --retry-delay 3 -C - --progress-bar -o "$M/$f" "$u" \
    && echo "   OK $f $(du -h "$M/$f" | cut -f1)" || echo "   FAIL $f"
}

# SDXL anime checkpoint (day du, khong can token)
dl animagine-xl-4.0.safetensors \
   https://huggingface.co/cagliostrolab/animagine-xl-4.0/resolve/main/animagine-xl-4.0.safetensors \
   6900000000
# ControlNet lineart SDXL (ban rank256 nhe 3.2 lan so voi ban day du)
dl mistoLine_rank256.safetensors \
   https://huggingface.co/TheMistoAI/MistoLine/resolve/main/mistoLine_rank256.safetensors \
   700000000
# LCM LoRA SDXL: 6-8 step thay vi 25
dl lcm-lora-sdxl.safetensors \
   https://huggingface.co/latent-consistency/lcm-lora-sdxl/resolve/main/pytorch_lora_weights.safetensors \
   300000000
# San pham phu de so sanh: duong SD1.5 re hon
dl control_v11p_sd15s2_lineart_anime_fp16.safetensors \
   https://huggingface.co/comfyanonymous/ControlNet-v1-1_fp16_safetensors/resolve/main/control_v11p_sd15s2_lineart_anime_fp16.safetensors \
   600000000
dl lcm-lora-sdv1-5.safetensors \
   https://huggingface.co/latent-consistency/lcm-lora-sdv1-5/resolve/main/pytorch_lora_weights.safetensors \
   100000000

echo "== KET LUOC =="
ls -l --block-size=M $M | tail -8
df -h /mnt/d | tail -1
