FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-jpn tesseract-ocr-chi-tra \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8787
EXPOSE 8787
# Per image ~600s (identify + jacket lock + related). The flag is fixed at
# process start, so 2400 = 4 × 600. A live job also calls Worker.notify().
CMD sh -c 'gunicorn -b 0.0.0.0:${PORT:-8080} -w 2 --timeout 2400 server:app'
