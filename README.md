# RPi HDMI Audio v4

Complete test bundle for Raspberry Pi 5 / Raspberry Pi 500+ on Debian 13 GNOME.

<img height="400" alt="image" src="https://github.com/user-attachments/assets/9e72da99-e52b-4f90-a208-5ab2b4a14574" />

## What this version does

- HDMI 0 - Stereo 2.0
- HDMI 0 - Dolby Digital 5.1
- HDMI 1 - Stereo 2.0
- HDMI 1 - Dolby Digital 5.1
- Keeps active browser / YouTube audio alive during switching by parking streams on a temporary null sink.
- The temporary holding sink uses the same channel count as the target mode, so restarting Stereo stays visually 2.0.
- Primes the AC-3 sink before moving real audio to it.
- Uses the tested PipeWire description quoting form, so GNOME can show the full output title.
- Removes the older experimental ACP / WirePlumber profile files. The current direct `module-alsa-sink` design does not require them.
- Stores the last successful selection in `~/.local/state/rpi-hdmi-audio/last-choice`.
- Installs one hidden system-wide GNOME autostart entry at `/etc/xdg/autostart/rpi-hdmi-audio-restore.desktop`.
- After login, the hidden restore rebuilds the last selected Stereo or Dolby sink without opening the application window.
- Dolby sinks disable idle suspend while selected, so an automatically restored Dolby output remains usable even when no media was playing during login.
- If automatic restoration fails, normal HDMI 0 Stereo is restored so GNOME does not remain on Dummy Output.

<img height="320" alt="image" src="https://github.com/user-attachments/assets/8e2075f5-c04b-47cc-990a-4859af3617d1" />
<img height="320" alt="image" src="https://github.com/user-attachments/assets/b0a0d8a9-33c4-4ef1-9438-97e49140803f" />

## Upgrade / install

You do **not** need to uninstall the previous version first. The installer overwrites the application and launcher, removes known obsolete experimental files, and preserves the current or previously saved selection.

```bash
cd rpi-hdmi-audio-total-2026-09-08-v4
chmod +x install-rpi-hdmi-audio.sh
sudo ./install-rpi-hdmi-audio.sh
```

Select the desired output once in **RPi HDMI Audio**. Every successful selection becomes the new saved startup mode.

## Remove

```bash
chmod +x remove-rpi-hdmi-audio.sh
sudo ./remove-rpi-hdmi-audio.sh
```

The remover deletes the saved selection and restores normal GNOME HDMI 0 Stereo.

## Important Dolby note

When Dolby Digital 5.1 is active, a monitor that cannot decode AC-3 may produce loud digital noise through its own speakers. The application deliberately does not change the monitor volume.
