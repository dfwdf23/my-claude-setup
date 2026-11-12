#!/usr/bin/env bash
# 리눅스: ./start-linux.sh
cd "$(dirname "$0")" || exit 1
exec bash scripts/start-unix.sh
