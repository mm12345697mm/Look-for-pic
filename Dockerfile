FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-jpn tesseract-ocr-chi-tra \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8787
EXPOSE 8787
CMD gunicorn -b 0.0.0.0:${PORT} -w 2 --timeout 180 server:app
