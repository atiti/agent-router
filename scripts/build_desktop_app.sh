#!/bin/sh
set -eu

AGENTROUTE_HOME_DIR=${AGENTROUTE_HOME:-"$HOME/.agentroute"}
# Use the same package-layout discovery and signing path as the public command.
exec "$AGENTROUTE_HOME_DIR/bin/agentroute" desktop install \
    --source "${1:-/Applications/ChatGPT.app}" \
    --destination "${2:-/Applications/ChatGPT-Routed.app}" \
    --signing-identity "${AGENTROUTE_DESKTOP_SIGNING_IDENTITY:--}"
