FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN python -m pip install "uv==0.11.27" \
    && uv export --frozen --no-dev --no-emit-project --no-header \
      --no-annotate --format requirements.txt --output-file requirements.lock \
    && python -m pip wheel --require-hashes --wheel-dir /wheels \
      --requirement requirements.lock \
    && python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --gid 10001 planner \
    && useradd --uid 10001 --gid planner --no-create-home --shell /usr/sbin/nologin planner \
    && python -m venv /opt/venv
COPY --from=builder /wheels /wheels
RUN /opt/venv/bin/pip install --no-cache-dir --no-index \
      --find-links=/wheels planning-platform==0.1.0 \
    && rm -rf /wheels

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["uvicorn"]
CMD ["planning_platform.planner.api:app", "--host=0.0.0.0", "--port=8080", "--no-access-log"]
