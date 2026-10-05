#!/usr/bin/env bash
# Builds "Agent Office.app" and installs it into /Applications.
#
# The bundle is self-contained: it carries the backend, the built UI, its
# Python packages and its own Python interpreter, so it keeps working if this
# checkout or the Python used to build it goes away. On the Mac it still needs
# QEMU (brew install qemu), and for local models Ollama.
#
# Usage: scripts/build-app.sh            build and install
#        INSTALL_DIR=~/Applications scripts/build-app.sh
#        INSTALL_DIR= scripts/build-app.sh   build only (dist/Agent Office.app)
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="Agent Office"
EXECUTABLE="agent-office"
BUNDLE_ID="local.agent-office.app"
PYTHON_VERSION="3.12"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
BUILD_DIR="dist"
APP="$BUILD_DIR/$APP_NAME.app"
RES="$APP/Contents/Resources"
INSTALL_DIR="${INSTALL_DIR-/Applications}"

die() { echo "error: $*" >&2; exit 1; }
command -v uv >/dev/null || die "uv not found (https://docs.astral.sh/uv/)"
command -v npm >/dev/null || die "npm not found (install Node.js)"

echo "==> Building the UI"
(cd frontend && { [ -d node_modules ] || npm install; } && npm run build >/dev/null 2>&1) || die "UI build failed"

echo "==> Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/app/frontend" "$RES/app/scripts"
rsync -a --exclude '__pycache__' backend guest "$RES/app/"
cp scripts/create-base-image.sh "$RES/app/scripts/"
cp -R frontend/dist "$RES/app/frontend/dist"

echo "==> Bundling Python $PYTHON_VERSION"
# A standalone (relocatable) interpreter managed by uv, copied whole into the bundle.
uv python install "$PYTHON_VERSION" >/dev/null 2>&1 || true
PYTHON_BIN="$(uv python find --no-project --python-preference only-managed "$PYTHON_VERSION")" \
  || die "no uv-managed Python $PYTHON_VERSION available"
# The installation itself, even if uv handed back an interpreter inside a virtualenv.
PYTHON_ROOT="$("$PYTHON_BIN" -c 'import sys; print(sys.base_prefix)')"
# uv keeps a version-named symlink to the real installation: copy the real one.
PYTHON_ROOT="$(cd "$PYTHON_ROOT" && pwd -P)"
[ -x "$PYTHON_ROOT/bin/python$PYTHON_VERSION" ] && [ ! -e "$PYTHON_ROOT/pyvenv.cfg" ] \
  || die "$PYTHON_ROOT is not a standalone Python installation"
case "$PYTHON_ROOT" in
  *Cellar*|*Frameworks*) die "$PYTHON_ROOT is a system Python and cannot be relocated into the app" ;;
esac
cp -R "$PYTHON_ROOT" "$RES/python"
[ -d "$RES/python/bin" ] && [ ! -L "$RES/python" ] || die "Python was not copied into the bundle"
PYTHON="$RES/python/bin/python$PYTHON_VERSION"
"$PYTHON" -c 'import sys' || die "the bundled Python does not run"

echo "==> Installing Python packages into the bundle"
REQUIREMENTS="$(mktemp)"
ICON_WORK="$(mktemp -d)"
trap 'rm -rf "$REQUIREMENTS" "$ICON_WORK"' EXIT
uv export --frozen --no-dev --group app --no-emit-project --no-hashes >"$REQUIREMENTS" 2>/dev/null
uv pip install --quiet --python "$PYTHON" --target "$RES/site-packages" -r "$REQUIREMENTS"

echo "==> Icon"
PYTHONPATH="$RES/site-packages" "$PYTHON" scripts/make_icon.py "$ICON_WORK/icon.png"
mkdir "$ICON_WORK/AppIcon.iconset"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$ICON_WORK/icon.png" --out "$ICON_WORK/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  sips -z "$((size * 2))" "$((size * 2))" "$ICON_WORK/icon.png" --out "$ICON_WORK/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICON_WORK/AppIcon.iconset" -o "$RES/AppIcon.icns"

cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Agent Office connects to model servers on your network that you add in Settings.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

cat >"$APP/Contents/MacOS/$EXECUTABLE" <<'LAUNCHER'
#!/bin/sh
# Starts the bundled backend and its window. Output goes to ~/Library/Logs/agent-office.log.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Logs/agent-office.log"
PYTHON="$RES/python/bin/python3.12"
MAX_LOG_BYTES=5000000
EXIT_ALREADY_RUNNING=3

alert() {
  /usr/bin/osascript -e "display alert \"Agent Office\" message \"$1\" as critical" >/dev/null 2>&1
}

mkdir -p "$HOME/Library/Logs"
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt "$MAX_LOG_BYTES" ]; then
  mv -f "$LOG" "$LOG.1"
fi

export PYTHONPATH="$RES/site-packages:$RES/app"
export PYTHONDONTWRITEBYTECODE=1
export AGENT_OFFICE_ICON="$RES/AppIcon.icns"
export AGENT_OFFICE_UI_DIR="$RES/app/frontend/dist"
cd "$RES/app" || exit 1

"$PYTHON" -m backend.app >>"$LOG" 2>&1
status=$?
if [ "$status" -eq "$EXIT_ALREADY_RUNNING" ]; then
  alert "Agent Office is already running from another place (for example a development server). Quit that first."
  exit "$status"
fi
# Whatever ended the app (window closed, Cmd-Q, crash), leave no agent computer
# running unseen, unless the user chose to keep them running.
"$PYTHON" -m backend.shutdown >>"$LOG" 2>&1
if [ "$status" -ne 0 ]; then
  alert "Agent Office stopped unexpectedly. Details are in ~/Library/Logs/agent-office.log."
fi
exit "$status"
LAUNCHER
chmod +x "$APP/Contents/MacOS/$EXECUTABLE"

# Nothing in the bundle may point back at the machine it was built on.
if /usr/bin/grep -rIl --include='*.cfg' --include='*.pth' "$HOME" "$RES" 2>/dev/null | /usr/bin/grep -q .; then
  die "the bundle contains paths into $HOME"
fi

# Nor may anything in it be a link to somewhere outside the bundle.
if find "$APP" -type l -exec readlink {} + | /usr/bin/grep -q '^/'; then
  die "the bundle contains links to files outside itself"
fi

# Ad-hoc signature: enough for an app built and run on the same Mac.
codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "    (codesign skipped)"

echo "==> Built $APP ($(du -sh "$APP" | cut -f1))"
if [ -n "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/$APP_NAME.app"
  ditto "$APP" "$INSTALL_DIR/$APP_NAME.app"
  # Two copies with one bundle id would leave macOS guessing which one to open.
  rm -rf "$APP"
  echo "==> Installed $INSTALL_DIR/$APP_NAME.app"
fi
