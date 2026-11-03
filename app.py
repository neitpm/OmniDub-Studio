import re
from pathlib import Path

import gradio as gr
import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer


MODEL_ID = "Helsinki-NLP/opus-mt-zh-vi"
MAX_CHARS = 700


def load_model():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_ID)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    return tokenizer, model, device


tokenizer, model, device = load_model()


def split_text(text: str, max_chars: int = MAX_CHARS):
    """Split by paragraphs/sentences while keeping novel formatting readable."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > max_chars:
            cut = max(
                paragraph.rfind("。", 0, max_chars),
                paragraph.rfind("！", 0, max_chars),
                paragraph.rfind("？", 0, max_chars),
                paragraph.rfind("，", 0, max_chars),
            )
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            chunks.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            chunks.append(paragraph)
    return chunks


@torch.inference_mode()
def translate_text(text: str, progress=gr.Progress()):
    if not text or not text.strip():
        return "", "Chưa có nội dung để dịch."
    chunks = split_text(text)
    results = []
    for i, chunk in enumerate(chunks):
        inputs = tokenizer(chunk, return_tensors="pt", truncation=True, max_length=512)
        inputs = {key: value.to(device) for key, value in inputs.items()}
        output = model.generate(**inputs, num_beams=4, max_new_tokens=512)
        results.append(tokenizer.decode(output[0], skip_special_tokens=True))
        progress((i + 1) / len(chunks), desc=f"Đang dịch {i + 1}/{len(chunks)} đoạn")
    return "\n\n".join(results), f"Đã dịch {len(chunks)} đoạn bằng {MODEL_ID} ({device})."


def read_file(file):
    if file is None:
        return ""
    return Path(file).read_text(encoding="utf-8-sig")


with gr.Blocks(title="Dịch truyện Trung - Việt") as demo:
    gr.Markdown("# Dịch truyện Trung → Việt\nNhập văn bản hoặc tải file `.txt`. Nội dung sẽ được chia đoạn tự động.")
    with gr.Row():
        with gr.Column():
            upload = gr.File(label="Tải truyện tiếng Trung (.txt)", file_types=[".txt"], type="filepath")
            source = gr.Textbox(label="Văn bản tiếng Trung", lines=18, placeholder="粘贴中文小说内容...")
            translate = gr.Button("Dịch sang tiếng Việt", variant="primary")
        with gr.Column():
            target = gr.Textbox(label="Bản dịch tiếng Việt", lines=18)
            status = gr.Markdown()
            download = gr.DownloadButton("Tải bản dịch", visible=False)

    upload.change(read_file, upload, source)
    translate.click(translate_text, source, [target, status])


if __name__ == "__main__":
    demo.launch()
