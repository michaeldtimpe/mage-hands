# Build context = repo root (mage-hands/) so the image can include ../common.
# See compose.yaml: build.context = .., build.dockerfile = router-hands/Dockerfile
FROM python:3.12-slim

# openssh-client provides `ssh` — this relay reaches the ROUTER over SSH (it does NOT touch its
# own host's namespaces, unlike synology-hands' nsenter approach), so there is no util-linux here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Shared relay core (reused by synology-hands and router-hands).
COPY common /app/common
RUN uv pip install --system /app/common

# Appliance-specific server. The SSH private key is NEVER copied in — it is bind-mounted at
# runtime (see compose.yaml) so the image stays reusable and secret-free.
COPY router-hands/server.py /app/server.py
WORKDIR /app

CMD ["python", "server.py"]
