#!/usr/bin/env bash
# ============================================================
# DATUM — Start Script
# FORGE · ANVIL · PANEL
#
# Usage:
#   ./start.sh              # Start everything
#   ./start.sh --setup      # First-time setup only
#   ./start.sh --stop       # Stop all processes
#   ./start.sh --status     # Check what's running
#   ./start.sh --once       # Run one loop of FORGE + ANVIL then stop
# ============================================================

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_DIR/.env"

RED='\033[0;31m'; YELLOW='\033[0;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

banner() {
  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}║                   DATUM                      ║${NC}"
  echo -e "${BOLD}║          FORGE  ·  ANVIL  ·  PANEL           ║${NC}"
  echo -e "${BOLD}║   Document & Code Intelligence for Shops     ║${NC}"
  echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
  echo ""
}

check_env() {
  if [ ! -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}⚠  No .env found. Copying from .env.example...${NC}"
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    echo -e "${RED}▶  Edit $ENV_FILE with your settings before running.${NC}"
    exit 1
  fi
  source "$ENV_FILE"
}

check_deps() {
  echo -e "${CYAN}Checking dependencies...${NC}"
  command -v python3 >/dev/null || { echo "Python 3 required"; exit 1; }
  command -v pip >/dev/null    || { echo "pip required"; exit 1; }
  echo -e "  Python:  ${GREEN}$(python3 --version)${NC}"

  if curl -sf "http://localhost:11434/api/tags" >/dev/null 2>&1; then
    echo -e "  Ollama:  ${GREEN}Running${NC}"
  else
    echo -e "  Ollama:  ${YELLOW}Not running${NC}"
    echo -e "           Install: curl -fsSL https://ollama.com/install.sh | sh"
    echo -e "           Models:  ollama pull llama3.1 && ollama pull mistral && ollama pull nomic-embed-text"
  fi

  if command -v pm2 >/dev/null 2>&1; then
    echo -e "  PM2:     ${GREEN}Available${NC}"
  else
    echo -e "  PM2:     ${YELLOW}Not found — install for production: npm install -g pm2${NC}"
  fi
}

install_deps() {
  echo -e "${CYAN}Installing Python dependencies...${NC}"
  pip install -r "$REPO_DIR/requirements.txt" --break-system-packages -q
  echo -e "${GREEN}✓ Dependencies installed${NC}"
}

create_dirs() {
  mkdir -p \
    "$REPO_DIR/forge/logs" "$REPO_DIR/forge/staging" \
    "$REPO_DIR/forge/processed" "$REPO_DIR/forge/reports" \
    "$REPO_DIR/forge/memory" "$REPO_DIR/forge/review_queue" \
    "$REPO_DIR/anvil/logs" "$REPO_DIR/anvil/reports" \
    "$REPO_DIR/anvil/review_queue" \
    "$REPO_DIR/logs"
  echo -e "${GREEN}✓ Directories ready${NC}"
}

start_with_pm2() {
  echo -e "${CYAN}Starting with PM2...${NC}"

  pm2 start "$REPO_DIR/forge/forge.py" \
    --interpreter python3 --name datum-forge \
    -- --config "$REPO_DIR/forge/config/forge.yaml" --path "$REPO_DIR/forge" \
    2>/dev/null || pm2 restart datum-forge

  pm2 start "$REPO_DIR/anvil/anvil.py" \
    --interpreter python3 --name datum-anvil \
    -- --config "$REPO_DIR/anvil/config/anvil.yaml" \
    2>/dev/null || pm2 restart datum-anvil

  pm2 start "$REPO_DIR/panel/server.py" \
    --interpreter python3 --name datum-panel \
    -- --port "${PANEL_PORT:-4000}" \
       --forge "$REPO_DIR/forge" \
       --anvil "$REPO_DIR/anvil" \
    2>/dev/null || pm2 restart datum-panel

  pm2 start "$REPO_DIR/forge/watcher.py" \
    --interpreter python3 --name datum-watcher \
    -- --path "$REPO_DIR/forge/staging" \
       --config "$REPO_DIR/forge/config/forge.yaml" \
    2>/dev/null || pm2 restart datum-watcher

  pm2 save
  echo ""
  echo -e "${GREEN}✓ DATUM started${NC}"
  pm2 list
}

start_direct() {
  echo -e "${CYAN}Starting directly...${NC}"
  echo -e "${YELLOW}  Ctrl+C to stop. Install PM2 for production.${NC}"
  echo ""

  python3 "$REPO_DIR/panel/server.py" \
    --port "${PANEL_PORT:-4000}" \
    --forge "$REPO_DIR/forge" \
    --anvil "$REPO_DIR/anvil" &
  PANEL_PID=$!
  echo -e "${GREEN}✓ PANEL  → http://localhost:${PANEL_PORT:-4000}${NC}"

  python3 "$REPO_DIR/forge/forge.py" \
    --config "$REPO_DIR/forge/config/forge.yaml" \
    --path "$REPO_DIR/forge" &
  FORGE_PID=$!
  echo -e "${GREEN}✓ FORGE  running (PID $FORGE_PID)${NC}"

  python3 "$REPO_DIR/anvil/anvil.py" \
    --config "$REPO_DIR/anvil/config/anvil.yaml" &
  ANVIL_PID=$!
  echo -e "${GREEN}✓ ANVIL  running (PID $ANVIL_PID)${NC}"

  trap "kill $PANEL_PID $FORGE_PID $ANVIL_PID 2>/dev/null; echo 'DATUM stopped.'" EXIT
  wait
}

run_once() {
  echo -e "${CYAN}Running one loop of FORGE + ANVIL...${NC}"
  python3 "$REPO_DIR/forge/forge.py" --once \
    --config "$REPO_DIR/forge/config/forge.yaml"
  python3 "$REPO_DIR/anvil/anvil.py" --once \
    --config "$REPO_DIR/anvil/config/anvil.yaml"
  echo -e "${GREEN}✓ Done${NC}"
}

print_status() {
  if command -v pm2 >/dev/null 2>&1; then pm2 list; fi
  echo -e "${CYAN}PANEL:${NC} http://localhost:${PANEL_PORT:-4000}"
}

stop_all() {
  if command -v pm2 >/dev/null 2>&1; then
    pm2 stop datum-forge datum-anvil datum-panel datum-watcher 2>/dev/null || true
    echo -e "${GREEN}✓ DATUM stopped${NC}"
  else
    echo "PM2 not installed — kill processes manually"
  fi
}

# ── Main ──────────────────────────────────────────────────
banner

case "${1:-}" in
  --setup)
    check_env; check_deps; install_deps; create_dirs
    echo -e "\n${GREEN}✓ Setup complete. Run ./start.sh to start DATUM.${NC}"
    ;;
  --stop)    stop_all ;;
  --status)  print_status ;;
  --once)    check_env; run_once ;;
  *)
    check_env; check_deps; create_dirs
    echo ""
    if command -v pm2 >/dev/null 2>&1; then
      start_with_pm2
    else
      start_direct
    fi
    echo ""
    echo -e "${BOLD}▶  PANEL:${NC} http://localhost:${PANEL_PORT:-4000}"
    ;;
esac
