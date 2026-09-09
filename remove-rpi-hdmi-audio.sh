#!/bin/bash

# 2026-09-09
# Ernst Lanser <ernst.lanser@wobbo.org>
# HDMI Audio Encoder v6 - complete remover

set -euo pipefail
trap 'printf "\n    Removal failed\n\n"' ERR

clear
printf "\n"
printf "  \033[1mHDMI Audio Encoder - Remover\033[0m\n\n"

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
PROJECT_STATE_DIR="/var/lib/hdmi-audio-encoder"

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

# Remove the per-user dca.conf include only when this installer added it.
if [ -f "$PROJECT_STATE_DIR/asoundrc-include-added" ]; then
    ASOUNDRC="$TARGET_HOME/.asoundrc"

    if [ -f "$ASOUNDRC" ]; then
        sed -i \
            '\|^[[:space:]]*<confdir:pcm/dca.conf>[[:space:]]*$|d' \
            "$ASOUNDRC"

        if ! grep -q '[^[:space:]]' "$ASOUNDRC"; then
            rm -f "$ASOUNDRC"
        else
            chown "$TARGET_USER:$TARGET_USER" "$ASOUNDRC"
        fi
    fi
fi

# Remove dcaenc only when it was installed by our installer. If dcaenc already
# existed before installation, leave its libraries and plugin alone.
if [ -f "$PROJECT_STATE_DIR/dcaenc-installed-by-hdmi-audio-encoder" ]; then
    DCA_LIBDIR=""

    if [ -s "$PROJECT_STATE_DIR/dca-libdir" ]; then
        DCA_LIBDIR="$(cat "$PROJECT_STATE_DIR/dca-libdir")"
    elif command -v pkg-config >/dev/null 2>&1; then
        DCA_LIBDIR="$(pkg-config --variable=libdir alsa 2>/dev/null || true)"
    fi

    printf "    Removing DTS/dcaenc files installed by this project...\n\n"

    rm -f /usr/bin/dcaenc
    rm -f /usr/include/dcaenc.h
    rm -f /usr/share/alsa/pcm/dca.conf

    if [ -n "$DCA_LIBDIR" ]; then
        rm -f "$DCA_LIBDIR/libdcaenc.so"
        rm -f "$DCA_LIBDIR/libdcaenc.so.0"
        rm -f "$DCA_LIBDIR/libdcaenc.so.0.0.0"
        rm -f "$DCA_LIBDIR/libdcaenc.la"
        rm -f "$DCA_LIBDIR/pkgconfig/dcaenc.pc"
        rm -f "$DCA_LIBDIR/alsa-lib/libasound_module_pcm_dca.so"
        rm -f "$DCA_LIBDIR/alsa-lib/libasound_module_pcm_dca.la"
    fi

    ldconfig
fi

# If a pre-existing dca.conf was patched by the installer, restore that exact
# original file after removing or leaving the dcaenc installation itself.
if [ -f "$PROJECT_STATE_DIR/dca.conf.before-hdmi-audio-encoder" ]; then
    mkdir -p /usr/share/alsa/pcm
    cp -a \
        "$PROJECT_STATE_DIR/dca.conf.before-hdmi-audio-encoder" \
        /usr/share/alsa/pcm/dca.conf
fi

rm -rf "$PROJECT_STATE_DIR"

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
printf "  ╔════════════ \033[1mHDMI Audio Encoder - Removed\033[0m ════════════╗\n"
printf "  ║  Application and custom audio files removed.       ║\n"
printf "  ║  Normal GNOME HDMI 0 stereo has been restored.     ║\n"
printf "  ╚═════════════════════════════════════════════════════╝\n\n"
