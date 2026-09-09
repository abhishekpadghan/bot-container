#!/bin/bash
# ─────────────────────────────────────────────────────────────
# run.sh — Convenience wrapper for Podman/Docker Compose
# Usage:
#   ./run.sh start          → Start bot + dashboard
#   ./run.sh stop           → Stop all services
#   ./run.sh logs           → Tail live logs
#   ./run.sh report         → Show today's P&L report
#   ./run.sh status         → Show container status
#   ./run.sh shell          → Open bash shell inside bot
#   ./run.sh restart        → Restart bot only
#   ./run.sh build          → Rebuild the image
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/podman-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"

# ── Detect: use podman compose or docker compose ─────────────
if command -v podman &>/dev/null; then
    COMPOSE="podman compose"
elif command -v docker &>/dev/null; then
    COMPOSE="docker compose"
else
    echo "❌ Neither podman nor docker found. Please install one."
    exit 1
fi

# ── Ensure .env exists ────────────────────────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "⚠️  .env not found. Copying from .env.example..."
    cp "${SCRIPT_DIR}/.env.example" "${ENV_FILE}"
    echo "✅ Created .env — Please fill in your API credentials before starting."
    exit 1
fi

CMD="${1:-help}"

case "$CMD" in

    start)
        echo "🚀 Starting Nifty Bot (mode: ${TRADING_MODE:-paper})..."
        $COMPOSE -f "$COMPOSE_FILE" up -d bot dashboard
        echo "✅ Bot running. Dashboard → http://localhost:8080"
        ;;

    stop)
        echo "🛑 Stopping all services..."
        $COMPOSE -f "$COMPOSE_FILE" down
        echo "✅ All services stopped."
        ;;

    restart)
        echo "🔄 Restarting bot..."
        $COMPOSE -f "$COMPOSE_FILE" restart bot
        ;;

    logs)
        echo "📋 Live logs (Ctrl+C to exit)..."
        $COMPOSE -f "$COMPOSE_FILE" logs -f bot
        ;;

    status)
        echo "📊 Container Status:"
        $COMPOSE -f "$COMPOSE_FILE" ps
        ;;

    build)
        echo "🔨 Building image..."
        $COMPOSE -f "$COMPOSE_FILE" build --no-cache bot
        echo "✅ Build complete."
        ;;

    report)
        echo "📈 Running P&L report..."
        $COMPOSE -f "$COMPOSE_FILE" run --rm --no-deps cli report.py
        ;;

    shell)
        echo "🐚 Opening shell inside bot container..."
        $COMPOSE -f "$COMPOSE_FILE" run --rm --no-deps --entrypoint bash cli
        ;;

    help|*)
        echo ""
        echo "  Nifty Bot — Command Reference"
        echo "  ─────────────────────────────────────"
        echo "  ./run.sh start      Start bot + dashboard"
        echo "  ./run.sh stop       Stop all containers"
        echo "  ./run.sh restart    Restart the bot"
        echo "  ./run.sh logs       Tail live logs"
        echo "  ./run.sh status     Show container status"
        echo "  ./run.sh build      Rebuild Docker image"
        echo "  ./run.sh report     Print today's P&L report"
        echo "  ./run.sh shell      Open bash inside container"
        echo ""
        ;;
esac
