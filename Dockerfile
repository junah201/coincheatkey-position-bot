FROM python:3.14-slim

# Timezone
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Essential packages
RUN apt-get update && apt-get install -y \
    git \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# uv install
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# env variables for uv
ENV UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT="/usr/local" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# dependencies
COPY pyproject.toml uv.lock* ./

# uv
RUN uv pip install --system -r pyproject.toml

# checkout
COPY . .

# run
CMD ["python", "main.py"]
