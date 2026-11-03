import os, sys, json, re, subprocess
sys.path.insert(0, r'D:\translate')

for pkg in ['cublas', 'cuda_nvrtc', 'cuda_runtime']:
    p = fr'D:\translate\.venv\Lib\site-packages\nvidia\{pkg}\bin'
    if os.path.isdir(p):
        os.environ['PATH'] = p + ';' + os.environ['PATH']

from pathlib import Path
from main import build_dubbed_audio, get_video_duration, run_ffmpeg
import numpy as np
import soundfile as sf

with open(r'D:\translate\data\translation.json', encoding='utf-8') as f:
    segs = json.load(f)['segments']

video_path = Path(r'D:\translate\temp_video1.mp4')
out_wav    = Path(r'D:\translate\output\dubbed_audio.wav')
out_mp4    = Path(r'D:\translate\output\dubbed_video.mp4')
srt_path   = Path(r'D:\translate\data\subtitle_vi.srt')
sample_rate = 16000

# 1. Build timestamp-aligned audio
video_dur = get_video_duration(video_path)
print(f'[i] Video duration: {video_dur:.2f}s')
build_dubbed_audio(segs, video_dur, out_wav, sample_rate)

# 2. Verify content
data, sr = sf.read(str(out_wav), dtype='float32')
nonzero = int(np.count_nonzero(data))
peak    = float(np.max(np.abs(data)))
print(f'[i] dubbed_audio.wav: {len(data)/sr:.2f}s  peak={peak:.4f}  non-zero={nonzero}/{len(data)}')

# 3. Build subtitle filter (Windows path escaping)
srt_abs = str(srt_path.resolve()).replace('\\', '/')
srt_esc = re.sub(r'^([A-Za-z]):', r'\1\\:', srt_abs)
vf = (
    "subtitles='" + srt_esc + "':"
    "force_style='FontName=Arial,FontSize=22,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "Outline=2,Shadow=1,Alignment=2'"
)

# 4. Final mux: video + dubbed audio 44100Hz stereo + burned subtitles + boosted volume
run_ffmpeg([
    '-y',
    '-i', str(video_path),
    '-i', str(out_wav),
    '-map', '0:v:0',
    '-map', '1:a:0',
    '-vf', vf,
    '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
    '-af', 'volume=2.2,aformat=channel_layouts=stereo:sample_rates=44100',
    '-c:a', 'aac', '-b:a', '192k',
    '-t', str(video_dur),
    str(out_mp4)
])
print('[OK] Done! Output:', out_mp4)
