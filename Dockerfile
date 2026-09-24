# 0xPlant lab image: contains the simulated plant (PLCs, HMI, EWS) and the 0xPlant
# security components (console, sensor). The role is selected by the container command.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="0xPlant" \
      org.opencontainers.image.description="ICS/OT security lab: simulated water treatment plant protected by 0xPlant" \
      org.opencontainers.image.source="https://github.com/SiteQ8/0xPlant" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY modbuslite/ modbuslite/
COPY plant/ plant/
COPY oxplant/ oxplant/
COPY config/ config/

RUN useradd --system --uid 10001 --home /app oxplant && mkdir -p /app/data && chown -R oxplant:oxplant /app
USER oxplant

# Default: print help. docker-compose overrides the command per role.
CMD ["python", "-m", "oxplant", "--help"]
