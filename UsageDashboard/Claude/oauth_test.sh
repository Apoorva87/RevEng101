#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   export ACCESS_TOKEN="..."
#   export REFRESH_TOKEN="..."
#   export TOKEN_URL="https://YOUR_AUTH_SERVER/oauth/token"
#   export API_URL="https://YOUR_API_SERVER/v1/some-endpoint"
#   ./oauth_test.sh
#
# Notes:
# - This script does NOT extract tokens from any app.
# - You must supply your own tokens and endpoints.
# - Adjust request payloads as needed for your provider.

: "${ACCESS_TOKEN:?ACCESS_TOKEN is required}"
: "${REFRESH_TOKEN:?REFRESH_TOKEN is required}"
: "${TOKEN_URL:?TOKEN_URL is required}"
: "${API_URL:?API_URL is required}"

# Optional client auth, if your provider requires it for refresh.
CLIENT_ID="${CLIENT_ID:-}"
CLIENT_SECRET="${CLIENT_SECRET:-}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

call_api() {
  local token="$1"
  local body_file="$TMPDIR/api_body.json"
  local headers_file="$TMPDIR/api_headers.txt"

  echo "==> Calling API with access token..."
  http_code=$(
    curl -sS \
      -D "$headers_file" \
      -o "$body_file" \
      -w "%{http_code}" \
      -X POST "$API_URL" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      --data '{"message":"Hello from OAuth token test"}'
  )

  echo "HTTP status: $http_code"
  echo "--- Response headers ---"
  cat "$headers_file"
  echo
  echo "--- Response body ---"
  cat "$body_file"
  echo
  echo "------------------------"

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    return 0
  fi

  # Heuristic for expired/invalid token.
  if grep -Eqi 'expired|invalid token|invalid_token|token expired|unauthorized' "$body_file"; then
    return 10
  fi

  return 1
}

refresh_access_token() {
  local body_file="$TMPDIR/refresh_body.json"
  local headers_file="$TMPDIR/refresh_headers.txt"

  echo "==> Refreshing access token..."

  local curl_args=(
    -sS
    -D "$headers_file"
    -o "$body_file"
    -w "%{http_code}"
    -X POST "$TOKEN_URL"
    -H "Content-Type: application/x-www-form-urlencoded"
    --data-urlencode "grant_type=refresh_token"
    --data-urlencode "refresh_token=$REFRESH_TOKEN"
  )

  if [[ -n "$CLIENT_ID" ]]; then
    curl_args+=( --data-urlencode "client_id=$CLIENT_ID" )
  fi

  if [[ -n "$CLIENT_SECRET" ]]; then
    curl_args+=( --data-urlencode "client_secret=$CLIENT_SECRET" )
  fi

  http_code=$(curl "${curl_args[@]}")

  echo "HTTP status: $http_code"
  echo "--- Refresh response headers ---"
  cat "$headers_file"
  echo
  echo "--- Refresh response body ---"
  cat "$body_file"
  echo
  echo "-------------------------------"

  if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "Refresh failed."
    return 1
  fi

  if command -v jq >/dev/null 2>&1; then
    NEW_ACCESS_TOKEN="$(jq -r '.access_token // empty' "$body_file")"
    NEW_REFRESH_TOKEN="$(jq -r '.refresh_token // empty' "$body_file")"
  else
    echo "jq not found; cannot safely parse JSON."
    echo "Install jq with: brew install jq"
    return 1
  fi

  if [[ -z "${NEW_ACCESS_TOKEN:-}" ]]; then
    echo "No access_token found in refresh response."
    return 1
  fi

  ACCESS_TOKEN="$NEW_ACCESS_TOKEN"

  if [[ -n "${NEW_REFRESH_TOKEN:-}" ]]; then
    REFRESH_TOKEN="$NEW_REFRESH_TOKEN"
    echo "Refresh token rotated; using new refresh token."
  fi

  echo "Obtained new access token."
  return 0
}

main() {
  echo "Starting OAuth access-token test flow..."
  echo

  if call_api "$ACCESS_TOKEN"; then
    echo "Success with current access token."
    exit 0
  fi

  status=$?
  if [[ "$status" -ne 10 ]]; then
    echo "API call failed, but not clearly due to token expiry."
    exit 1
  fi

  echo "Access token appears expired or invalid."
  echo

  refresh_access_token
  echo

  if call_api "$ACCESS_TOKEN"; then
    echo "Success with refreshed access token."
    exit 0
  fi

  echo "Retry with refreshed access token still failed."
  exit 1
}

main "$@"
