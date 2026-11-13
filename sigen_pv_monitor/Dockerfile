# Dodatek Home Assistant: aplikacja Sigen PV Monitor z wydania (tagu) o numerze wersji dodatku.
ARG BUILD_FROM=python:3.12-slim-bookworm
FROM ${BUILD_FROM}
ARG BUILD_VERSION
ENV PYTHONUNBUFFERED=1 SIGEN_PV_DATA=/data SIGEN_HA_ADDON=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/mgrys/Sigen/archive/refs/tags/v${BUILD_VERSION}.tar.gz" \
       | tar xz --strip-components=1 -C /app \
    && pip install --no-cache-dir -r requirements.txt websockets
COPY run.py /run.py
CMD ["python3", "/run.py"]
