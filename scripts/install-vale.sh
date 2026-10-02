#!/usr/bin/env bash
# Download the vale release for this machine's architecture into a private
# directory, and print that directory so the caller can put it on PATH.
#
# One installer, two callers: the composite action's "Install vale" step and
# this repo's own prose unit-test job in ci.yml. The install used to be written
# twice — once in action.yml, once in ci.yml — and the second copy had already
# lost the arm64 branch, so the prose tests could not install vale on an aarch64
# runner. The architecture switch and the destination directory belong in one
# place, and here it is.
#
# The destination is deliberately a private directory under RUNNER_TEMP rather
# than /usr/local/bin: the latter needs root on some self-hosted runners, and
# nothing here has a reason to need it.
#
# Usage:
#   VALE_VERSION=3.20.0 scripts/install-vale.sh [DESTDIR]
#
#   VALE_VERSION  vale release to install. Required; each caller names its own,
#                 so neither silently follows the other when a pin moves.
#   RUNNER_TEMP   base for the destination directory. Default /tmp.
#   DESTDIR       install directory. Default "$RUNNER_TEMP/vale-bin".
#
# On stdout: the directory holding the vale binary, and nothing else, so the
# caller can capture it with a command substitution. Progress and errors go to
# stderr.
#
# Exit 0 installed, 1 the download or extract failed, 2 this machine's
# architecture has no release asset.
set -euo pipefail

: "${VALE_VERSION:?set VALE_VERSION to the vale release to install, e.g. VALE_VERSION=3.20.0}"

dest=${1:-"${RUNNER_TEMP:-/tmp}/vale-bin"}

arch=$(uname -m)
case "$arch" in
  x86_64) asset=64-bit ;;
  aarch64 | arm64) asset=arm64 ;;
  *)
    echo "::error::vale has no release asset for $arch"
    exit 2
    ;;
esac

url="https://github.com/errata-ai/vale/releases/download/v${VALE_VERSION}/vale_${VALE_VERSION}_Linux_${asset}.tar.gz"

mkdir -p "$dest"
curl -fsSL "$url" | tar -xz -C "$dest" vale
chmod +x "$dest/vale"

# The install is only worth reporting once the binary answers. A download that
# produced an unusable file would otherwise sit in PATH until some later step
# failed for an unrelated-looking reason.
"$dest/vale" --version >&2

echo "$dest"
