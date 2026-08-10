#!/usr/bin/env bash
# SursumAI — one-command installer (Ollama-style), fully self-contained.
#
#   curl -fsSL https://github.com/Ga0512/SursumAI/raw/main/install.sh | bash
#
# No git required: downloads a tarball, resolves Python via uv, creates the venv,
# puts `sursumai` on the PATH, adds a desktop icon, and starts everything.
#
# Overrides (env):
#   SURSUMAI_REPO        github repo to fetch the tarball from (default Ga0512/sursumai)
#   SURSUMAI_VERSION     release tag (default latest)
#   SURSUMAI_TARBALL_URL full tarball URL (takes precedence over repo+version)
#   SURSUMAI_DIR         install dir (default $HOME/sursumai)
#   SURSUMAI_SRC_DIR     use a local source dir instead of downloading (dev/testing)
#
# Idempotent: re-running updates the code but never deletes sursumai.db / logs.

set -euo pipefail

SURSUMAI_REPO="${SURSUMAI_REPO:-Ga0512/SursumAI}"
SURSUMAI_VERSION="${SURSUMAI_VERSION:-main}"
SURSUMAI_TARBALL_URL="${SURSUMAI_TARBALL_URL:-https://github.com/$SURSUMAI_REPO/archive/refs/heads/$SURSUMAI_VERSION.tar.gz}"
SURSUMAI_DIR="${SURSUMAI_DIR:-$HOME/sursumai}"
SURSUMAI_SRC_DIR="${SURSUMAI_SRC_DIR:-}"
BIN_DIR="$HOME/.local/bin"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }

echo
bold "◆ SursumAI — instalador"
echo

# --- detect OS ---------------------------------------------------------------
IS_MAC=0; IS_WSL=0
case "$(uname -s)" in
  Darwin) IS_MAC=1 ;;
  Linux)
    if grep -qi "microsoft" /proc/version 2>/dev/null; then IS_WSL=1; fi
    ;;
  *) fail "sistema operacional não suportado: $(uname -s)" ;;
esac

command -v curl >/dev/null 2>&1 || fail "curl não encontrado. Instale o curl primeiro."
command -v tar  >/dev/null 2>&1 || fail "tar não encontrado."

# --- código (tarball sem git) --------------------------------------------------
mkdir -p "$SURSUMAI_DIR"

if [ -n "$SURSUMAI_SRC_DIR" ]; then
  ok "Usando código local: $SURSUMAI_SRC_DIR"
  cp -a "$SURSUMAI_SRC_DIR"/. "$SURSUMAI_DIR"/
else
  echo "Baixando SursumAI $SURSUMAI_VERSION …"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "$SURSUMAI_TARBALL_URL" -o "$TMP/sursumai.tar.gz" \
    || fail "falha ao baixar $SURSUMAI_TARBALL_URL"
  mkdir -p "$TMP/src"
  tar -xzf "$TMP/sursumai.tar.gz" -C "$TMP/src"
  # tarballs do GitHub trazem um diretório-raiz (ex: sursumai-1.0) — normaliza
  INNER="$(find "$TMP/src" -maxdepth 2 -name start.sh -printf '%h\n' | head -1)"
  SOURCE="${INNER:-$TMP/src}"
  cp -a "$SOURCE"/. "$SURSUMAI_DIR"/
  ok "Código em $SURSUMAI_DIR"
fi

cd "$SURSUMAI_DIR"

# --- python: uv preferido -------------------------------------------------------
ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  if [ "$IS_WSL" -eq 1 ] || [ "$IS_MAC" -eq 1 ] || [ -n "$(command -v apt-get)" ]; then
    echo "Instalando uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
      || warn "não consegui instalar o uv (vou tentar python3)"
  fi
  command -v uv >/dev/null 2>&1
}

PY=""
if ensure_uv; then
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv disponível."
  if [ ! -x "$SURSUMAI_DIR/.venv/bin/python" ]; then
    echo "Criando ambiente (uv venv)…"
    uv venv --python 3.11 .venv >/dev/null 2>&1 || uv venv .venv >/dev/null \
      || fail "não consegui criar o ambiente virtual"
  fi
  PY="$SURSUMAI_DIR/.venv/bin/python"
  echo "Instalando dependências (uv pip)…"
  uv pip install --python "$PY" -q -r requirements.txt || \
    "$PY" -m pip install --quiet -r requirements.txt || \
    fail "não consegui instalar as dependências"
else
  PYBIN="$(command -v python3 || command -v python || true)"
  [ -n "$PYBIN" ] || fail "nenhum Python 3 encontrado. Instale Python 3.10+ ou uv."
  ok "Usando Python do sistema ($PYBIN)."
  if [ ! -x "$SURSUMAI_DIR/.venv/bin/python" ]; then
    "$PYBIN" -m venv .venv || fail "não consegui criar o ambiente virtual"
  fi
  PY="$SURSUMAI_DIR/.venv/bin/python"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt || \
    fail "não consegui instalar as dependências"
fi
ok "Ambiente pronto."

# --- CLI no PATH ----------------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$SURSUMAI_DIR/sursumai/bin/sursumai" "$BIN_DIR/sursumai"
chmod +x "$SURSUMAI_DIR/sursumai/bin/sursumai"

add_path() {
  local rc="$1" line='export PATH="$HOME/.local/bin:$PATH"'
  [ -f "$rc" ] || touch "$rc"
  grep -qF "$line" "$rc" || printf '\n%s\n' "$line" >> "$rc"
}
if [ "$IS_MAC" -eq 1 ]; then
  add_path "$HOME/.zprofile"
else
  [ -n "${BASH_VERSION:-}" ] && add_path "$HOME/.bashrc"
  [ -f "$HOME/.zshrc" ] && add_path "$HOME/.zshrc"
fi
export PATH="$BIN_DIR:$PATH"
ok "Comando 'sursumai' no PATH ($BIN_DIR)."

# --- ícone no desktop ------------------------------------------------------------
ICON_SRC="$SURSUMAI_DIR/assets/sursumai-logo.svg"

if [ "$IS_MAC" -eq 1 ]; then
  APP="$HOME/Applications/SursumAI.app"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp -f "$ICON_SRC" "$APP/Contents/Resources/icon.svg"
  cat > "$APP/Contents/MacOS/SursumAI" <<EOF
#!/bin/bash
exec "$BIN_DIR/sursumai" --ui
EOF
  chmod +x "$APP/Contents/MacOS/SursumAI"
  cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>SursumAI</string>
  <key>CFBundleDisplayName</key><string>SursumAI</string>
  <key>CFBundleIdentifier</key><string>ai.sursum.app</string>
  <key>CFBundleExecutable</key><string>SursumAI</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict>
</plist>
EOF
  ok "Atalho em ~/Applications/SursumAI.app"

elif [ "$IS_WSL" -eq 1 ]; then
  # atalho no Desktop do Windows
  WIN_PROFILE="$(cmd.exe /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r' | sed 's/ *$//')"
  if [ -n "$WIN_PROFILE" ]; then
    DESKTOP="$(wslpath -u "$WIN_PROFILE\\Desktop")" 2>/dev/null || DESKTOP=""
    if [ -n "$DESKTOP" ] && [ -d "$DESKTOP" ]; then
      cat > "$DESKTOP/sursumai.bat" <<EOF
@echo off
wsl -e bash -lc "$BIN_DIR/sursumai --ui"
EOF
      ok "Atalho no Desktop do Windows: sursumai.bat"
    fi
  fi
fi

# ícone do menu Linux (sempre, também no WSL)
if [ "$IS_MAC" -eq 0 ]; then
  ICON_DIR="$HOME/.local/share/icons"
  APP_DIR="$HOME/.local/share/applications"
  mkdir -p "$ICON_DIR" "$APP_DIR"
  cp -f "$ICON_SRC" "$ICON_DIR/sursumai.svg"
  cat > "$APP_DIR/sursumai.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SursumAI
Comment=Seu modelo. Sua máquina. Sua URL.
Exec=$BIN_DIR/sursumai --ui
Icon=$ICON_DIR/sursumai.svg
Terminal=false
Categories=Development;Science;
EOF
  chmod +x "$APP_DIR/sursumai.desktop"
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
  ok "Ícone no menu de aplicativos."
fi

# --- subir -----------------------------------------------------------------------
echo
bold "Subindo o SursumAI…"
cd "$SURSUMAI_DIR"
if command -v sursumai >/dev/null 2>&1; then
  sursumai --ui
else
  bash "$SURSUMAI_DIR/start.sh"
fi

echo
ok "Instalação concluída. Próximos passos:"
echo "  • Terminal:  sursumai status"
echo "  • Ícone:     SursumAI no menu de aplicativos"
