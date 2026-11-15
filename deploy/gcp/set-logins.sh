#!/bin/bash
# Asks for the server's logins and stores them in Google Secret Manager, where the VM's startup
# script reads them. Leave an answer empty to keep what is stored. Passwords are not shown while
# you type, and nothing is written to disk or to your shell history.
#
#   deploy/gcp/set-logins.sh PROJECT [ZONE] [VM]
set -euo pipefail
PROJECT=${1:?usage: $0 PROJECT [ZONE] [VM]}
ZONE=${2:-us-central1-a}
VM=${3:-matcha-anki}

store() {  # name, value
  if gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1; then
    printf %s "$2" | gcloud secrets versions add "$1" --data-file=- --project "$PROJECT" >/dev/null
  else
    printf %s "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic --project "$PROJECT" >/dev/null
  fi
  echo "  saved $1"
}

ask() {  # name, prompt, hidden
  local value
  if [ "$3" = hidden ]; then
    read -rsp "$2: " value
    echo
  else
    read -rp "$2: " value
  fi
  if [ -n "$value" ]; then store "$1" "$value"; fi
}

echo "AnkiWeb (empty to skip)"
ask ankiweb-username "  AnkiWeb email" shown
ask ankiweb-password "  AnkiWeb password" hidden
echo "Migaku (empty to skip)"
ask migaku-email "  Migaku email" shown
ask migaku-password "  Migaku password" hidden

read -rp "Time zone, e.g. Europe/Berlin (empty to skip): " tz
if [ -n "$tz" ]; then
  gcloud compute instances add-metadata "$VM" --zone "$ZONE" --project "$PROJECT" --metadata matcha-tz="$tz" >/dev/null
  echo "  saved time zone $tz"
fi

read -rp "Restart the server now so it uses them? [y/N] " restart
if [ "$restart" = y ] || [ "$restart" = Y ]; then
  gcloud compute instances reset "$VM" --zone "$ZONE" --project "$PROJECT" --quiet >/dev/null
  echo "Restarting; the server answers again in about two minutes."
fi
