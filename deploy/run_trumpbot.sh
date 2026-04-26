#!/bin/zsh
# Wrapper for launchd. Sources ~/.config/trumpbot/secrets.env (the
# canonical secrets file, mode 0600) and execs the daemon. The
# launchd plist points at THIS script, not at uv directly, so the
# Anthropic / Kalshi / Telegram credentials are populated as env
# vars before the daemon's config loader does ${ENV_VAR} substitution.
#
# Why a wrapper instead of plist EnvironmentVariables: keeping
# secrets in the LaunchAgent plist (~/Library/LaunchAgents/...) would
# spread them across multiple files. secrets.env is the single
# 0600-permissioned source of truth.
set -e

if [[ -f "$HOME/.config/trumpbot/secrets.env" ]]; then
  set -a
  source "$HOME/.config/trumpbot/secrets.env"
  set +a
else
  echo "WARN: $HOME/.config/trumpbot/secrets.env not found; daemon will run with empty creds" >&2
fi

cd "$HOME/Desktop/Auto Trading"
exec "$HOME/.local/bin/uv" run --directory "$HOME/Desktop/Auto Trading" \
  python -m trumpbot --config "$HOME/.config/trumpbot/config.yaml"
