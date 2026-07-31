FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

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

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG PLANNING_PLATFORM_VERSION=0.1.8

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --gid 10001 planner \
    && useradd --uid 10001 --gid planner --no-create-home --shell /usr/sbin/nologin planner \
    && python -m venv /opt/venv
COPY --from=builder /wheels /wheels
RUN /opt/venv/bin/pip install --no-cache-dir --no-index \
      --find-links=/wheels "planning-platform==${PLANNING_PLATFORM_VERSION}" \
    && rm -rf /wheels

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["uvicorn"]
CMD ["planning_platform.planner.api:app", "--host=0.0.0.0", "--port=8080", "--no-access-log"]
