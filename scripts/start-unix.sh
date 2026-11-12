#!/usr/bin/env bash
# Called by start-mac.command / start-linux.sh. Uses the system Node.js if it is 20+,
# otherwise downloads a private copy into .runtime/ (no admin rights needed).
set -euo pipefail
cd "$(dirname "$0")/.."

NODE_VERSION="v24.22.3"

node_major() {
  local v
  v="$("$1" -v 2>/dev/null || true)"
  v="${v#v}"
  echo "${v%%.*}"
}

NODE=""
if command -v node >/dev/null 2>&1; then
  m="$(node_major node)"
  if [[ "$m" =~ ^[0-9]+$ ]] && (( m >= 20 )); then NODE="$(command -v node)"; fi
fi
if [[ -z "$NODE" && -x ".runtime/node/bin/node" ]]; then NODE="$PWD/.runtime/node/bin/node"; fi

if [[ -z "$NODE" ]]; then
  case "$(uname -s)" in
    Darwin) plat="darwin" ;;
    Linux) plat="linux" ;;
    *) echo "지원하지 않는 운영체제입니다: $(uname -s)"; exit 1 ;;
  esac
  case "$(uname -m)" in
    x86_64 | amd64) arch="x64" ;;
    arm64 | aarch64) arch="arm64" ;;
    *) echo "지원하지 않는 CPU 입니다: $(uname -m)"; exit 1 ;;
  esac
  name="node-${NODE_VERSION}-${plat}-${arch}"
  echo "▶ Node.js 를 내려받는 중 (처음 한 번만)…"
  mkdir -p .runtime
  url="https://nodejs.org/dist/${NODE_VERSION}/${name}.tar.gz"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --progress-bar "$url" -o ".runtime/node.tar.gz"
  elif command -v wget >/dev/null 2>&1; then
    wget -O ".runtime/node.tar.gz" "$url"
  else
    echo "✗ 내려받기 도구(curl 또는 wget)가 필요합니다. 예: sudo apt install curl"
    exit 1
  fi
  rm -rf ".runtime/${name}" ".runtime/node"
  tar -xzf ".runtime/node.tar.gz" -C .runtime
  mv ".runtime/${name}" ".runtime/node"
  rm -f ".runtime/node.tar.gz"
  NODE="$PWD/.runtime/node/bin/node"
fi

export PATH="$(dirname "$NODE"):$PATH"
exec "$NODE" scripts/launch.js
