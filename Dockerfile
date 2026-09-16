FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" QUALITY_STATE_DIR=/state
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.15 && useradd --uid 10001 --create-home app
COPY pyproject.toml uv.lock README.md ./
COPY agent ./agent
COPY quality ./quality
COPY data ./data
COPY datasets ./datasets
COPY scripts ./scripts
RUN uv sync --locked --extra production --no-dev && mkdir -p /state && chown app:app /state
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
