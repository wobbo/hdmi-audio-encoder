#!/bin/bash

# 2026-09-08
# Ernst Lanser <ernst.lanser@gmail.com>
# RPi HDMI Audio v5 - complete remover

set -euo pipefail
trap 'printf "\n    Removal failed\n\n"' ERR

clear
printf "\n"
printf "  \033[1mRPi HDMI Audio - Remover\033[0m\n\n"

if [ "$EUID" -ne 0 ]; then
    printf "  Run this remover with sudo:\n"
    printf "  sudo ./remove-rpi-hdmi-audio.sh\n\n"
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    printf "  Run this remover from your normal GNOME account with sudo.\n\n"
    exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

user_cmd() {
    runuser -u "$TARGET_USER" -- env \
        HOME="$TARGET_HOME" \
        XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
        "$@"
}

printf "    Removing application and launchers...\n\n"
rm -f /usr/local/bin/rpi-hdmi-audio
rm -f /usr/share/applications/rpi-hdmi-audio.desktop
rm -f /usr/share/applications/org.wobbo.RPiHDMIAudio.desktop
rm -f /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop
rm -f "$TARGET_HOME/.config/autostart/rpi-hdmi-audio.desktop"
rm -f "$TARGET_HOME/.config/autostart/rpi-hdmi-audio-restore.desktop"
rm -rf "$TARGET_HOME/.local/state/rpi-hdmi-audio"

printf "    Removing old experimental configuration if it still exists...\n\n"
rm -f "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi0.conf"
rm -f "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi1.conf"
rm -f "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-rpi-hdmi-ac3.conf"
rm -f "$TARGET_HOME/.config/pipewire/pipewire.conf.d/90-rpi-hdmi-audio-names.conf"

rmdir --ignore-fail-on-non-empty \
    "$TARGET_HOME/.config/alsa-card-profile/profile-sets" \
    "$TARGET_HOME/.config/alsa-card-profile" \
    "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d" \
    "$TARGET_HOME/.config/pipewire/pipewire.conf.d" \
    2>/dev/null || true

update-desktop-database /usr/share/applications >/dev/null 2>&1 || true

printf "    Restoring normal GNOME HDMI audio...\n\n"
if [ -S "/run/user/$TARGET_UID/bus" ]; then
    user_cmd systemctl --user restart pipewire pipewire-pulse wireplumber || true

    for _ in $(seq 1 30); do
        if user_cmd pactl info >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done

    user_cmd pactl set-card-profile \
        alsa_card.platform-107c701400.hdmi output:hdmi-stereo \
        >/dev/null 2>&1 || true
fi

printf "\n"
printf "  ╔════════════ \033[1mRPi HDMI Audio - Removed\033[0m ═════════════╗\n"
printf "  ║  Application and custom audio files removed.       ║\n"
printf "  ║  Normal GNOME HDMI 0 stereo has been restored.     ║\n"
printf "  ╚═════════════════════════════════════════════════════╝\n\n"
