FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask 'yt-dlp[default,curl-cffi]==2026.10.21'
WORKDIR /app
COPY server.py /app/server.py
EXPOSE 8080
CMD ["python", "server.py"]
