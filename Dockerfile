FROM python:3.12-alpine

WORKDIR /app
COPY server.py /app/server.py
COPY ainews.py /app/ainews.py
COPY webui.py /app/webui.py
COPY tasks.py /app/tasks.py
COPY uploads.py /app/uploads.py
COPY bilicookies.py /app/bilicookies.py
COPY sync_bilibili_favorites.py /app/sync_bilibili_favorites.py
COPY miku.conf /app/miku.conf
COPY music.json /app/music.json
COPY media /app/media

EXPOSE 8080
USER nobody
CMD ["python", "/app/server.py", "--config", "/app/miku.conf"]
