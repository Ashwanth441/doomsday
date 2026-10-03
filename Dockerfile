FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    COOKIE_SECURE=true \
    DOOMSDAY_DB_PATH=/var/lib/doomsday/doomsday.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py README.md ./
COPY static ./static

RUN useradd --system --uid 10001 --create-home doomsday \
    && mkdir -p /var/lib/doomsday \
    && chown -R doomsday:doomsday /app /var/lib/doomsday

USER doomsday
EXPOSE 8787
CMD ["python", "server.py"]
