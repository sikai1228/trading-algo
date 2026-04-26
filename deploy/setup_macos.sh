#!/bin/zsh
# Idempotent installer for the trumpbot launchd agent on macOS.
#
# Why this exists: launchd-spawned processes can't read ~/Desktop
# without "Full Disk Access" entitlement (TCC restriction since
# Mojave). The wrapper script must live OUTSIDE Desktop. This
# installer copies it to ~/Library/Application Support/trumpbot/bin/,
# substitutes __HOME__ in the plist, and (re)loads the agent.
#
# Run from the repo root:
#     deploy/setup_macos.sh
#
# Re-run anytime — it's idempotent: existing wrapper + plist are
# replaced with the current versions and the agent is reloaded.
#
# Prerequisites:
#   - ~/.config/trumpbot/secrets.env exists (mode 0600) with at
#     least TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, KALSHI_API_KEY_ID,
#     KALSHI_PRIVATE_KEY_PASSPHRASE, ANTHROPIC_API_KEY filled in.
#   - ~/.config/trumpbot/config.yaml exists.
#   - uv is installed at ~/.local/bin/uv (the wrapper hard-codes
#     this path because launchd's PATH is minimal).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER_DIR="$HOME/Library/Application Support/trumpbot/bin"
WRAPPER_DST="$WRAPPER_DIR/run_trumpbot.sh"
PLIST_DST="$HOME/Library/LaunchAgents/com.trumpbot.daemon.plist"
LABEL="com.trumpbot.daemon"
LOG_DIR="$HOME/Library/Logs/trumpbot"

echo "[setup] repo: $REPO_DIR"
echo "[setup] wrapper -> $WRAPPER_DST"
echo "[setup] plist   -> $PLIST_DST"

# ---------------------------------------------------------------------
# Sanity check secrets + config exist
# ---------------------------------------------------------------------
if [[ ! -f "$HOME/.config/trumpbot/secrets.env" ]]; then
    echo "ERROR: $HOME/.config/trumpbot/secrets.env not found" >&2
    echo "       Create it from config/secrets.example.env first." >&2
    exit 1
fi
if [[ ! -f "$HOME/.config/trumpbot/config.yaml" ]]; then
    echo "ERROR: $HOME/.config/trumpbot/config.yaml not found" >&2
    exit 1
fi

# ---------------------------------------------------------------------
# Install wrapper to TCC-friendly location.
# ---------------------------------------------------------------------
mkdir -p "$WRAPPER_DIR"
cp "$REPO_DIR/deploy/run_trumpbot.sh" "$WRAPPER_DST"
chmod +x "$WRAPPER_DST"

# ---------------------------------------------------------------------
# Install plist.
# ---------------------------------------------------------------------
mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$PLIST_DST")"
sed "s|__HOME__|$HOME|g" "$REPO_DIR/deploy/com.trumpbot.daemon.plist" > "$PLIST_DST"

# ---------------------------------------------------------------------
# (Re)load the agent. unload-then-load is safer than `launchctl
# kickstart` because it picks up any plist changes.
# ---------------------------------------------------------------------
if launchctl list | grep -q "$LABEL"; then
    echo "[setup] unloading existing $LABEL"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    sleep 1
fi
echo "[setup] loading $LABEL"
launchctl load "$PLIST_DST"
sleep 4

# ---------------------------------------------------------------------
# Verify it actually started.
# ---------------------------------------------------------------------
if ! launchctl list | grep -q "$LABEL"; then
    echo "ERROR: $LABEL didn't load. Check $LOG_DIR/stderr.log" >&2
    exit 1
fi

PID=$(launchctl list | awk -v lbl="$LABEL" '$3 == lbl {print $1}')
echo "[setup] $LABEL is running, PID=$PID"
echo "[setup] tailing first 10 startup events:"
sleep 2
grep -E "trumpbot_starting|telegram_bot_started|trumpbot_started|_loop_started" \
    "$LOG_DIR/stdout.log" | tail -10 || true

echo ""
echo "[setup] done. Send /help to your bot to confirm."
echo "[setup] tail logs: tail -f $LOG_DIR/stdout.log"
