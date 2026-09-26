#!/usr/bin/env bash
# HDMI Audio Encoder 16.4 herzien - Raspberry Pi 5 / 500+, Debian 13 desktop.
# Run as your desktop user: sudo ./install-rpi-hdmi-audio.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if (( EUID != 0 )) || [[ -z ${SUDO_USER:-} || ${SUDO_USER:-} == root ]]; then
    printf 'Start dit script vanuit je gewone desktopaccount met sudo.\n' >&2
    exit 1
fi

TARGET_USER=$SUDO_USER
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_GID=$(id -g "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -d $TARGET_HOME && $TARGET_HOME == /* ]] || { echo 'Gebruikersmap niet gevonden.' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
APP_SOURCE="$SCRIPT_DIR/rpi-hdmi-audio.py"
APP=/usr/local/bin/rpi-hdmi-audio
PROJECT_STATE=/var/lib/hdmi-audio-encoder
USER_META="$PROJECT_STATE/users/$TARGET_UID"
DCA_COMMIT=68ed0d6d370268f04c22cadc9c8fc54a479958ab
DCA_CONF=/usr/share/alsa/pcm/dca.conf
ALSA_INCLUDE=/etc/alsa/conf.d/60-dca-encoder.conf
BUILD_DIR=
trap '[[ -z $BUILD_DIR ]] || rm -rf -- "$BUILD_DIR"' EXIT

user_cmd() {
    runuser -u "$TARGET_USER" -- env \
        HOME="$TARGET_HOME" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" "$@"
}

session_live() {
    [[ -S /run/user/$TARGET_UID/bus ]] && user_cmd pactl info >/dev/null 2>&1
}

[[ -f $APP_SOURCE ]] || { printf 'Ontbreekt: %s\n' "$APP_SOURCE" >&2; exit 1; }
if [[ -e $ALSA_INCLUDE ]] && \
   [[ $(cat -- "$ALSA_INCLUDE") != '<confdir:pcm/dca.conf>' ]]; then
    printf 'Bestaand %s heeft andere inhoud; installatie afgebroken.\n' "$ALSA_INCLUDE" >&2
    exit 1
fi

printf 'HDMI Audio Encoder 16.4 herzien installeren...\n'
apt-get update -qq
apt-get install -y -qq \
    python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    libasound2-plugins libasound2-dev alsa-utils pulseaudio-utils \
    desktop-file-utils pipewire pipewire-bin wireplumber \
    git build-essential autoconf automake libtool pkg-config

python3 - "$APP_SOURCE" <<'PY'
import ast
import pathlib
import sys
tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
version = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'APP_VERSION'
                        for t in n.targets) and isinstance(n.value, ast.Constant)), None)
revision = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'DSP_CONFIG_REVISION'
                         for t in n.targets) and isinstance(n.value, ast.Constant)), None)
if version != '16.4' or revision != '16.4-herzien':
    raise SystemExit('rpi-hdmi-audio.py moet versie 16.4 herzien zijn.')
PY

install -d -m 700 "$PROJECT_STATE" "$PROJECT_STATE/users" "$USER_META"

# Only a fresh installation can tell us the user's actual pre-app audio state.
# Never replace this snapshot during an upgrade.
if [[ ! -e $APP && ! -f $USER_META/audio-before-install ]] && session_live; then
    default_sink=$(user_cmd pactl get-default-sink 2>/dev/null || true)
    if [[ $default_sink != rpi_hdmi_audio_* ]]; then
        card_profiles=$(user_cmd pactl list cards 2>/dev/null | awk '
            /^[[:space:]]*Name: / { name=$2 }
            /^[[:space:]]*Active Profile: / {
                if (name == "alsa_card.platform-107c701400.hdmi") print "card0=" $3
                if (name == "alsa_card.platform-107c706400.hdmi") print "card1=" $3
            }')
        {
            printf '%s\n' "$card_profiles"
            printf 'default=%s\n' "$default_sink"
        } > "$USER_META/audio-before-install"
        chmod 600 "$USER_META/audio-before-install"
        if user_cmd systemctl --user is-active --quiet filter-chain.service; then
            : > "$USER_META/filter-chain-was-active"
        fi
    fi
fi

ALSA_LIBDIR=$(pkg-config --variable=libdir alsa)
[[ $ALSA_LIBDIR == /* && $ALSA_LIBDIR != *..* ]] || { echo 'Ongeldig ALSA-libpad.' >&2; exit 1; }
DCA_PLUGIN="$ALSA_LIBDIR/alsa-lib/libasound_module_pcm_dca.so"
printf '%s\n' "$ALSA_LIBDIR" > "$PROJECT_STATE/dca-libdir"

if [[ ! -f $DCA_PLUGIN || ! -f $DCA_CONF ]]; then
    if [[ ! -f $PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder ]]; then
        # Building over a partially installed third-party dcaenc would erase
        # its files and make safe removal impossible.
        for existing in "$DCA_PLUGIN" "$DCA_CONF" /usr/bin/dcaenc \
            /usr/include/dcaenc.h "$ALSA_LIBDIR/libdcaenc.so" \
            "$ALSA_LIBDIR/libdcaenc.so.0" \
            "$ALSA_LIBDIR/libdcaenc.so.0.0.0" \
            "$ALSA_LIBDIR/libdcaenc.la" \
            "$ALSA_LIBDIR/alsa-lib/libasound_module_pcm_dca.la" \
            "$ALSA_LIBDIR/pkgconfig/dcaenc.pc"; do
            if [[ -e $existing || -L $existing ]]; then
                echo 'Onvolledige bestaande DTS-installatie: herstel die eerst handmatig.' >&2
                exit 1
            fi
        done
    fi
    printf 'DTS-plugin bouwen...\n'
    BUILD_DIR=$(mktemp -d /tmp/hdmi-audio-encoder-dcaenc.XXXXXXXX)
    git clone -q https://github.com/darealshinji/dcaenc.git "$BUILD_DIR/dcaenc"
    git -C "$BUILD_DIR/dcaenc" checkout -q "$DCA_COMMIT"
    pushd "$BUILD_DIR/dcaenc" >/dev/null
    if ! grep -q '^AC_CONFIG_AUX_DIR' configure.ac; then
        sed -i '/AC_CONFIG_HEADERS(\[config.h\])/a AC_CONFIG_AUX_DIR([.])' configure.ac
    fi
    autoreconf -f -i -v
    ./configure --prefix=/usr --libdir="$ALSA_LIBDIR"
    make -j"$(nproc)"
    # Track ownership before the first installed file, also on partial errors.
    : > "$PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder"
    make install
    popd >/dev/null
    ldconfig
fi
[[ -f $DCA_PLUGIN && -f $DCA_CONF ]] || { echo 'DTS-installatie onvolledig.' >&2; exit 1; }

# An existing, independently installed dcaenc config must return byte-for-byte.
if [[ ! -f $PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder && \
      ! -e $PROJECT_STATE/dca.conf.before-hdmi-audio-encoder ]]; then
    cp -a -- "$DCA_CONF" "$PROJECT_STATE/dca.conf.before-hdmi-audio-encoder"
fi
sed -i 's/@args \[ CARD DEV AES0 AES1 AES2 AES3 \]/@args [ CARD DEV AES0 AES1 AES2 AES3 IEC61937 ]/' "$DCA_CONF"
(( $(grep -cF '@args [ CARD DEV AES0 AES1 AES2 AES3 IEC61937 ]' "$DCA_CONF" || true) >= 2 )) || {
    echo 'DTS-configuratie heeft niet de verwachte IEC61937-argumenten.' >&2
    exit 1
}

# This project-specific path also covers an earlier manually applied system fix.
if [[ ! -e $ALSA_INCLUDE ]]; then
    printf '%s\n' '<confdir:pcm/dca.conf>' > "$ALSA_INCLUDE"
    chmod 644 "$ALSA_INCLUDE"
fi

# Old versions appended an ALSA include to only the installing user's file.
# Remove precisely that line when the old installer recorded ownership.
if [[ -f $PROJECT_STATE/asoundrc-include-added ]]; then
    python3 - "$TARGET_HOME/.asoundrc" <<'PY'
import os, pathlib, sys, tempfile
p = pathlib.Path(sys.argv[1])
if p.is_file():
    old = p.read_bytes()
    new = old.replace(b'\n<confdir:pcm/dca.conf>\n', b'', 1)
    if new == old and old == b'<confdir:pcm/dca.conf>\n':
        new = b''
    if new != old:
        if not new.strip():
            p.unlink()
        else:
            fd, name = tempfile.mkstemp(dir=p.parent)
            try:
                os.fchmod(fd, p.stat().st_mode & 0o777)
                os.fchown(fd, p.stat().st_uid, p.stat().st_gid)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(new)
                os.replace(name, p)
            finally:
                if os.path.exists(name): os.unlink(name)
PY
    rm -f "$PROJECT_STATE/asoundrc-include-added"
fi

# The app owns these exact old development filenames; never delete folders.
rm -f "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi0.conf" \
      "$TARGET_HOME/.config/alsa-card-profile/profile-sets/rpi-hdmi1.conf" \
      "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-rpi-hdmi-ac3.conf" \
      "$TARGET_HOME/.config/pipewire/pipewire.conf.d/90-rpi-hdmi-audio-names.conf" \
      "$TARGET_HOME/.config/autostart/rpi-hdmi-audio.desktop" \
      "$TARGET_HOME/.config/autostart/rpi-hdmi-audio-restore.desktop"
rm -f /usr/local/bin/add-channel-volume.py /usr/local/bin/add-channel-volume-v2.py

STATE_DIR="$TARGET_HOME/.local/state/rpi-hdmi-audio"
for dir in "$TARGET_HOME/.local" "$TARGET_HOME/.local/state" "$STATE_DIR"; do
    if [[ ! -d $dir ]]; then
        install -d -o "$TARGET_UID" -g "$TARGET_GID" -m 700 "$dir"
    fi
done
STATE_FILE="$STATE_DIR/last-choice"

# Keep volume and Frequency lines when updating an existing choice.
active_choice=
if session_live; then
    case $(user_cmd pactl get-default-sink 2>/dev/null || true) in
        rpi_hdmi_audio_hdmi0_stereo) active_choice=hdmi0-stereo ;;
        rpi_hdmi_audio_hdmi0_pcm51)  active_choice=hdmi0-pcm51 ;;
        rpi_hdmi_audio_hdmi0_ac3)    active_choice=hdmi0-ac3 ;;
        rpi_hdmi_audio_hdmi0_dts)    active_choice=hdmi0-dts ;;
        rpi_hdmi_audio_hdmi1_stereo) active_choice=hdmi1-stereo ;;
        rpi_hdmi_audio_hdmi1_pcm51)  active_choice=hdmi1-pcm51 ;;
        rpi_hdmi_audio_hdmi1_ac3)    active_choice=hdmi1-ac3 ;;
        rpi_hdmi_audio_hdmi1_dts)    active_choice=hdmi1-dts ;;
    esac
fi
if [[ -n $active_choice || ! -s $STATE_FILE ]]; then
    { printf '%s\n' "${active_choice:-hdmi0-stereo}"
      [[ ! -f $STATE_FILE ]] || tail -n +2 "$STATE_FILE"
    } > "$STATE_FILE.install-tmp"
    mv -f "$STATE_FILE.install-tmp" "$STATE_FILE"
fi
chown "$TARGET_UID:$TARGET_GID" "$STATE_FILE"
chmod 600 "$STATE_FILE"

# Terminate only this app's process and its specifically named silent keepalive.
python3 - "$TARGET_UID" <<'PY'
import os, pathlib, signal, sys, time
uid = int(sys.argv[1]); victims = []
for proc in pathlib.Path('/proc').iterdir():
    if not proc.name.isdigit() or int(proc.name) == os.getpid(): continue
    try:
        if proc.stat().st_uid != uid: continue
        a = [x.decode(errors='replace') for x in (proc/'cmdline').read_bytes().split(b'\0') if x]
        app = '/usr/local/bin/rpi-hdmi-audio' in a[:2]
        sound = (a and pathlib.Path(a[0]).name == 'paplay' and '--raw' in a and
                 any(x.startswith('--device=rpi_hdmi_audio_hdmi') for x in a))
        if app or sound: victims.append(int(proc.name))
    except (OSError, ValueError): pass
for pid in victims:
    try: os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError): pass
end = time.monotonic() + 1.0
alive = victims
while time.monotonic() < end:
    alive = []
    for pid in victims:
        try:
            p = pathlib.Path('/proc') / str(pid)
            a = (p/'cmdline').read_bytes().split(b'\0')
            if p.stat().st_uid == uid and (b'/usr/local/bin/rpi-hdmi-audio' in a[:2] or
                any(x.startswith(b'--device=rpi_hdmi_audio_hdmi') for x in a)):
                alive.append(pid)
        except (OSError, ValueError): pass
    if not alive: break
    time.sleep(.05)
for pid in alive:
    try: os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError): pass
PY

install -m 755 "$APP_SOURCE" "$APP"
user_cmd "$APP" --write-filter-config

cat > /usr/share/applications/rpi-hdmi-audio.desktop <<'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=HDMI Audio Encoder
Comment=Choose HDMI stereo, PCM 5.1, Dolby Digital or DTS with Frequency Balance
Exec=/usr/local/bin/rpi-hdmi-audio
Icon=audio-card
Terminal=false
Categories=Settings;AudioVideo;Audio;
Keywords=audio;hdmi;pcm;dolby;ac3;dts;surround;frequency;raspberry;
DESKTOP
chmod 644 /usr/share/applications/rpi-hdmi-audio.desktop

cat > /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=HDMI Audio Encoder Restore
Comment=Restore the last HDMI audio mode after login
Exec=/usr/local/bin/rpi-hdmi-audio --restore-monitor
Icon=audio-card
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
DESKTOP
chmod 644 /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop
update-desktop-database /usr/share/applications >/dev/null 2>&1 || true

if [[ -S /run/user/$TARGET_UID/bus ]]; then
    user_cmd systemctl --user restart pipewire pipewire-pulse wireplumber
    for _ in {1..40}; do
        user_cmd pactl info >/dev/null 2>&1 && break
        sleep 0.2
    done
    if session_live; then
        # This one process performs both restore and volume monitoring.
        runuser -u "$TARGET_USER" -- env \
            HOME="$TARGET_HOME" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
            nohup "$APP" --restore-monitor </dev/null >/dev/null 2>&1 &
    else
        echo 'Audio nog niet bereikbaar: meld je opnieuw aan om de opgeslagen uitvoer te herstellen.' >&2
    fi
fi

printf 'Gereed: versie 16.4 herzien. Open HDMI Audio Encoder via het applicatiemenu.\n'
