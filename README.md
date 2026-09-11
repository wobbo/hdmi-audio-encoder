# RPi HDMI Audio v16

Complete test bundle for Raspberry Pi 5 / Raspberry Pi 500+ on Debian 13 GNOME.

<img height="400" alt="Schermafdruk van 2026-09-11 15-29-54" src="https://github.com/user-attachments/assets/120d9bf2-e0cd-4798-a857-e54f563dd929" />
<img height="200" alt="Schermafdruk van 2026-09-11 15-30-58" src="https://github.com/user-attachments/assets/59ff0c9f-68a2-4168-b561-0844bf366100" />

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

Chrome/Chromium: New Netflix 1080p: 

https://chromewebstore.google.com/detail/new-netflix-1080p/mdlbikciddolbenfkgggdegphnhmnfcg

<img height="400" alt="Schermafdruk van 2026-09-11 15-46-51" src="https://github.com/user-attachments/assets/b6562f12-3361-458d-a1be-a981d6562b79" />
<img height="400" alt="Schermafdruk van 2026-09-11 15-27-50" src="https://github.com/user-attachments/assets/3f0c1bcc-b113-4791-a760-df880dc4f350" />


## Upgrade / install

You do **not** need to uninstall the previous version first. The installer overwrites the application and launcher, removes known obsolete experimental files, and preserves the current or previously saved selection.

```bash
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
