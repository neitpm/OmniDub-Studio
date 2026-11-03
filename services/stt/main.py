"""STT Microservice using faster-whisper with Speaker Diarization & Gender Detection."""

import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from faster_whisper import WhisperModel
import torch
import numpy as np
import librosa
import soundfile as sf
from sklearn.cluster import AgglomerativeClustering

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared_data"))

app = FastAPI(title="STT Service with Diarization")

model = None


def get_whisper_model():
    candidates = [
        ("cuda", "float16"),
        ("cuda", "int8"),
        ("cuda", "default"),
        ("cpu", "int8")
    ]
    for dev, comp in candidates:
        try:
            print(f"[i] Trying Whisper on {dev} with {comp}...")
            m = WhisperModel("medium", device=dev, compute_type=comp)
            print(f"[OK] Whisper successfully loaded on {dev} ({comp})")
            return m
        except Exception as e:
            print(f"[!] {dev}/{comp} failed: {e}")
    raise RuntimeError("Could not load Whisper model on any configuration.")


@app.on_event("startup")
def load_model():
    global model
    model = get_whisper_model()


def detect_segment_pitch_gender(y_seg, sr=16000) -> tuple[float, str]:
    """Estimate fundamental pitch (F0) and infer male/female gender.
    
    Male F0 is typically ~85Hz - 165Hz.
    Female F0 is typically ~165Hz - 260Hz+.
    """
    if len(y_seg) < sr * 0.3:  # Too short to estimate accurately
        return 150.0, "male"
    try:
        f0, voiced_flag, voiced_probs = librosa.pyin(
            y_seg,
            fmin=librosa.note_to_hz('C2'),  # ~65 Hz
            fmax=librosa.note_to_hz('C6'),  # ~1046 Hz
            sr=sr
        )
        valid_f0 = f0[voiced_flag & ~np.isnan(f0)]
        if len(valid_f0) > 0:
            median_f0 = float(np.median(valid_f0))
            gender = "female" if median_f0 >= 165.0 else "male"
            return round(median_f0, 1), gender
    except Exception as e:
        print(f"[!] Pitch estimation error: {e}")
    return 150.0, "male"


def perform_diarization_and_gender(audio_path: Path, segments: list[dict]) -> list[dict]:
    """Cluster segments by speaker and detect gender for each segment."""
    if not segments:
        return segments
    
    try:
        # Load audio at 16kHz mono
        y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
        total_len = len(y)

        features = []
        pitches = []
        genders = []

        for seg in segments:
            start_samp = int(max(0, seg["start"]) * sr)
            end_samp = int(min(total_len / sr, seg["end"]) * sr)
            if end_samp <= start_samp:
                end_samp = min(total_len, start_samp + sr)
            
            y_clip = y[start_samp:end_samp]
            
            # 1. Pitch & gender detection
            pitch_val, gender_val = detect_segment_pitch_gender(y_clip, sr=sr)
            pitches.append(pitch_val)
            genders.append(gender_val)

            # 2. Acoustic MFCC + Spectral features for voice fingerprinting
            if len(y_clip) >= 512:
                mfcc = librosa.feature.mfcc(y=y_clip, sr=sr, n_mfcc=13)
                mfcc_mean = np.mean(mfcc, axis=1)
                mfcc_std = np.std(mfcc, axis=1)
                spec_cent = np.mean(librosa.feature.spectral_centroid(y=y_clip, sr=sr))
                feat = np.hstack([mfcc_mean, mfcc_std, [spec_cent / 1000.0], [pitch_val / 200.0]])
            else:
                feat = np.zeros(28)
            features.append(feat)

        features = np.array(features)

        # Cluster into speakers (2 speakers max if short/dialogue, or distance threshold)
        if len(segments) >= 2:
            n_clusters = min(2, len(segments))
            clustering = AgglomerativeClustering(
                n_clusters=n_clusters,
                metric='cosine',
                linkage='average'
            )
            labels = clustering.fit_predict(features)
        else:
            labels = [0]

        for i, seg in enumerate(segments):
            speaker_id = f"SPEAKER_{labels[i]:02d}"
            seg["speaker"] = speaker_id
            seg["pitch_hz"] = pitches[i]
            seg["gender"] = genders[i]

    except Exception as e:
        print(f"[!] Diarization failed: {e}. Falling back to default speaker.")
        for seg in segments:
            seg["speaker"] = "SPEAKER_00"
            seg["gender"] = "male"
            seg["pitch_hz"] = 140.0

    return segments


class TranscribeRequest(BaseModel):
    audio_path: str
    language: str = "auto"
    enable_diarization: bool = True


@app.post("/transcribe")
def transcribe_audio(req: TranscribeRequest):
    global model
    audio_abs = SHARED_DIR / req.audio_path
    if not audio_abs.exists():
        raise HTTPException(status_code=404, detail=f"Audio not found: {req.audio_path}")

    lang = None if req.language == "auto" else req.language
    
    try:
        segments_gen, info = model.transcribe(
            str(audio_abs),
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            language=lang
        )
        results = []
        for seg in segments_gen:
            results.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text.strip(),
                "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (seg.words or [])]
            })

    except Exception as e:
        print(f"[!] STT transcribe error on current device: {e}")
        print("[↺] Fallback Whisper model to CPU int8...")
        model = WhisperModel("medium", device="cpu", compute_type="int8")
        segments_gen, info = model.transcribe(
            str(audio_abs),
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            language=lang
        )
        results = []
        for seg in segments_gen:
            results.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text.strip(),
                "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (seg.words or [])]
            })

    # Run Speaker Diarization & Gender Detection
    if req.enable_diarization:
        print("[i] Running Speaker Diarization & Gender Detection...")
        results = perform_diarization_and_gender(audio_abs, results)

    return {
        "detected_lang": info.language,
        "prob": round(info.language_probability, 2),
        "segments": results
    }
