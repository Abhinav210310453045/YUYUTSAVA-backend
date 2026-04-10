# Deep Agents tutorial: local Docker sandbox (python3 required for BaseSandbox file tools).
# Build: docker build -t deepagent-sandbox:local -f tutorials/docker_sandbox/Dockerfile .
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        grep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Long-running no-op; the host starts the container and uses docker exec.
CMD ["sleep", "infinity"]
