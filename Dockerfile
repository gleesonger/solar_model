FROM python:3.12.10-slim

ARG GIT_COMMIT_ID=unknown
ARG GIT_COMMIT_DATE=unknown
ARG GIT_COMMIT_MESSAGE=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN printf '%s\n%s\n%s\n' "$GIT_COMMIT_ID" "$GIT_COMMIT_DATE" "$GIT_COMMIT_MESSAGE" > /app/git-build-info

RUN chmod +x /app/docker-entrypoint.sh

WORKDIR /config

VOLUME ["/config"]

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["python", "/app/docker_start.py"]
