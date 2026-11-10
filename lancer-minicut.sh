#!/usr/bin/env bash
# Lanceur MiniCut Studio — démarre le serveur et ouvre le navigateur.
cd "$(dirname "$0")" || exit 1
echo "Démarrage de MiniCut Studio…"
exec python3 server.py
