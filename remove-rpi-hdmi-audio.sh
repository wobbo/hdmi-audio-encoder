#!/usr/bin/env bash
# HDMI Audio Encoder 16.4 - remove this app and restore native desktop audio.
# Run from a logged-in desktop account: sudo ./remove-rpi-hdmi-audio.sh
set -euo pipefail

if (( EUID != 0 )) || [[ -z ${SUDO_USER:-} || ${SUDO_USER:-} == root ]]; then
    printf 'Start dit script vanuit je gewone desktopaccount met sudo.\n' >&2
    exit 1
fi

TARGET_USER=$SUDO_USER
TARGET_UID=$(id -u "$TARGET_USER")
PROJECT_STATE=/var/lib/hdmi-audio-encoder
APP=/usr/local/bin/rpi-hdmi-audio
DCA_CONF=/usr/share/alsa/pcm/dca.conf
ALSA_INCLUDE=/etc/alsa/conf.d/60-dca-encoder.conf
CARD0=alsa_card.platform-107c701400.hdmi
CARD1=alsa_card.platform-107c706400.hdmi
declare -a ISSUES=()

user_cmd() {
    local user=$1 uid=$2 home=$3
    shift 3
    runuser -u "$user" -- env \
        HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

session_live() {
    local user=$1 uid=$2 home=$3
    [[ -S /run/user/$uid/bus ]] && user_cmd "$user" "$uid" "$home" pactl info >/dev/null 2>&1
}

# Stop only this program's GUI, monitor and its raw silent Dolby/DTS stream.
# This works even when the installed binary is damaged or already missing.
stop_app_processes() {
    python3 - "$1" <<'PY'
import os, pathlib, signal, sys, time
uid = int(sys.argv[1]); victims = []
def matches(pid):
    p = pathlib.Path('/proc') / str(pid)
    try:
        if p.stat().st_uid != uid: return False
        a = [x.decode(errors='replace') for x in (p/'cmdline').read_bytes().split(b'\0') if x]
        app = '/usr/local/bin/rpi-hdmi-audio' in a[:2]
        sound = (a and pathlib.Path(a[0]).name == 'paplay' and '--raw' in a and
                 any(x.startswith('--device=rpi_hdmi_audio_hdmi') for x in a))
        return app or sound
    except (OSError, ValueError): return False
for p in pathlib.Path('/proc').iterdir():
    if p.name.isdigit() and int(p.name) != os.getpid() and matches(int(p.name)):
        victims.append(int(p.name))
for pid in victims:
    try: os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError): pass
end = time.monotonic() + 1.0
while time.monotonic() < end and any(matches(pid) for pid in victims):
    time.sleep(.05)
for pid in victims:
    if matches(pid):
        try: os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError): pass
PY
}

unload_our_modules() {
    local user=$1 uid=$2 home=$3 index module arguments
    while IFS=$'\t' read -r index module arguments; do
        [[ $index =~ ^[0-9]+$ ]] || continue
        [[ $module == module-alsa-sink || $module == module-null-sink ]] || continue
        case $arguments in
            *sink_name=rpi_hdmi_audio_*|*sink_name=rpi_test_ac3*|*sink_name=rpi_test_dts*|\
            *sink_name=hdmi0_ac3*|*sink_name=hdmi1_ac3*|\
            *sink_name=hdmi0_dts*|*sink_name=hdmi1_dts*|\
            *sink_name=hdmi0_stereo*|*sink_name=hdmi1_stereo*|\
            *sink_name=hdmi0_pcm51*|*sink_name=hdmi1_pcm51*)
                user_cmd "$user" "$uid" "$home" pactl unload-module "$index" >/dev/null 2>&1 || true ;;
        esac
    done < <(user_cmd "$user" "$uid" "$home" pactl list modules short 2>/dev/null || true)
}

# The old installer owned exactly one added line. Preserve any unrelated
# .asoundrc entries, mode and ownership.
remove_old_asoundrc_include() {
    python3 - "$1/.asoundrc" <<'PY'
import os, pathlib, sys, tempfile
p = pathlib.Path(sys.argv[1])
if not p.is_file(): raise SystemExit(0)
old = p.read_bytes()
new = old.replace(b'\n<confdir:pcm/dca.conf>\n', b'', 1)
if new == old and old == b'<confdir:pcm/dca.conf>\n': new = b''
if new == old: raise SystemExit(0)
if not new.strip():
    p.unlink()
else:
    st = p.stat()
    fd, name = tempfile.mkstemp(dir=p.parent)
    try:
        os.fchmod(fd, st.st_mode & 0o777)
        os.fchown(fd, st.st_uid, st.st_gid)
        with os.fdopen(fd, 'wb') as stream: stream.write(new)
        os.replace(name, p)
    finally:
        if os.path.exists(name): os.unlink(name)
PY
}

connected_port() {
    local n=$1 status
    for status in /sys/class/drm/card*-HDMI-A-"$n"/status; do
        [[ -f $status ]] && [[ $(cat -- "$status") == connected ]] && return 0
    done
    return 1
}

restore_profiles() {
    local user=$1 uid=$2 home=$3 baseline="$PROJECT_STATE/users/$2/audio-before-install"
    local old_card0= old_card1= key value
    if [[ -f $baseline ]]; then
        while IFS='=' read -r key value; do
            case $key in card0) old_card0=$value ;; card1) old_card1=$value ;; esac
        done < "$baseline"
    fi
    # Apply the recorded original profiles if known; otherwise recover a
    # native stereo HDMI port from the previous app's "off" profiles.
    if [[ -n $old_card0 || -n $old_card1 ]]; then
        for profile_spec in "${CARD0}=${old_card0}" "${CARD1}=${old_card1}"; do
            local card=${profile_spec%%=*} profile=${profile_spec#*=}
            if [[ $profile =~ ^[A-Za-z0-9_:.-]+$ ]]; then
                user_cmd "$user" "$uid" "$home" pactl set-card-profile \
                    "$card" "$profile" >/dev/null 2>&1 || true
            fi
        done
    else
        local first=$CARD0 second=$CARD1
        if ! connected_port 1 && connected_port 2; then
            first=$CARD1 second=$CARD0
        fi
        user_cmd "$user" "$uid" "$home" pactl set-card-profile \
            "$first" output:hdmi-stereo >/dev/null 2>&1 || \
        user_cmd "$user" "$uid" "$home" pactl set-card-profile \
            "$second" output:hdmi-stereo >/dev/null 2>&1 || true
    fi
}

restore_default() {
    local user=$1 uid=$2 home=$3 baseline="$PROJECT_STATE/users/$2/audio-before-install"
    local preferred= candidate= key value
    if [[ -f $baseline ]]; then
        while IFS='=' read -r key value; do
            [[ $key != default ]] || preferred=$value
        done < "$baseline"
    fi
    # A pre-existing headphone/Bluetooth/default device takes priority.
    if [[ -n $preferred && $preferred != rpi_hdmi_audio_* ]] && \
       user_cmd "$user" "$uid" "$home" pactl list sinks short 2>/dev/null | \
            awk -v name="$preferred" '$2 == name {found=1} END {exit !found}'; then
        user_cmd "$user" "$uid" "$home" pactl set-default-sink "$preferred" >/dev/null 2>&1 && return 0
    fi
    # Otherwise use a newly visible native HDMI sink; prefer the connected port.
    local port=107c701400 other=107c706400
    if ! connected_port 1 && connected_port 2; then
        port=107c706400 other=107c701400
    fi
    for prefix in "$port" "$other"; do
        candidate=$(user_cmd "$user" "$uid" "$home" pactl list sinks short 2>/dev/null | \
            awk -v p="$prefix" '$2 ~ /^alsa_output\./ && index($2,p) &&
                  $2 ~ /hdmi/ {print $2; exit}' || true)
        if [[ -n $candidate ]]; then
            user_cmd "$user" "$uid" "$home" pactl set-default-sink "$candidate" >/dev/null 2>&1 && return 0
        fi
    done
    # Other native outputs can still be selected by the normal desktop UI.
    return 1
}

restore_session() {
    local user=$1 uid=$2 home=$3
    if ! user_cmd "$user" "$uid" "$home" systemctl --user restart \
            pipewire pipewire-pulse wireplumber; then
        ISSUES+=("Kan de audiodiensten van $user niet herstarten.")
        return
    fi
    local ready=0
    for _ in {1..40}; do
        if user_cmd "$user" "$uid" "$home" pactl info >/dev/null 2>&1; then
            ready=1; break
        fi
        sleep 0.2
    done
    if (( ! ready )); then
        ISSUES+=("Audio van $user is niet bereikbaar; herstel na opnieuw inloggen.")
        return
    fi
    restore_profiles "$user" "$uid" "$home"
    # Give WirePlumber time to expose the restored native HDMI sink.
    sleep 0.5
    if ! restore_default "$user" "$uid" "$home"; then
        ISSUES+=("Geen aangesloten native HDMI-uitvoer zichtbaar bij $user.")
    fi

    local meta="$PROJECT_STATE/users/$uid"
    if [[ -f $meta/filter-chain-was-active ]]; then
        user_cmd "$user" "$uid" "$home" systemctl --user restart filter-chain.service || true
    elif user_cmd "$user" "$uid" "$home" systemctl --user is-active --quiet filter-chain.service; then
        # Leave an independently configured filter-chain running, but unload
        # this application's nodes by restarting with its fragment removed.
        if compgen -G "$home/.config/pipewire/filter-chain.conf.d/*.conf" >/dev/null || \
           compgen -G '/etc/pipewire/filter-chain.conf.d/*.conf' >/dev/null; then
            user_cmd "$user" "$uid" "$home" systemctl --user restart filter-chain.service || true
        else
            user_cmd "$user" "$uid" "$home" systemctl --user stop filter-chain.service || true
        fi
    fi
}

# Do not delete a changed ALSA file or DTS library on the assumption that it
# still belongs to this app. An unexpected include needs manual inspection.
if [[ -e $ALSA_INCLUDE ]] && \
   [[ $(cat -- "$ALSA_INCLUDE") != '<confdir:pcm/dca.conf>' ]]; then
    printf 'Bestaand %s is aangepast. Niets verwijderd: controleer dit bestand eerst.\n' \
        "$ALSA_INCLUDE" >&2
    exit 1
fi

# Apply per-user cleanup to every local desktop account; this system-wide app
# can have user state and Frequency configuration in more than one account.
declare -a ONLINE=()
declare -a USERS=()
while IFS=: read -r user _ uid gid _ home _; do
    [[ $uid =~ ^[0-9]+$ ]] || continue
    if (( uid < 1000 || uid >= 60000 )) && [[ $user != "$TARGET_USER" ]]; then
        continue
    fi
    [[ $home == /* && -d $home && ! -L $home ]] || continue
    USERS+=("$user:$uid:$home")
done < <(getent passwd)

if (( ${#USERS[@]} == 0 )); then
    echo 'Geen gebruikersmap gevonden; verwijdering afgebroken.' >&2
    exit 1
fi

target_home=$(getent passwd "$TARGET_USER" | cut -d: -f6)
if [[ ! -S /run/user/$TARGET_UID/bus ]]; then
    echo 'Geen actieve desktopsessie. Meld je aan in de desktop en voer de remover opnieuw uit.' >&2
    exit 1
fi

printf 'HDMI Audio Encoder verwijderen en gewone audio herstellen...\n'
rm -f /etc/xdg/autostart/rpi-hdmi-audio-restore.desktop

for entry in "${USERS[@]}"; do
    IFS=: read -r user uid home <<< "$entry"
    stop_app_processes "$uid"
    if [[ -S /run/user/$uid/bus ]]; then
        ONLINE+=("$entry")
        if session_live "$user" "$uid" "$home"; then
            unload_our_modules "$user" "$uid" "$home"
        fi
    fi

    rm -f -- "$home/.config/pipewire/filter-chain.conf.d/99-rpi-hdmi-audio-frequency.conf" \
        "$home/.config/alsa-card-profile/profile-sets/rpi-hdmi0.conf" \
        "$home/.config/alsa-card-profile/profile-sets/rpi-hdmi1.conf" \
        "$home/.config/wireplumber/wireplumber.conf.d/51-rpi-hdmi-ac3.conf" \
        "$home/.config/pipewire/pipewire.conf.d/90-rpi-hdmi-audio-names.conf" \
        "$home/.config/autostart/rpi-hdmi-audio.desktop" \
        "$home/.config/autostart/rpi-hdmi-audio-restore.desktop"
    rm -rf -- "$home/.local/state/rpi-hdmi-audio"

    rmdir "$home/.config/pipewire/filter-chain.conf.d" \
        "$home/.config/pipewire/pipewire.conf.d" \
        "$home/.config/pipewire" \
        "$home/.config/alsa-card-profile/profile-sets" \
        "$home/.config/alsa-card-profile" \
        "$home/.config/wireplumber/wireplumber.conf.d" \
        2>/dev/null || true
done

# Earlier installers wrote the include to the installing account's .asoundrc.
# The marker records that action; do not touch user-maintained ALSA settings.
if [[ -f $PROJECT_STATE/asoundrc-include-added ]]; then
    legacy_home=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    [[ -d $legacy_home ]] && remove_old_asoundrc_include "$legacy_home"
fi

rm -f -- "$APP" \
    /usr/local/bin/add-channel-volume.py \
    /usr/local/bin/add-channel-volume-v2.py \
    /usr/share/applications/rpi-hdmi-audio.desktop \
    /usr/share/applications/org.wobbo.RPiHDMIAudio.desktop \
    "$ALSA_INCLUDE"
update-desktop-database /usr/share/applications >/dev/null 2>&1 || true

if [[ -f $PROJECT_STATE/dcaenc-installed-by-hdmi-audio-encoder ]]; then
    DCA_LIBDIR=
    if [[ -s $PROJECT_STATE/dca-libdir ]]; then
        DCA_LIBDIR=$(cat -- "$PROJECT_STATE/dca-libdir")
    elif command -v pkg-config >/dev/null 2>&1; then
        DCA_LIBDIR=$(pkg-config --variable=libdir alsa 2>/dev/null || true)
    fi
    if [[ $DCA_LIBDIR != /usr/lib/* && $DCA_LIBDIR != /lib/* ]] || \
       [[ $DCA_LIBDIR == *..* ]]; then
        ISSUES+=('DTS-libpad ongeldig; DTS-bestanden bewaard voor handmatige controle.')
    else
        rm -f -- /usr/bin/dcaenc /usr/include/dcaenc.h "$DCA_CONF" \
            "$DCA_LIBDIR/libdcaenc.so" "$DCA_LIBDIR/libdcaenc.so.0" \
            "$DCA_LIBDIR/libdcaenc.so.0.0.0" "$DCA_LIBDIR/libdcaenc.la" \
            "$DCA_LIBDIR/pkgconfig/dcaenc.pc" \
            "$DCA_LIBDIR/alsa-lib/libasound_module_pcm_dca.so" \
            "$DCA_LIBDIR/alsa-lib/libasound_module_pcm_dca.la"
        ldconfig
    fi
fi

# Revert a dca.conf changed on top of a pre-existing DTS installation.
if [[ -e $PROJECT_STATE/dca.conf.before-hdmi-audio-encoder ]]; then
    install -d /usr/share/alsa/pcm
    cp -a -- "$PROJECT_STATE/dca.conf.before-hdmi-audio-encoder" "$DCA_CONF"
fi

for entry in "${ONLINE[@]}"; do
    IFS=: read -r user uid home <<< "$entry"
    restore_session "$user" "$uid" "$home"
done

if (( ${#ISSUES[@]} == 0 )); then
    rm -rf -- "$PROJECT_STATE"
    printf 'Verwijderd. De standaard HDMI/desktop-audio is hersteld voor actieve gebruikers.\n'
else
    printf 'App verwijderd; controleer het volgende:\n' >&2
    printf ' - %s\n' "${ISSUES[@]}" >&2
    printf 'Herstelgegevens blijven bewaard in %s.\n' "$PROJECT_STATE" >&2
    exit 1
fi
