#!/bin/sh
# Entrypoint wrapper for the render-tools image.
#
# Runs as root (PID-1 child of tini). On every boot it:
#   1. Ensures /opt/data exists and is owned by hermes:hermes.
#   2. Runs the config patcher as the hermes user. The patcher is
#      idempotent: it only INSERTs the Render MCP server and the
#      skills.external_dirs entry; it never overwrites user edits.
#   3. Exec's the upstream entrypoint dispatcher with the original args
#      (default CMD is `gateway run`), which brings up s6 and then runs
#      that CMD as the container's main program.
#
# The upstream entrypoint also chowns /opt/data and drops to the hermes
# user via gosu for the gateway process. Our chown here is redundant in
# the happy path but harmless, and it lets the patcher run on a fresh
# disk that hasn't been chowned yet.

set -eu

DATA_DIR="${HERMES_HOME:-/opt/data}"
PATCHER="/opt/render-tools/patch-config.py"

# Make sure the data dir exists and the hermes user can write to it
# before we run the patcher. Idempotent — if /opt/data is already a
# mounted, chowned disk this is a no-op.
mkdir -p "${DATA_DIR}"
if ! chown -R hermes:hermes "${DATA_DIR}" 2>/dev/null; then
  echo "[render-tools] warning: could not chown ${DATA_DIR}; continuing" >&2
fi

# Patch config.yaml. We never fail the boot on a patch error — the agent
# can still run without the Render MCP server registered, and the user
# can always add it manually from the dashboard.
if [ -x "${PATCHER}" ]; then
  if ! gosu hermes "${PATCHER}" "${DATA_DIR}/config.yaml"; then
    echo "[render-tools] warning: config patch failed; continuing with unmodified config" >&2
  fi
else
  echo "[render-tools] warning: ${PATCHER} not found or not executable; skipping" >&2
fi

# Hand off to the upstream entrypoint dispatcher, which delegates to
# s6-overlay's /init and then runs the CMD through main-wrapper.sh.
#
# NOT docker/entrypoint.sh. That is now a compatibility shim which runs the
# cont-init bootstrap and then returns WITHOUT exec'ing the CMD -- its own
# warning says so. Against a newer base image it left the container with s6
# up, every service registered, and no main program, so s6-overlay began
# shutting straight back down; whether the gateway happened to start before
# that sweep reached it was a race. It lost that race once for eighteen hours.
#
# `exec` matters: this script is PID 1, and the dispatcher only delegates to
# /init when it is PID 1 itself. Losing that would silently drop the container
# to the unsupervised fallback path, where the dashboard service does not exist.
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
