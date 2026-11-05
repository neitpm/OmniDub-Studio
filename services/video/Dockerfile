FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-freefont-ttf \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu128

COPY . .

EXPOSE 8004

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004"]
