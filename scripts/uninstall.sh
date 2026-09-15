#!/bin/sh
set -eu

AGENTROUTE_HOME_DIR=${AGENTROUTE_HOME:-"$HOME/.agentroute"}
printf 'AgentRoute keeps all installed state under %s.\n' "$AGENTROUTE_HOME_DIR"
printf 'Remove that directory and the tagged PATH line from .zshrc manually after preserving any audit data you want.\n'

