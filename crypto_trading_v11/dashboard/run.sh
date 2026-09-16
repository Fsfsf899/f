#!/usr/bin/env bash
# تشغيل اللوحة. إيقافها لا يؤثر على محرك التداول إطلاقاً.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec python3 backend/app.py
