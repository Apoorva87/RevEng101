#!/usr/bin/env bash
set -euo pipefail

# macOS Keychain + OpenAI API test
# Usage:
#   ./openai_keychain.sh save    # prompts for key, stores in Keychain
#   ./openai_keychain.sh test    # reads key from Keychain and makes API call
#   ./openai_keychain.sh delete  # removes key from Keychain

SERVICE_NAME="openai-api-key"
ACCOUNT_NAME="${USER}"

save_key() {
  echo "Paste your OpenAI API key, then press Enter:"
  read -r API_KEY
  if [[ -z "${API_KEY}" ]]; then
    echo "No key entered."
    exit 1
  fi

  # Delete existing item if present
  security delete-generic-password \
    -s "${SERVICE_NAME}" \
    -a "${ACCOUNT_NAME}" >/dev/null 2>&1 || true

  # Save new key
  security add-generic-password \
    -U \
    -a "${ACCOUNT_NAME}" \
    -s "${SERVICE_NAME}" \
    -w "${API_KEY}"

  echo "Saved OpenAI API key to macOS Keychain."
}

get_key() {
  security find-generic-password \
    -a "${ACCOUNT_NAME}" \
    -s "${SERVICE_NAME}" \
    -w
}

test_key() {
  API_KEY="$(get_key)" || {
    echo "Could not read key from Keychain."
    echo "Run: ./openai_keychain.sh save"
    exit 1
  }

  echo "Calling OpenAI Responses API..."
  RESPONSE="$(curl -sS https://api.openai.com/v1/responses \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${API_KEY}" \
    -d '{
      "model": "gpt-4.1",
      "input": "Reply with exactly: keychain test ok"
    }')"

  echo "Raw response:"
  echo "${RESPONSE}"

  if command -v jq >/dev/null 2>&1; then
    echo
    echo "Parsed output text:"
    echo "${RESPONSE}" | jq -r '
      .output[]? 
      | select(.type=="message") 
      | .content[]? 
      | select(.type=="output_text") 
      | .text
    '
  else
    echo
    echo "Install jq for cleaner parsing:"
    echo "  brew install jq"
  fi
}

delete_key() {
  security delete-generic-password \
    -s "${SERVICE_NAME}" \
    -a "${ACCOUNT_NAME}" >/dev/null 2>&1 && \
    echo "Deleted key from Keychain." || \
    echo "No matching Keychain item found."
}

case "${1:-}" in
  save)
    save_key
    ;;
  test)
    test_key
    ;;
  delete)
    delete_key
    ;;
  *)
    echo "Usage: $0 {save|test|delete}"
    exit 1
    ;;
esac
