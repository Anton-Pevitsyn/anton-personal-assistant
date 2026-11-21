FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir cryptography==50.0.0
WORKDIR /app
COPY bot.py calendar_bridge.py ./
CMD ["python", "-u", "bot.py"]
