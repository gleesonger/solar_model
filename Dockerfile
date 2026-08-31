FROM python:3.12.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

WORKDIR /config

VOLUME ["/config"]

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["python", "/app/docker_start.py"]