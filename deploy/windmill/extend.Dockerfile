ARG WINDMILL_BASE=ghcr.io/windmill-labs/windmill:1.775.2@sha256:ef39329523f4806e5cd5169ffa7af2618f39439bcf659115e8bb804c592d7132
FROM ${WINDMILL_BASE}

ARG PLANNING_PLATFORM_VERSION=0.1.19
ARG NPM_VERSION=11.19.0

USER root
COPY dist/ /tmp/planning-platform-dist/
COPY windmill/ /opt/planning-platform-workspace/
RUN npm install --global "npm@${NPM_VERSION}" \
    && test "$(npm --version)" = "${NPM_VERSION}" \
    && test "$(node -p 'require(process.argv[1] + "/npm/node_modules/tar/package.json").version' "$(npm root --global)")" = "7.5.19" \
    && npm cache clean --force \
    && build_uv_cache="$(mktemp -d /tmp/planning-platform-build-uv.XXXXXX)" \
    && export UV_CACHE_DIR="${build_uv_cache}" \
    && windmill_python="$(uv python find 3.12 --system --python-preference only-managed)" \
    && "${windmill_python}" -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
    && uv pip install --python "${windmill_python}" --require-hashes \
      --target /opt/planning-platform \
      --requirement /tmp/planning-platform-dist/requirements.lock \
    && uv pip install --python "${windmill_python}" --no-deps \
      --target /opt/planning-platform \
      "/tmp/planning-platform-dist/planning_platform-${PLANNING_PLATFORM_VERSION}-py3-none-any.whl" \
    && PYTHONPATH=/opt/planning-platform "${windmill_python}" -c \
      'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from importlib.metadata import version; from psycopg import pq; import sys; assert version("planning-platform") == sys.argv[1]; assert pq.__impl__ == "binary"; assert AESGCM is not None' \
      "${PLANNING_PLATFORM_VERSION}" \
    && rm -rf "${build_uv_cache:?}" \
    && rm -f /usr/bin/wmill \
    && npm install --global "windmill-cli@1.775.2" \
    && wmill_version="$(wmill --version)" \
    && printf '%s\n' "$wmill_version" | grep -Fx "CLI version: 1.775.2" >/dev/null \
    && rm -rf /tmp/planning-platform-dist \
    && find /opt/planning-platform -type d -exec chmod 0755 {} + \
    && find /opt/planning-platform -type f -exec chmod 0644 {} + \
    && find /opt/planning-platform-workspace -type d -exec chmod 0755 {} + \
    && find /opt/planning-platform-workspace -type f -exec chmod 0644 {} +

ENV PYTHONPATH=/opt/planning-platform \
    ADDITIONAL_PYTHON_PATHS=/opt/planning-platform \
    PIP_LOCAL_DEPENDENCIES="^planning-platform([<=> !].*)?$"

USER 1000:1000
