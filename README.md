# Xposer

Expose your local work to anyone on your WiFi — instantly.

Xposer is a Linux system tray application that shares what is running on
your machine with everyone on the same network. Expose an already-running
dev server by port, or serve any folder as a website — Xposer generates a
scannable QR code and a plain LAN URL like `http://192.168.1.5:3000`.
No accounts, no tunneling, no terminal.

## Features

- **Expose Running Port** — reverse-proxies LAN traffic to a localhost port
- **Serve Folder** — internal static HTTP server for any directory
- **QR codes** — one per service, scannable by any phone on the WiFi
- **Auto-detection** — lists your currently running local ports with process names
- **Live tray menu** — start, stop, restart, and inspect each service
- **LAN-only** — nothing leaves your network; no data sent anywhere

## Requirements

- Python 3.10+
- GTK3 with PyGObject
- An appindicator implementation (libayatana-appindicator preferred)

## Install (system packages first)

Fedora:

    sudo dnf install python3-gobject gtk3 libayatana-appindicator-gtk3

Ubuntu / Debian:

    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
        gir1.2-ayatanaappindicator3-0.1

Arch:

    sudo pacman -S python-gobject gtk3 libayatana-appindicator

Then the Python dependencies:

    pip install --user -r requirements.txt

`netifaces` is optional; Xposer detects your LAN IP with the standard
library when it is absent (it does not build on Python 3.12+ anyway).

## Run

    python3 main.py

A tray icon appears (gray = idle, green = something is shared).
Right-click it to expose a port, serve a folder, or open QR codes.

## Security notes

- Services are reachable by **everyone on the same network** — treat a
  shared URL like an unlocked door into that folder or port.
- Serving your home directory or system folders triggers a warning;
  take it seriously.
- **Folder sharing exposes hidden files too.** Anything in the served
  directory — `.env`, `.git`, `.ssh` — is downloadable, and symlinks
  are followed even when they point outside the folder. Check what you
  are about to share before you share it.
- There is no authentication in v1. Stop sharing when you are done.
- "Start on Login" writes an XDG autostart entry that runs
  `python3 <this project>/main.py`. If you move or delete this
  checkout, toggle it off (or fix the entry) first.

## Troubleshooting

- **No tray icon on GNOME/Wayland** — vanilla GNOME hides tray icons.
  Xposer detects this at startup and tells you; install and enable the
  AppIndicator extension (Fedora: `sudo dnf install
  gnome-shell-extension-appindicator`, then log out and back in) or use
  a desktop that hosts StatusNotifier items.
- **Phone cannot open the URL** — your firewall is probably blocking
  inbound TCP on the shared port; allow it (e.g. `firewall-config`,
  `sudo ufw allow <port>`).
- **Wrong IP in URLs** — pick the correct network interface in Settings.

## Development

    python3 -m pytest -v
