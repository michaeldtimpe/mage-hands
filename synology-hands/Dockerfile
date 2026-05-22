# Build context = repo root (mage-hands/) so the image can include ../common.
# See compose.yaml: build.context = .., build.dockerfile = synology-hands/Dockerfile
FROM python:3.12-slim

# util-linux provides `nsenter` (used to run commands in the host namespaces).
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Shared relay core (reused by synology-hands and future appliance relays).
COPY common /app/common
RUN uv pip install --system /app/common

# Appliance-specific server + its modules.
COPY synology-hands/server.py /app/server.py
COPY synology-hands/firewall.py /app/firewall.py
WORKDIR /app

CMD ["python", "server.py"]
