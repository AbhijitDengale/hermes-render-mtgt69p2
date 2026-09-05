# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render, pre-baked with Render tooling.
#
# Extends the upstream NousResearch/hermes-agent image with:
#   - A bundle of Render-focused skills mounted via skills.external_dirs
#   - A boot-time patcher that registers the Render MCP server in
#     config.yaml (idempotent; never overwrites user edits)
#
# We deliberately do NOT install the `render` CLI. This image is configured
# around the Render MCP server; installing extra CLIs should be a conscious
# operator choice, not something the agent does as an automatic fallback.
#
# Pin the upstream tag here. Bump and redeploy to upgrade Hermes.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.8.27
FROM ${HERMES_IMAGE}

# Workarounds for upstream issues that prevent the dashboard's Chat tab
# from connecting on hosted deploys. Baked into the image so the runtime
# command stays simple. See render.yaml comments + the README for context.
#   - chown: dashboard runs as `hermes` but ui-tui/ + node_modules/ ship root-owned
#   - touch ink-bundle.js: short-circuits _hermes_ink_bundle_stale()
#   - touch entry.js: bumps mtime above source .ts files so _tui_build_needed() returns False
USER root
RUN chown -R hermes:hermes /opt/hermes/ui-tui /opt/hermes/node_modules \
 && mkdir -p /opt/hermes/ui-tui/packages/hermes-ink/dist /opt/hermes/ui-tui/dist \
 && touch /opt/hermes/ui-tui/packages/hermes-ink/dist/ink-bundle.js \
          /opt/hermes/ui-tui/dist/entry.js \
 && chown -R hermes:hermes /opt/hermes/ui-tui

# Memory-provider client libraries.
#
# Hermes ships the provider *plugins* (plugins/memory/honcho, supermemory, …)
# but not their client SDKs, so `hermes memory status` reports the provider as
# unavailable until the package is present. This MUST be baked into the image:
# /opt/hermes/.venv lives in the image layer, so a runtime `uv pip install` is
# silently discarded on the next deploy and memory falls back without erroring.
#
# The venv is uv-managed and has no pip, so install via uv (already on PATH
# in the upstream image). The import check fails the build early rather than
# shipping an image where memory quietly does not work.
#
# EMPTY ON PURPOSE. This deployment runs Hermes' built-in memory only
# (memory.provider: "" -> MEMORY.md / USER.md under each profile's
# HERMES_HOME). Honcho was removed on 2026-09-05: the SDK is no longer
# installed and nothing imports it. The plugin still ships with Hermes, and
# both of its `from honcho import ...` statements sit inside `if TYPE_CHECKING:`
# blocks, so its absence is inert -- verified by hiding the package and running
# a real write/recall turn, `hermes memory status` and the watchdog suite, all
# of which passed.
#
# To adopt a provider later: set MEMORY_PACKAGES (e.g. "honcho-ai"), set
# memory.provider in config.yaml, and supply its key.
ARG MEMORY_PACKAGES=""
RUN if [ -n "${MEMORY_PACKAGES}" ]; then \
      VIRTUAL_ENV=/opt/hermes/.venv uv pip install --no-cache ${MEMORY_PACKAGES}; \
      echo "memory client(s) installed: ${MEMORY_PACKAGES}"; \
    else \
      echo "no external memory provider: built-in MEMORY.md/USER.md only"; \
    fi

# Pull the official Render skill bundle from github.com/render-oss/skills
# at a pinned commit. Mounted via skills.external_dirs at boot, so the
# upstream Hermes skills-sync flow never touches these files. To upgrade,
# bump RENDER_SKILLS_REF (a commit SHA, tag, or branch) and rebuild.
ARG RENDER_SKILLS_REPO=render-oss/skills
ARG RENDER_SKILLS_REF=1b8496570748203351f628b2ae738805ac2c23d5
RUN set -eu; \
    tmp="$(mktemp -d)"; \
    url="https://codeload.github.com/${RENDER_SKILLS_REPO}/tar.gz/${RENDER_SKILLS_REF}"; \
    curl -fsSL --retry 3 -o "${tmp}/skills.tar.gz" "${url}"; \
    tar -xzf "${tmp}/skills.tar.gz" -C "${tmp}"; \
    extracted="$(find "${tmp}" -maxdepth 2 -type d -name 'skills' | head -n 1)"; \
    test -n "${extracted}" || { echo "could not find skills/ in tarball" >&2; exit 1; }; \
    install -d -o hermes -g hermes -m 0755 /opt/render-tools/skills-upstream; \
    cp -a "${extracted}/." /opt/render-tools/skills-upstream/; \
    chown -R hermes:hermes /opt/render-tools/skills-upstream; \
    rm -rf "${tmp}"; \
    echo "${RENDER_SKILLS_REPO}@${RENDER_SKILLS_REF}" > /opt/render-tools/skills-upstream/.source

# Local overlay: a Hermes-specific `render-on-hermes` skill that tells
# the agent the MCP server is pre-wired (so skip "install MCP" from
# upstream skills) and that the CLI is deliberately absent (so don't
# try to invoke it). Listed FIRST in skills.external_dirs so same-named
# overlays would shadow upstream entries.
COPY --chown=hermes:hermes skills/ /opt/render-tools/skills-local/

# Boot-time wrapper: patches /opt/data/config.yaml, then hands off to
# the upstream entrypoint chain (tini → docker/entrypoint.sh).
COPY --chown=root:root scripts/bootstrap.sh /opt/render-tools/bootstrap.sh
COPY --chown=root:root scripts/patch-config.py /opt/render-tools/patch-config.py
RUN chmod 0755 /opt/render-tools/bootstrap.sh /opt/render-tools/patch-config.py

# Pre-create the dir the patcher writes to so chown works cleanly on
# first boot. The mounted disk replaces this empty dir at runtime;
# baking it just keeps the image self-contained for any non-disk use.
RUN install -d -o hermes -g hermes -m 0755 /opt/data

# Stay as root so the bootstrap can chown the mounted /opt/data on first
# boot, then `gosu hermes` for the config patch, then exec the upstream
# entrypoint dispatcher (which also runs as root and does its own drop).
#
# No tini. /usr/bin/tini in this image is not tini -- it is a shim whose
# fallback branch is `exec /init main-wrapper.sh "$@"`, so the real tini flags
# we used to pass (`-g --`) were forwarded verbatim to main-wrapper.sh, which
# has no way to interpret them. The main program then died immediately, and a
# container whose main program has exited is a container s6-overlay starts
# shutting down -- which is how this image ended up sitting in a hung
# `s6-rc -bda change` with no gateway. s6-overlay's own /init is PID 1 here and
# does the signal handling and zombie reaping tini would have provided.
#
# bootstrap.sh must therefore be exec'd directly, so that it inherits PID 1 and
# its own exec keeps it.
ENTRYPOINT ["/opt/render-tools/bootstrap.sh"]
CMD ["gateway", "run"]
