#!/usr/bin/env bash
# Wraps yt-dlp with the same format/merge options as documentation files/help.md.
# Requires: yt-dlp on PATH (e.g. brew install yt-dlp, or pipx install yt-dlp).
set -euo pipefail

usage() {
  echo "Usage: $0 <youtube-url> [full | SECTION]" >&2
  echo "" >&2
  echo "  Default SECTION: first 5 minutes (*00:00:00-00:05:00), merged to mp4." >&2
  echo "  full            : entire video (no --download-sections)." >&2
  echo "  SECTION         : yt-dlp --download-sections value, e.g. '*00:00:00-00:30:00'" >&2
  echo "" >&2
  echo "Examples:" >&2
  echo "  $0 'https://www.youtube.com/watch?v=VIDEO_ID'" >&2
  echo "  $0 'https://youtu.be/VIDEO_ID' full" >&2
  exit 1
}

[[ "${1:-}" ]] || usage

url="$1"
second="${2:-}"

args=(
  yt-dlp
  -f "137+140/136+140/best"
  --merge-output-format mp4
)

if [[ "$second" == "full" || "$second" == "-" ]]; then
  :
else
  section="${second:-*00:00:00-00:05:00}"
  args+=(--download-sections "$section")
fi

args+=("$url")
exec "${args[@]}"
