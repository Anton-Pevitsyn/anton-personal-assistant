FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir cryptography==50.0.0 python-docx==1.2.0 pypdf==6.21.4
WORKDIR /app
COPY bot.py calendar_bridge.py document_work.py ./
CMD ["python", "-u", "bot.py"]
