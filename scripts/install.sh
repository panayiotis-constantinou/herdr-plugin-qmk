#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$root/herdr-plugin.toml")
repo="panayiotis-constantinou/herdr-plugin-qmk"
mkdir -p "$root/bin"

if command -v nix >/dev/null 2>&1; then
  # NixOS has no global libasound, so prebuilt binaries fail; build natively.
  out=$(mktemp -d)
  trap 'rm -rf "$out"' EXIT HUP INT TERM
  nix build "$root#" --out-link "$out/result"
  cp "$out/result/bin/qmk-herdr" "$root/bin/qmk-herdr"
  exit 0
fi

case "$(uname -s):$(uname -m)" in
Linux:x86_64) target=x86_64-unknown-linux-gnu ;;
Darwin:x86_64) target=x86_64-apple-darwin ;;
Darwin:arm64) target=aarch64-apple-darwin ;;
*)
  echo "qmk-herdr: unsupported platform $(uname -s)/$(uname -m)" >&2
  exit 1
  ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
archive="qmk-herdr-$target.tar.gz"
url="https://github.com/$repo/releases/download/v$version/$archive"

if ! command -v gh >/dev/null 2>&1 ||
  ! gh release download "v$version" --repo "$repo" --pattern "$archive" --dir "$tmp"; then
  curl -fL "$url" -o "$tmp/$archive"
fi

tar -xzf "$tmp/$archive" -C "$root/bin"
chmod +x "$root/bin/qmk-herdr"
