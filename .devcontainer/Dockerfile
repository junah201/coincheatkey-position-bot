FROM python:3.14-slim

# Timezone
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# essential packages
RUN apt-get update && apt-get install -y \
    git \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# uv install
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# set env variables
ENV UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT="/usr/local" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
