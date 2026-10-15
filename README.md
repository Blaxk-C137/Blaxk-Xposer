# Xposer

Share your local work with anyone on your Wi-Fi — instantly.

Xposer is a small tray application for Linux and Windows. Share a dev
server that is already running, or serve any folder as a website; Xposer
gives you a link like `http://192.168.1.5:5173` (or a custom
`http://myapp.local:5173`) and a QR code for phones. No accounts, no
tunnels, no terminal.

## What's new in 1.1

- **Custom links work with dev servers.** Vite, webpack, Angular, Django,
  Rails and Next.js reject requests whose Host or Origin is not
  `localhost`. Xposer now speaks HTTP and presents every request to your
  app as if it came from your own browser, so `myapp.local` links and IP
  links pass those checks.
- **Frontend + backend apps work.** Your frontend calls
  `http://localhost:8000` — on a phone that means the phone itself.
  Xposer notices, offers **Share it too** for the backend, and rewrites
  those links (in JS, JSON, HTML, redirects and cookies) to the backend's
  LAN address. Backend CORS allow-lists written for `localhost` keep
  working.
- **HTTPS dev servers can be opened with `http://`.** Xposer talks TLS to
  your app and plain HTTP to the phone: no certificate warning. Keeping
  `https://` end to end is one click away when you need camera,
  microphone or other secure-only browser features.
- **Every local port is found and shareable.** IPv6-only servers (Vite on
  Node 17+ binds `::1`), apps bound to a specific address, WebSockets and
  live reload, server-sent events, HTTP/1.0 and raw TCP services such as
  databases. Detected ports show the tool behind them (Vite, Next.js,
  Django, Uvicorn, …).
- **Windows support** (see install below), with Windows Firewall help.
- **Faster and lighter:** one async network thread for all shared apps,
  no per-request delay, zero-copy file sending, byte ranges for video.
- **Refreshed interface:** a dashboard with live status, traffic, copy
  buttons and quick actions; same native GTK look.

## Features

- **Share a running app** — reverse proxy from the LAN to a localhost port
- **Share a folder** — static site with mobile-friendly listings,
  optional single-page-app mode, hidden files kept private by default
- **QR codes** — for the IP link (every phone can open it) and, when set,
  the custom link
- **Custom `.local` links** — published with Avahi (Linux) or zeroconf
- **Follows your network** — if your laptop's address changes, links and
  QR codes update by themselves
- **Remembers services** — previous shares reappear, ready to restart
- **LAN-only** — nothing leaves your network

## How sharing works

| You open | Phones open | Notes |
| --- | --- | --- |
| `http://localhost:5173` | `http://192.168.1.5:5173` | Same port when free, otherwise port + 10000 |
| `https://localhost:5443` | `http://192.168.1.5:5443` | Default for HTTPS apps — no certificate warning |
| `https://localhost:5443` | `https://192.168.1.5:5443` | Optional — devices warn about the dev certificate |
| `mysql://localhost:3306` | `192.168.1.5:3306` | Non-web services (databases, SSH, …) are relayed as raw TCP |

**Smart sharing** (on by default, in Settings) does the HTTP translation.
Turning it off relays raw bytes only, like Xposer 1.0.

**Custom links** are optional. iPhone, macOS, Windows and Linux open
`.local` names; many Android phones cannot, which is why the QR code uses
the IP link unless you switch it to the custom link.

### An app with a backend

1. Share the frontend.
2. When a phone loads it, the dashboard shows *"This app calls port 8000
   on this computer"* — click **Share it too**.
3. Reload the page on the phone.

The backend gets the same custom link name, and your CORS settings keep
working because the backend still sees `http://localhost:<frontend port>`
as the origin.

## Install

### Linux

Fedora:

    sudo dnf install python3-gobject gtk3 libayatana-appindicator-gtk3 \
        python3-psutil python3-pillow python3-qrcode avahi-tools

Ubuntu / Debian:

    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
        gir1.2-ayatanaappindicator3-0.1 python3-psutil python3-pil \
        python3-qrcode avahi-utils

Arch:

    sudo pacman -S python-gobject gtk3 libayatana-appindicator \
        python-psutil python-pillow python-qrcode avahi

Anything missing can come from pip: `pip install --user -r requirements.txt`.

### Windows

GTK for Python on Windows comes from [MSYS2](https://www.msys2.org/).

1. Install MSYS2, then open **MSYS2 UCRT64** from the Start menu.
2. Install GTK and the Python packages:

        pacman -Syu
        pacman -S mingw-w64-ucrt-x86_64-gtk3 mingw-w64-ucrt-x86_64-python \
            mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-pillow \
            mingw-w64-ucrt-x86_64-python-psutil mingw-w64-ucrt-x86_64-python-zeroconf \
            mingw-w64-ucrt-x86_64-python-pip mingw-w64-ucrt-x86_64-adwaita-icon-theme
        python -m pip install --user --break-system-packages qrcode

3. Run it from the UCRT64 shell (`python main.py`), or create a shortcut to
   `C:\msys64\ucrt64\bin\pythonw.exe "C:\path\to\Xposer\main.py"`
   (no console window). *Start on login* in Settings does this for you.

The first time you share something, Windows asks whether Python may use
the network — allow it on **private** networks. If you dismissed that
prompt, use *Can't connect?* → **Allow through firewall**, and make sure
your Wi-Fi is set to *Private* in Windows settings.

### Optional: zeroconf

`pip install zeroconf` enables custom `.local` links where Avahi is not
available (Windows, minimal Linux installs). Everything else works
without it.

## Run

    python3 main.py

A tray icon appears (gray = idle, green = something is shared; the badge
counts live shares). On desktops without a tray (GNOME without the
AppIndicator extension), the dashboard opens as a normal window instead;
closing it quits Xposer. Starting Xposer a second time brings the running
copy to the front.

## Security notes

- Shared links are reachable by **everyone on the same network**. Stop
  sharing when you are done.
- Folder sharing hides dotfiles (`.env`, `.git`, …) and Windows hidden
  files unless you tick *Include hidden files*, and refuses symlinks that
  point outside the folder. Sharing your home or a system folder asks for
  confirmation.
- There is no authentication; treat a link like an unlocked door into
  that app or folder.
- *Start on login* registers this checkout's location (an XDG autostart
  entry on Linux, the per-user Run key on Windows). If you move the
  folder, toggle it off and on again.

## Troubleshooting

- **Phone can't connect** — same Wi-Fi? (guest networks isolate devices.)
  Then check the firewall: *Can't connect?* in the dashboard shows the
  exact command or button for your system.
- **Custom link doesn't open on Android** — use the IP link or its QR
  code; many Android phones don't resolve `.local` names.
- **Custom link unavailable** — install Avahi (Linux) or `zeroconf`; the
  IP link is shared meanwhile.
- **Wrong address in links** (VPN, Docker, several networks) — choose the
  network interface in Settings.
- **An app still refuses the phone** — make sure *Smart sharing* is on.
  If the app is HTTPS shared as `https://`, Xposer cannot adjust it;
  share it as `http://` instead.
- **Logs** — Settings → *Log file* opens the folder with `xposer.log`.

## Development

    python3 -m pytest

UI tests need a display; on a headless machine run
`dbus-run-session -- xvfb-run -a python3 -m pytest`. Without PyGObject
(e.g. plain Windows Python) the GTK tests skip and the networking core
is still fully tested. CI runs both (`.github/workflows/ci.yml`).
