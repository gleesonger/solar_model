FROM python:3.12.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY *.py ./
COPY config.yaml sigen_register_map.json /config/

# config.yaml and solar.db are resolved relative to the working directory.
WORKDIR /config
VOLUME ["/config"]

CMD ["python", "/app/main.py"]
