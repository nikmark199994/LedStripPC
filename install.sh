#!/usr/bin/env bash
# Установка «Ленты» на Linux Mint: окружение, зависимости, ярлык и автозапуск.
set -e
D="$(cd "$(dirname "$0")" && pwd)"
sudo apt install -y python3-venv libxcb-cursor0 pulseaudio-utils
command -v dbus-monitor >/dev/null || sudo apt install -y dbus-bin
python3 -m venv "$D/venv"
"$D/venv/bin/pip" install --upgrade pip PySide6 bleak numpy
mkdir -p ~/.local/share/applications ~/.config/autostart
cat > ~/.local/share/applications/ledstrip.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Лента
Comment=Управление RGB-лентой
Exec=$D/venv/bin/python $D/ledstrip.py
Terminal=false
Categories=Utility;
DESK
sed "s|ledstrip.py|ledstrip.py --tray|" ~/.local/share/applications/ledstrip.desktop > ~/.config/autostart/ledstrip.desktop
echo "Готово. Запуск: $D/venv/bin/python $D/ledstrip.py (или ярлык «Лента» в меню)."
