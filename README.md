# SimpleMail — a beautiful, minimal Fasthosts mail app for Windows

A small native Windows email app so you can read and send mail from your
Fasthosts mailbox **without logging into webmail.fasthosts.co.uk**.

Works on **both Windows x64 and Windows ARM64** with a modern, clean UI
(WebView2 + Pico CSS — the same rendering engine as Edge), including proper
HTML email rendering, a signature, and Sent/Drafts/Junk/Trash folders.

## Features

The v1.4.1 release includes in-app model setup and Start/Pause controls, owner
review for exceptions as well as drafts, readable work outcomes, a scoped
MCP connection, persistent drafts for
review, and a small Agent panel. See [connection instructions](AGENT.md) and the
[implementation plan](PLAN.md). This build has been installed and checked on the
owner's x64 machine and is published with native x64 and ARM64 downloads.

- ✅ **Inbox, Sent, Drafts, Junk, Trash** — one click in the sidebar
- ✅ **Real HTML email rendering** (sandboxed iframe, scripts stripped)
- ✅ **Compose with signature** — set it once in Settings, auto-appended
- ✅ **Automatic local draft saving and recovery**; keep or discard
  drafts and resume them from Drafts after restarting. New human drafts are saved
  on this device; existing server drafts remain visible.
- ✅ Reply, Reply All, forwarding with files, CC/BCC and outgoing attachments.
  Attachments survive local draft recovery; replies honour Reply-To and
  preserve conversation headers.
- ✅ Delete messages
- ✅ Unread counts, message snippets, modern three-pane layout
- ✅ Click links in HTML and plain-text emails to open them in your browser
- ✅ Mail refreshes every 30 seconds and when returning to the app, preserving
  the open email, search and message-list scroll position
- ✅ Credentials + signature stored locally in `%APPDATA%\SimpleMail`
- ✅ Connection test button in Settings
- ✅ Native ARM64 and x64 builds from the same codebase

## How it connects

Fasthosts mailboxes are provisioned on the **livemail** platform. The app
uses these settings (editable in Settings):

| Protocol | Server                 | Port | Security        |
|----------|------------------------|------|-----------------|
| IMAP     | `mail.livemail.co.uk`  | 993  | SSL/TLS         |
| SMTP     | `smtp.fasthosts.co.uk` | 587  | STARTTLS        |

Your login is your **full email address** + your normal mailbox password.

## Run from source

1. Install Python from https://www.python.org/downloads/ — the **ARM64**
   installer on a Snapdragon/Eloise Windows laptop, the **64-bit** installer
   otherwise. Tick *"Add python.exe to PATH"*.
2. One-time setup:
   ```
   py -3 -m pip install pywebview==5.3.2 pythonnet==3.0.5 bottle pillow
   ```
   > If you're on ARM64, also pin `cffi==1.17.1` (newer cffi has no ARM64
   > wheel and silently falls back to pure-Python, breaking pythonnet).
3. Double-click `run.bat` (or `py -3 mailapp.py`).
   `run.bat` auto-applies a small patch to pywebview that makes it work with
   .NET Core (needed for ARM64; idempotent, safe to re-run).

## Build a standalone .exe (single icon, taskbar-pinnable)

Run `build.bat` (or the commands below). The result is a single
`dist\SimpleMail.exe` with the app icon embedded — pin it to the taskbar,
drop it in your Start menu, or copy it to any machine **without Python**.

```
py -3 -m pip install pyinstaller
py -3 patch_pywebview.py
py -3 make_icon.py
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name SimpleMail ^
    --icon assets\icon.ico ^
    --add-data "web;web" --add-data "assets;assets" --add-data "runtimeconfig.json;." ^
    --hidden-import webview.platforms.winforms ^
    mailapp.py
```

**Build once on an x64 machine, once on an ARM64 machine** — PyInstaller
produces a native binary for the machine it runs on (verified: `SimpleMail.exe`
is `PE32+ ... ARM64` on this machine). The taskbar icon + pinning work via the
embedded icon and the app's `AppUserModelID` (`SimpleMail.App`).

## Requirements on the target machine (for the .exe)

- **WebView2 runtime** — preinstalled on Windows 11 and most Windows 10
  machines (it's what Edge uses). If missing, grab the Evergreen runtime:
  https://developer.microsoft.com/microsoft-edge/webview2/
- **.NET 8 WindowsDesktop runtime** — needed by pywebview's WinForms layer.
  Install "Windows Desktop Runtime 8.x" from https://dotnet.microsoft.com/download
  (ARM64 variant on ARM64 machines). Auto-installable on first run if you
  ever add a bootstrapper.

## CLI check (no GUI)

```
py -3 mailapp.py --check
```

Prints IMAP + SMTP connection results — handy for debugging.

## Auto-update & distribution

SimpleMail distributes itself through **GitHub releases**:

- Repo: https://github.com/Extra-Life-Records/SimpleMail
- Each release carries desktop and console agent executables for x64 and ARM64:
  `SimpleMail-x64.exe`, `SimpleMail-arm64.exe`, `SimpleMailAgent-x64.exe`, and
  `SimpleMailAgent-arm64.exe`. GitHub Actions builds each on its native architecture.
- On launch, the app silently checks the latest release; if a newer version
  exists it offers **Update now / Later**. Update downloads the right .exe for
  the machine's architecture, swaps it in, and relaunches — no installer, no
  manual steps.

### Releasing a new version (the workflow)

1. **Edit code** on any machine, commit, push to `main`.
2. **Bump the version** in `mailapp.py` (`APP_VERSION = "x.y.z"`), commit, push.
3. **Tag and push**:
   ```
   git tag v1.2.3
   git push origin v1.2.3
   ```
   GitHub Actions runs regression checks and builds both architectures. It checks
   the executable headers and runs an isolated console-agent smoke check before
   publishing a release with all four downloads.
4. Verify the published downloads and the installed app before claiming delivery.
   Machines running an older SimpleMail version see the update on next launch.

Requirements for the x64 GitHub build (runs on `windows-latest`):
Python 3.12, pyinstaller, pywebview 5.3.2, pythonnet 3.0.5, bottle, pillow,
cffi 2.1.1 — all installed by the workflow itself. ARM64 uses `windows-11-arm`.

## Project layout

```
mailapp.py            Python backend (IMAP/SMTP + pywebview bridge)
web/index.html        Frontend (Pico CSS)
web/app.js            Frontend logic
web/pico.min.css      Design framework (local, no CDN)
patch_pywebview.py    One-time pywebview/.NET Core compat patch (idempotent)
make_icon.py          Generates assets/icon.ico
runtimeconfig.json    .NET Core WindowsDesktop runtime config for pythonnet
run.bat               Launcher (applies patch, starts app)
build.bat             One-click .exe builder
publish_arm64.bat     Build ARM64 exe + upload to a GitHub release
.github/workflows/    GitHub Actions: auto-build x64 exe on version tags
```

## Troubleshooting

- **"pythonnet cannot be loaded"** → check `cffi==1.17.1` is installed on
  ARM64 (`py -3 -m pip install cffi==1.17.1`), and that the .NET 8
  WindowsDesktop runtime is present (`dotnet --list-runtimes`).
- **System.Windows.Forms not found** → the runtimeconfig.json next to the
  app forces the WindowsDesktop runtime; make sure it ships with the .exe
  (it's bundled by build.bat) or the .NET Desktop runtime is installed.
- **IMAP login failed but SMTP works** → wrong IMAP host. Fasthosts uses
  `mail.livemail.co.uk`, *not* `imap.1and1.co.uk`.
- **Credentials** are saved plaintext in `%APPDATA%\SimpleMail\config.json`.
  Don't share that file.
