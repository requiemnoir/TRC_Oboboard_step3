#!/usr/bin/env bash
# Install the voice toolchain for the EV-Q Onboard Manager:
#   - PipeWire + Bluez (audio + Bluetooth stack)
#   - whisper.cpp (offline STT) + Italian model
#   - piper-tts (offline TTS) + Italian voice model
#
# Idempotent: safe to re-run. All Italian-language assets are stored under
# <project>/databases/voice/ so the system stays operational without internet.
#
# Usage:
#   sudo ./install_voice.sh                  # full install
#   sudo ./install_voice.sh --skip-system    # only fetch models, no apt
#   sudo ./install_voice.sh --whisper-size small   # smaller model (faster, less accurate)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVBM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$KVBM_DIR"

VOICE_DIR="$PROJECT_DIR/databases/voice"
WHISPER_DIR="/opt/whisper.cpp"
PIPER_DIR="/opt/piper"

WHISPER_SIZE="medium"     # small | medium
SKIP_SYSTEM=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-system) SKIP_SYSTEM=1; shift;;
        --whisper-size) WHISPER_SIZE="${2:-medium}"; shift 2;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0;;
        *) echo "[voice-install] unknown arg: $1" >&2; exit 2;;
    esac
done

log() { printf '[voice-install] %s\n' "$*"; }
need_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        log "Re-running with sudo..."
        exec sudo -E "$0" "$@"
    fi
}

if [[ $SKIP_SYSTEM -eq 0 ]]; then need_root "$@"; fi

mkdir -p "$VOICE_DIR"

# ────────────────────── 1) System packages ──────────────────────
if [[ $SKIP_SYSTEM -eq 0 ]]; then
    log "Installing system audio + bluetooth stack (apt)…"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends \
        bluez bluez-tools \
        pipewire pipewire-pulse pipewire-audio-client-libraries \
        libspa-0.2-bluetooth wireplumber \
        pulseaudio-utils alsa-utils \
        ffmpeg sox \
        build-essential cmake git curl ca-certificates
    log "System packages OK."

    # Make sure the bluetooth service is enabled.
    systemctl enable --now bluetooth.service 2>/dev/null || true
    # PipeWire is a per-user service; we only ensure binaries are present.
fi

# ────────────────────── 2) whisper.cpp ──────────────────────
if [[ ! -x "$WHISPER_DIR/build/bin/whisper-cli" && ! -x "$WHISPER_DIR/main" ]]; then
    log "Building whisper.cpp at $WHISPER_DIR…"
    rm -rf "$WHISPER_DIR"
    git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
    pushd "$WHISPER_DIR" >/dev/null
    if [[ -f CMakeLists.txt ]]; then
        cmake -B build -DGGML_NATIVE=ON
        cmake --build build -j"$(nproc 2>/dev/null || echo 2)" --config Release
    else
        # very old layout
        make -j"$(nproc 2>/dev/null || echo 2)"
    fi
    popd >/dev/null
else
    log "whisper.cpp already built."
fi

WHISPER_BIN="$WHISPER_DIR/build/bin/whisper-cli"
[[ -x "$WHISPER_BIN" ]] || WHISPER_BIN="$WHISPER_DIR/main"

# Install a stable shim so the python wrapper finds it via PATH.
if [[ -x "$WHISPER_BIN" ]]; then
    install -m 0755 "$WHISPER_BIN" /usr/local/bin/whisper-cli
fi

# Italian whisper model (multilingual; whisper-medium covers IT well).
# HF naming is inconsistent across sizes:
#   - small ships only q5_1
#   - medium ships only q5_0
# Try the standard quant variants first, then fall back to the unquantized
# .bin if neither is available.
WHISPER_MODEL_PATH=""
if compgen -G "$VOICE_DIR/ggml-${WHISPER_SIZE}-q5_*.bin" > /dev/null; then
    WHISPER_MODEL_PATH="$(ls -1 "$VOICE_DIR"/ggml-${WHISPER_SIZE}-q5_*.bin | head -1)"
    log "whisper model already present: $(basename "$WHISPER_MODEL_PATH")"
else
    for q in q5_0 q5_1; do
        cand="$VOICE_DIR/ggml-${WHISPER_SIZE}-${q}.bin"
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_SIZE}-${q}.bin"
        log "Trying $url…"
        if curl -fL --retry 3 --retry-delay 5 -o "$cand" "$url"; then
            WHISPER_MODEL_PATH="$cand"
            break
        else
            rm -f "$cand"
        fi
    done
    if [[ -z "$WHISPER_MODEL_PATH" ]]; then
        # Fallback: full (non-quantized) model
        cand="$VOICE_DIR/ggml-${WHISPER_SIZE}.bin"
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_SIZE}.bin"
        log "Falling back to non-quantized: $url"
        if curl -fL --retry 3 --retry-delay 5 -o "$cand" "$url"; then
            WHISPER_MODEL_PATH="$cand"
        else
            log "WARN: whisper model download failed; STT will be unavailable."
            rm -f "$cand"
        fi
    fi
fi
WHISPER_MODEL_FILE="$(basename "${WHISPER_MODEL_PATH:-NONE}")"

# Symlink for easy discovery
if [[ -f "$WHISPER_MODEL_PATH" ]]; then
    ln -sf "$WHISPER_MODEL_PATH" "$VOICE_DIR/whisper-active.bin"
fi

# ────────────────────── 3) Piper TTS ──────────────────────
PIPER_BIN="/usr/local/bin/piper"
if [[ ! -x "$PIPER_BIN" ]]; then
    log "Installing Piper TTS at $PIPER_DIR…"
    mkdir -p "$PIPER_DIR"
    ARCH="$(uname -m)"
    case "$ARCH" in
        aarch64|arm64)  PIPER_ASSET="piper_linux_aarch64.tar.gz";;
        x86_64|amd64)   PIPER_ASSET="piper_linux_x86_64.tar.gz";;
        armv7l|armhf)   PIPER_ASSET="piper_linux_armv7l.tar.gz";;
        *) log "WARN: unsupported arch '$ARCH' for prebuilt piper; skipping."; PIPER_ASSET="";;
    esac
    if [[ -n "$PIPER_ASSET" ]]; then
        curl -fL --retry 3 --retry-delay 5 \
            -o "$PIPER_DIR/$PIPER_ASSET" \
            "https://github.com/rhasspy/piper/releases/latest/download/$PIPER_ASSET" \
            && tar -xzf "$PIPER_DIR/$PIPER_ASSET" -C "$PIPER_DIR" \
            && [[ -x "$PIPER_DIR/piper/piper" ]] \
            && install -m 0755 "$PIPER_DIR/piper/piper" "$PIPER_BIN" \
            || log "WARN: piper install failed; TTS will be unavailable."
        rm -f "$PIPER_DIR/$PIPER_ASSET"
    fi
else
    log "Piper already installed at $PIPER_BIN."
fi

# Italian voice model: it_IT-paola-medium (warmer, female) preferred.
VOICE_MODEL="it_IT-paola-medium"
VOICE_ONNX="$VOICE_DIR/${VOICE_MODEL}.onnx"
VOICE_CONF="$VOICE_DIR/${VOICE_MODEL}.onnx.json"
if [[ ! -f "$VOICE_ONNX" ]]; then
    log "Downloading piper voice: $VOICE_MODEL…"
    curl -fL --retry 3 --retry-delay 5 -o "$VOICE_ONNX" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/it/it_IT/paola/medium/${VOICE_MODEL}.onnx" \
        || { log "WARN: piper voice download failed."; rm -f "$VOICE_ONNX"; }
fi
if [[ ! -f "$VOICE_CONF" ]]; then
    curl -fL --retry 3 --retry-delay 5 -o "$VOICE_CONF" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/it/it_IT/paola/medium/${VOICE_MODEL}.onnx.json" \
        || { log "WARN: piper voice json download failed."; rm -f "$VOICE_CONF"; }
fi
if [[ -f "$VOICE_ONNX" ]]; then
    ln -sf "$VOICE_ONNX" "$VOICE_DIR/piper-active.onnx"
    [[ -f "$VOICE_CONF" ]] && ln -sf "$VOICE_CONF" "$VOICE_DIR/piper-active.onnx.json"
fi

# ────────────────────── 4) Sanity checks ──────────────────────
log "──── Voice toolchain summary ────"
echo "whisper-cli:    $(command -v whisper-cli || echo MISSING)"
echo "whisper model:  ${WHISPER_MODEL_PATH} ($([[ -f $WHISPER_MODEL_PATH ]] && du -h "$WHISPER_MODEL_PATH" | cut -f1 || echo MISSING))"
echo "piper:          $(command -v piper || echo MISSING)"
echo "piper voice:    ${VOICE_ONNX} ($([[ -f $VOICE_ONNX ]] && du -h "$VOICE_ONNX" | cut -f1 || echo MISSING))"
echo "bluetoothctl:   $(command -v bluetoothctl || echo MISSING)"
echo "pactl:          $(command -v pactl || echo MISSING)"
echo "ffmpeg:         $(command -v ffmpeg || echo MISSING)"
log "Done. Toggle voice in the UI under Settings → Voice & Audio."
