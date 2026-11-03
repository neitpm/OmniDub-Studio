"""TTS Microservice supporting multiple Vietnamese voices (Female & Male) and Multi-speaker assignment."""

import os
import re
import asyncio
import subprocess
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel
import torch
from transformers import VitsModel, AutoProcessor
import soundfile as sf
import numpy as np
import edge_tts
from gtts import gTTS

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared_data"))
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

app = FastAPI(title="TTS Service")

processor = None
vits = None

@app.on_event("startup")
def load_models():
    global processor, vits
    mname = "facebook/mms-tts-vie"
    try:
        print(f"[i] Loading fallback TTS model {mname} on {DEVICE}...")
        processor = AutoProcessor.from_pretrained(mname)
        vits = VitsModel.from_pretrained(mname).to(DEVICE)
        print(f"[OK] MMS TTS model loaded.")
    except Exception as e:
        print(f"[!] Warning MMS model load failed: {e}")


class SegmentItem(BaseModel):
    start: float
    end: float
    text: str
    translation: str
    words: list = []
    speaker: str = "SPEAKER_00"
    gender: str = "male"
    pitch_hz: float = 140.0


class TTSRequest(BaseModel):
    segments: list[SegmentItem]
    voice: str = "female_standard"
    multi_speaker: bool = False


VOICE_CONFIGS = {
    # ================= GIỌNG NỮ (FEMALE VOICES) =================
    "female_standard": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 1.0, "tempo": 1.0, "gender": "female"
    },
    "female_sweet": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 1.15, "tempo": 1.02, "gender": "female"
    },
    "female_review": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 1.10, "tempo": 1.15, "gender": "female"
    },
    "female_gentle": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 1.04, "tempo": 0.94, "gender": "female"
    },
    "female_story": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 0.92, "tempo": 0.90, "gender": "female"
    },
    "female_deep": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 0.86, "tempo": 0.88, "gender": "female"
    },
    "female_news": {
        "engine": "edge", "voice": "vi-VN-HoaiMyNeural", "rate": "+0%",
        "pitch_shift": 1.00, "tempo": 1.12, "gender": "female"
    },
    "female_google": {
        "engine": "gtts", "lang": "vi", "gender": "female"
    },

    # ================= GIỌNG NAM (MALE VOICES) =================
    "male_standard": {
        "engine": "edge", "voice": "vi-VN-NamMinhNeural", "rate": "+0%",
        "pitch_shift": 1.0, "tempo": 1.0, "gender": "male"
    },
    "male_deep": {
        "engine": "edge", "voice": "vi-VN-NamMinhNeural", "rate": "+0%",
        "pitch_shift": 0.88, "tempo": 0.94, "gender": "male"
    },
    "male_dynamic": {
        "engine": "edge", "voice": "vi-VN-NamMinhNeural", "rate": "+0%",
        "pitch_shift": 1.05, "tempo": 1.12, "gender": "male"
    },

    # ================= GIỌNG AI OFFLINE =================
    "mms": {
        "engine": "mms", "gender": "neutral"
    }
}


def contains_speakable_text(text: str) -> bool:
    """Check if text contains letters or numbers."""
    return bool(re.search(r"[a-zA-Z0-9\u00C0-\u1EF9]", text))


async def generate_edge_tts(text: str, voice_name: str, rate: str, tmp_mp3: Path):
    """Generate TTS via Edge with aggressive retries."""
    last_err = None
    for attempt in range(4):
        try:
            comm = edge_tts.Communicate(text, voice_name, rate=rate)
            await comm.save(str(tmp_mp3))
            if tmp_mp3.exists() and tmp_mp3.stat().st_size > 0:
                return
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"Edge TTS failed after 4 retries: {last_err}")


async def generate_audio_file(text: str, voice_cfg: dict, out_wav: Path):
    clean_text = text.strip()
    if not clean_text or not contains_speakable_text(clean_text):
        sf.write(str(out_wav), np.zeros(1600, dtype=np.float32), samplerate=16000)
        return

    tmp_mp3 = out_wav.with_suffix(".mp3")
    engine = voice_cfg.get("engine", "edge")

    if engine == "edge":
        voice_name = voice_cfg.get("voice", "vi-VN-HoaiMyNeural")
        rate = voice_cfg.get("rate", "+0%")
        
        try:
            await generate_edge_tts(clean_text, voice_name, rate, tmp_mp3)
        except Exception as edge_err:
            print(f"[!] Edge error: {edge_err}. Fallback to Google TTS with matching gender...")
            tts = gTTS(clean_text, lang="vi")
            tts.save(str(tmp_mp3))

        pitch_shift = voice_cfg.get("pitch_shift", 1.0)
        tempo = voice_cfg.get("tempo", 1.0)

        # Build FFmpeg audio filter for rich voice coloration
        filters = []
        if abs(pitch_shift - 1.0) > 0.01:
            sr_shift = int(16000 * pitch_shift)
            atempo_val = round((1.0 / pitch_shift) * tempo, 4)
            filters.append(f"asetrate={sr_shift}")
            # atempo supports values between 0.5 and 2.0
            if abs(atempo_val - 1.0) > 0.01:
                filters.append(f"atempo={atempo_val}")
        elif abs(tempo - 1.0) > 0.01:
            filters.append(f"atempo={tempo}")

        cmd = ["ffmpeg", "-y", "-i", str(tmp_mp3)]
        if filters:
            cmd.extend(["-af", ",".join(filters)])
        cmd.extend(["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)])

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        tmp_mp3.unlink(missing_ok=True)

    elif engine == "gtts":
        tts = gTTS(clean_text, lang="vi")
        tts.save(str(tmp_mp3))
        subprocess.run([
            "ffmpeg", "-y", "-i", str(tmp_mp3),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(out_wav)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        tmp_mp3.unlink(missing_ok=True)

    else:
        # Local MMS TTS
        inputs = processor(text=clean_text, return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            audio = vits(**inputs).waveform.squeeze().cpu().numpy()
        sf.write(str(out_wav), audio, samplerate=16000)


@app.post("/synthesize")
async def synthesize(req: TTSRequest):
    seg_dir = SHARED_DIR / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    default_voice_key = req.voice.lower()
    default_cfg = VOICE_CONFIGS.get(default_voice_key, VOICE_CONFIGS["female_standard"])

    out_segments = []
    success_count = 0

    for idx, seg in enumerate(req.segments, start=1):
        wav_path = seg_dir / f"{idx:04d}.wav"
        text = seg.translation.strip()

        # Decide voice: if multi_speaker is on, pick voice based on detected gender
        if req.multi_speaker:
            if seg.gender == "female":
                seg_voice_cfg = VOICE_CONFIGS["female_standard"]
            else:
                seg_voice_cfg = VOICE_CONFIGS["male_standard"]
        else:
            seg_voice_cfg = default_cfg

        try:
            await generate_audio_file(text, seg_voice_cfg, wav_path)
            if seg_voice_cfg.get("engine") in ["edge", "gtts"]:
                await asyncio.sleep(0.04)
            success_count += 1
        except Exception as e:
            print(f"[!] Error on segment {idx} ('{text}'): {e}")
            sf.write(str(wav_path), np.zeros(1600, dtype=np.float32), samplerate=16000)

        d = seg.dict()
        d["audio"] = wav_path.relative_to(SHARED_DIR).as_posix()
        out_segments.append(d)

    print(f"[OK] Successfully synthesized {success_count}/{len(req.segments)} segments (voice={default_voice_key}, multi_spk={req.multi_speaker}).")
    return {"segments": out_segments}


@app.get("/health")
def health():
    return {"status": "ok", "service": "tts"}
