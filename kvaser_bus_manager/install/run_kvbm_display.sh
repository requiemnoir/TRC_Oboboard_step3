#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPLAY_URL="${KVBM_DISPLAY_URL:-http://127.0.0.1:5000/display}"
POLL_S="${KVBM_DISPLAY_POLL_S:-5}"
PROFILE_DIR="${HOME}/.config/kvbm-display-browser"
LOG_FILE="${HOME}/.cache/kvbm-display.log"

mkdir -p "${PROFILE_DIR}" "$(dirname "${LOG_FILE}")"

browser_cmd() {
  if command -v chromium-browser >/dev/null 2>&1; then
    printf '%s\n' "$(command -v chromium-browser)"
    return 0
  fi
  if command -v chromium >/dev/null 2>&1; then
    printf '%s\n' "$(command -v chromium)"
    return 0
  fi
  if command -v google-chrome >/dev/null 2>&1; then
    printf '%s\n' "$(command -v google-chrome)"
    return 0
  fi
  return 1
}

detect_display() {
  local line name mode width height

  if command -v xrandr >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
    while IFS= read -r line; do
      [[ "${line}" == *" connected"* ]] || continue
      name="$(awk '{print $1}' <<<"${line}")"
      mode="$(grep -oE '[0-9]+x[0-9]+\+[0-9]+\+[0-9]+' <<<"${line}" | head -n 1 || true)"
      if [[ -z "${mode}" ]]; then
        mode="$(xrandr --query 2>/dev/null | awk -v out="${name}" '
          $1 == out && $2 == "connected" { active = 1; next }
          active && /^[[:space:]]+[0-9]+x[0-9]+/ { gsub(/^[[:space:]]+/, "", $1); print $1; exit }
        ' || true)"
      fi
      [[ -n "${mode}" ]] || continue
      width="${mode%%x*}"
      height="${mode#*x}"
      height="${height%%+*}"
      printf '%s %s %s\n' "${name}" "${width}" "${height}"
      return 0
    done < <(xrandr --query 2>/dev/null || true)
  fi

  if command -v wlr-randr >/dev/null 2>&1 && [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    wlr-randr 2>/dev/null | awk '
      /^[A-Za-z0-9_.:-]+ / { output = $1 }
      /current/ {
        match($0, /([0-9]+)x([0-9]+)/, dims)
        if (dims[1] != "" && dims[2] != "") {
          print output, dims[1], dims[2]
          exit
        }
      }
    '
    return ${PIPESTATUS[0]}
  fi

  return 1
}

display_mode() {
  local width="$1"
  local height="$2"
  local short_side="${width}"
  local long_side="${height}"

  if (( height < width )); then
    short_side="${height}"
    long_side="${width}"
  fi

  if (( short_side <= 900 && long_side <= 1600 )); then
    printf '%s\n' "kiosk"
  else
    printf '%s\n' "window"
  fi
}

stop_browser() {
  pkill -f "${PROFILE_DIR}" >/dev/null 2>&1 || true
}

launch_browser() {
  local mode="$1"
  local browser
  browser="$(browser_cmd)" || return 1

  stop_browser
  sleep 1

  local common_args=(
    "--user-data-dir=${PROFILE_DIR}"
    "--no-first-run"
    "--no-default-browser-check"
    "--disable-session-crashed-bubble"
    "--disable-infobars"
    "--check-for-update-interval=31536000"
    "--ozone-platform=wayland"
    "--enable-wayland-ime"
    "--wayland-text-input-version=3"
  )

  if [[ "${mode}" == "kiosk" ]]; then
    nohup "${browser}" "${common_args[@]}" --kiosk --start-fullscreen "${DISPLAY_URL}" >>"${LOG_FILE}" 2>&1 &
  else
    nohup "${browser}" "${common_args[@]}" --app="${DISPLAY_URL}" --start-maximized >>"${LOG_FILE}" 2>&1 &
  fi
}

current_signature=""

while true; do
  if display_info="$(detect_display)"; then
    read -r output width height <<<"${display_info}"
    mode="$(display_mode "${width}" "${height}")"
    wanted_signature="${output}:${width}x${height}:${mode}"
    if [[ "${wanted_signature}" != "${current_signature}" ]] || ! pgrep -f "${PROFILE_DIR}" >/dev/null 2>&1; then
      launch_browser "${mode}" || true
      current_signature="${wanted_signature}"
    fi
  else
    if [[ -n "${current_signature}" ]]; then
      stop_browser
      current_signature=""
    fi
  fi
  sleep "${POLL_S}"
done