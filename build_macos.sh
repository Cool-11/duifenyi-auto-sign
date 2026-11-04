#!/bin/zsh
set -euo pipefail

cd "${0:A:h}"

if [[ ! -f assets/icon.icns ]]; then
  iconset="$(mktemp -d)/duifenyi.iconset"
  mkdir -p "$iconset"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" assets/icon_1024.png --out "$iconset/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" assets/icon_1024.png --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o assets/icon.icns
fi

venv_dir=".venv-macos"
if [[ ! -x "$venv_dir/bin/python" ]]; then
  python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install -r requirements-macos.txt
"$venv_dir/bin/python" -m PyInstaller build.spec --noconfirm --clean
echo "构建完成：dist/对分易自动签到.app"
