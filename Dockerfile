FROM python:3.12-slim

LABEL org.opencontainers.image.title="FitWeek API" \
    org.opencontainers.image.description="MySQL 8.4 LTS with degradable Redis baseline"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system fitweek \
    && useradd --system --gid fitweek --home-dir /app fitweek

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir -e ".[dev]"

COPY --chown=fitweek:fitweek . .
RUN chmod +x /app/scripts/docker-entrypoint.sh

USER fitweek

EXPOSE 8000

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
