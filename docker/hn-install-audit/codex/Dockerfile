FROM node:20-bookworm

ARG CODEX_NPM_PACKAGE="@openai/codex"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      python3 \
      python3-pip \
      python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "${CODEX_NPM_PACKAGE}"

RUN useradd --create-home --uid 10001 audit

ENV AI_HOST=codex \
    HOME=/home/audit \
    CODEX_HOME=/home/audit/.codex \
    AUDIT_OUTPUT_DIR=/audit-output

WORKDIR /workspace

COPY docker/hn-install-audit/common/install-audit.mjs /opt/hn-install-audit/install-audit.mjs
COPY docker/hn-install-audit/common/state-manager.mjs /opt/hn-install-audit/state-manager.mjs
COPY docker/hn-install-audit/validate-report.mjs /opt/hn-install-audit/validate-report.mjs

RUN chmod 0755 /opt/hn-install-audit/install-audit.mjs /opt/hn-install-audit/state-manager.mjs /opt/hn-install-audit/validate-report.mjs \
    && chown -R audit:audit /home/audit /workspace /opt/hn-install-audit

USER audit

ENTRYPOINT ["node", "/opt/hn-install-audit/install-audit.mjs"]
