"""Translation Microservice using Helsinki-NLP models."""

from fastapi import FastAPI
from pydantic import BaseModel
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

app = FastAPI(title="Translation Service")

models = {}
tokenizers = {}

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

@app.on_event("startup")
def preload():
    for lang, mname in [("zh", "Helsinki-NLP/opus-mt-zh-vi"), ("en", "Helsinki-NLP/opus-mt-en-vi")]:
        try:
            print(f"[i] Loading {mname} on {DEVICE}...")
            tokenizers[lang] = AutoTokenizer.from_pretrained(mname)
            models[lang] = AutoModelForSeq2SeqLM.from_pretrained(mname).to(DEVICE)
            print(f"[OK] Loaded {mname}")
        except Exception as e:
            print(f"[!] Failed to load {mname}: {e}")


class SegmentItem(BaseModel):
    start: float
    end: float
    text: str
    words: list = []


class TranslateRequest(BaseModel):
    segments: list[SegmentItem]
    source_lang: str = "zh"  # zh or en


@app.post("/translate")
def translate(req: TranslateRequest):
    lang = req.source_lang if req.source_lang in ["zh", "en"] else "zh"
    tok = tokenizers.get(lang)
    mod = models.get(lang)

    if not tok or not mod:
        tok = AutoTokenizer.from_pretrained(f"Helsinki-NLP/opus-mt-{lang}-vi")
        mod = AutoModelForSeq2SeqLM.from_pretrained(f"Helsinki-NLP/opus-mt-{lang}-vi").to(DEVICE)
        tokenizers[lang] = tok
        models[lang] = mod

    texts = [s.text for s in req.segments]
    translations = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        inputs = tok(batch, return_tensors="pt", padding=True, truncation=True, max_length=512).to(DEVICE)
        with torch.no_grad():
            outputs = mod.generate(**inputs, max_new_tokens=512)
        translations.extend(tok.batch_decode(outputs, skip_special_tokens=True))

    out_segments = []
    for s, trans in zip(req.segments, translations):
        d = s.dict()
        d["translation"] = trans.strip()
        out_segments.append(d)

    return {"segments": out_segments}


@app.get("/health")
def health():
    return {"status": "ok", "service": "translation"}
