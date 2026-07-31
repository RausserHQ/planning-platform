ARG WINDMILL_BASE
FROM ${WINDMILL_BASE}

ARG PLANNING_PLATFORM_VERSION=0.1.0

USER root
COPY dist/ /tmp/planning-platform-dist/
RUN uv pip install --require-hashes \
      --target /opt/planning-platform \
      --requirement /tmp/planning-platform-dist/requirements.lock \
    && uv pip install --no-deps \
      --target /opt/planning-platform \
      "/tmp/planning-platform-dist/planning_platform-${PLANNING_PLATFORM_VERSION}-py3-none-any.whl" \
    && bun install -g windmill-cli@1.775.2 \
    && rm -rf /tmp/planning-platform-dist \
    && find /opt/planning-platform -type d -exec chmod 0755 {} + \
    && find /opt/planning-platform -type f -exec chmod 0644 {} +

ENV ADDITIONAL_PYTHON_PATHS=/opt/planning-platform \
    PIP_LOCAL_DEPENDENCIES="^planning-platform([<=> !].*)?$"

USER 1000:1000
