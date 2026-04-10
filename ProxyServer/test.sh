#!/bin/bash
#
# test.sh — Interactive test script for ProxyServer
#
# Sends 5 sample HTTP requests through the proxy one at a time.
# Opens the dashboard so you can browse captured traffic between requests.
#
# Usage:
#   1. Start the proxy:  node server.js
#   2. In another terminal:  bash test.sh
#

set -e

PROXY="http://localhost:9080"
DASHBOARD="http://localhost:9081"

# Colors
BOLD='\033[1m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
DIM='\033[2m'
RESET='\033[0m'

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   ProxyServer Interactive Test Script    ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""

# Check if proxy is running
if ! curl -s --max-time 2 "$DASHBOARD/api/traffic" > /dev/null 2>&1; then
  echo -e "${YELLOW}⚠  ProxyServer doesn't seem to be running.${RESET}"
  echo -e "   Start it first:  ${CYAN}node server.js${RESET}"
  echo ""
  exit 1
fi

echo -e "${GREEN}✓${RESET} ProxyServer is running"
echo ""

# Open dashboard in default browser
echo -e "${CYAN}Opening dashboard at ${DASHBOARD}...${RESET}"
if command -v open &> /dev/null; then
  open "$DASHBOARD"
elif command -v xdg-open &> /dev/null; then
  xdg-open "$DASHBOARD"
else
  echo -e "  ${DIM}(Open ${DASHBOARD} in your browser manually)${RESET}"
fi

echo ""
echo -e "${DIM}The dashboard is now open. You'll see each request appear in real time.${RESET}"
echo -e "${DIM}Press Enter to send each request. Click on rows in the dashboard to inspect them.${RESET}"
echo ""

# ─────────────────────────────────────────────
# Request 1
# ─────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}[1/5] GET — Simple JSON API${RESET}"
echo -e "${DIM}  curl -x $PROXY http://httpbin.org/get${RESET}"
echo ""
echo -e "  Fetches a simple JSON response. Look at the dashboard for:"
echo -e "  - Status code 200 (green)"
echo -e "  - Content-Type: JSON"
echo -e "  - Response body with your request headers echoed back"
echo ""
read -p "  Press Enter to send → "
echo ""

echo -e "  ${CYAN}Sending...${RESET}"
curl -s -x "$PROXY" http://httpbin.org/get > /dev/null 2>&1
echo -e "  ${GREEN}✓ Done${RESET} — Check the dashboard!"
echo ""

# ─────────────────────────────────────────────
# Request 2
# ─────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}[2/5] POST — JSON Body${RESET}"
echo -e "${DIM}  curl -x $PROXY -X POST http://httpbin.org/post -H 'Content-Type: application/json' -d '{...}'${RESET}"
echo ""
echo -e "  Sends a POST with a JSON body. In the dashboard:"
echo -e "  - Click the row to see request details"
echo -e "  - Switch to the Request tab → Body section"
echo -e "  - You'll see the JSON body syntax-highlighted"
echo ""
read -p "  Press Enter to send → "
echo ""

echo -e "  ${CYAN}Sending...${RESET}"
curl -s -x "$PROXY" \
  -X POST http://httpbin.org/post \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "action": "login", "remember": true}' > /dev/null 2>&1
echo -e "  ${GREEN}✓ Done${RESET} — Click the POST row to see the request body!"
echo ""

# ─────────────────────────────────────────────
# Request 3
# ─────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}[3/5] GET — HTML Page${RESET}"
echo -e "${DIM}  curl -x $PROXY http://httpbin.org/html${RESET}"
echo ""
echo -e "  Fetches an HTML page. In the dashboard:"
echo -e "  - Content-Type column shows 'HTML'"
echo -e "  - Response body is syntax-highlighted HTML"
echo ""
read -p "  Press Enter to send → "
echo ""

echo -e "  ${CYAN}Sending...${RESET}"
curl -s -x "$PROXY" http://httpbin.org/html > /dev/null 2>&1
echo -e "  ${GREEN}✓ Done${RESET} — Check the Response tab for highlighted HTML!"
echo ""

# ─────────────────────────────────────────────
# Request 4
# ─────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}[4/5] PUT — Update with Custom Headers${RESET}"
echo -e "${DIM}  curl -x $PROXY -X PUT http://httpbin.org/put -H 'Authorization: Bearer tok_123' -d '{...}'${RESET}"
echo ""
echo -e "  Sends a PUT with an Authorization header. In the dashboard:"
echo -e "  - Method column shows PUT"
echo -e "  - Expand the Headers section to see the Authorization header"
echo -e "  - Try pressing 'j' and 'k' to navigate between requests"
echo ""
read -p "  Press Enter to send → "
echo ""

echo -e "  ${CYAN}Sending...${RESET}"
curl -s -x "$PROXY" \
  -X PUT http://httpbin.org/put \
  -H "Authorization: Bearer tok_123_secret" \
  -H "X-Request-Id: test-run-42" \
  -H "Content-Type: application/json" \
  -d '{"id": 42, "name": "Updated Item", "status": "active"}' > /dev/null 2>&1
echo -e "  ${GREEN}✓ Done${RESET} — Expand Headers to see Authorization + X-Request-Id!"
echo ""

# ─────────────────────────────────────────────
# Request 5
# ─────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}[5/5] DELETE — Error Response${RESET}"
echo -e "${DIM}  curl -x $PROXY -X DELETE http://httpbin.org/status/404${RESET}"
echo ""
echo -e "  Sends a DELETE that returns 404. In the dashboard:"
echo -e "  - Status code 404 shows in yellow"
echo -e "  - Use the Status filter dropdown to filter by 4xx"
echo -e "  - Try the URL filter to search for 'status'"
echo ""
read -p "  Press Enter to send → "
echo ""

echo -e "  ${CYAN}Sending...${RESET}"
curl -s -x "$PROXY" -X DELETE http://httpbin.org/status/404 > /dev/null 2>&1
echo -e "  ${GREEN}✓ Done${RESET} — Notice the yellow 404 status!"
echo ""

# ─────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}All 5 requests sent!${RESET}"
echo ""
echo -e "  Things to try in the dashboard now:"
echo -e "  ${CYAN}•${RESET} Click rows to view full request/response details"
echo -e "  ${CYAN}•${RESET} Use j/k keys to navigate up and down"
echo -e "  ${CYAN}•${RESET} Try the filter dropdowns (Method, Status, Type)"
echo -e "  ${CYAN}•${RESET} Type in the URL filter to search"
echo -e "  ${CYAN}•${RESET} Click 'Export HAR' to download a HAR file"
echo -e "  ${CYAN}•${RESET} Click 'Save' to save this session for later"
echo -e "  ${CYAN}•${RESET} Expand the AI Chat panel (bottom-right) and ask about the traffic"
echo ""
echo -e "  ${DIM}Dashboard: ${DASHBOARD}${RESET}"
echo ""
