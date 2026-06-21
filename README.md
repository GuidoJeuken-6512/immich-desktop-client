# Immich Desktop Client

<p align="center">
  <img src="resources/icon.png" title="Icon of the Immich Desktop Client Application" alt="The Immich Logo behind a monitor">
</p>

The **Immich Desktop Client** is an open-source application designed to integrate seamlessly with the Immich self-hosted
media management platform. This client simplifies the process of uploading and managing media files directly from your
desktop to your Immich server.

## Features

- **Automated Media Upload**: Scans specified directories for media files and uploads new or modified files to your
  Immich server
- **Uploads to Album**: Automatically creates an album and puts all the images in it
- **Local Shelve Storage**: Tracks uploaded files using local shelve storage and SHA-1 hashes to avoid duplicate uploads
- **Replaces Modified Assets**: When a locally tracked file's content changes, the existing server asset is replaced
  in place instead of being uploaded again as a duplicate
- **Optional Remote Deletion**: Can optionally delete the corresponding server asset when a locally tracked file is
  deleted (disabled by default to protect the backup use case)
- **Checksum Validation**: Ensures data integrity with SHA-1 checksum verification during uploads
- **Resilient Uploads**: Automatically reconnects and resumes with the next pending file if the connection to the
  Immich server drops mid-sync, instead of failing the whole sync
- **Runs in the Background**: Lives in the system tray, keeps syncing after the window is closed or minimized, and
  can optionally start automatically with Windows
- **Cross-Platform**: _should_ be compatible with Windows, macOS, and Linux (only tested on Windows 11)



## Prerequisites

- Immich server instance
- API key for your Immich server (accessible from the Immich web interface)

## Usage

### Installation

#### Windows

1. Install with the Installer executable
2. modify the config file in the .immich-desktop-client folder in your home directory, or use the in-app
   Settings dialog (also lets you toggle "Start with Windows", which is enabled by default on first setup)
3. enjoy

#### Other Platforms

theoretically the python script is cross platform, therefore it should be executable on macOS and Linux

### System Tray

The app behaves like OneDrive or the Nextcloud desktop client:

- Closing or minimizing the window does **not** quit the app — it hides to the system tray and keeps syncing in
  the background.
- The tray icon's menu lets you re-open the window, pause/resume syncing, open Settings, delete all uploads, or
  fully quit the app ("Beenden").
- Only one instance runs at a time. Starting the app again (e.g. via the Start Menu or Desktop shortcut) while it's
  already running in the tray just brings the existing window to the front instead of starting a second instance.
- The Settings dialog has a "Mit Windows starten" (Start with Windows) checkbox, enabled by default on first setup.
  This is implemented via a registry autostart entry (`HKCU\...\Run`), not via `config.yaml`, so it reflects the
  actual Windows autostart state even if changed from Windows' own Startup Apps settings. When launched at login,
  the app starts directly into the tray (internally via a `--background` flag) without flashing a window.

## Configuration

> [!NOTE]
> The config file __MUST__ be in the `.immich-desktop-client` folder in your home directory!

### Configuration Fields

#### `api`

- **`key`**: Your Immich API key. This is required to authenticate with the Immich server.
- **`url`**: The base URL of your Immich server API endpoint. _Ensure the URL ends with `/api` and does not have a
  trailing slash._
- **`album`**: (Optional) The name of the album where media files will be uploaded.
- **`album_by_year`**: (Optional) If `true`, instead of using a single fixed album, the client automatically
  creates (or reuses) one album per file creation year (e.g. `2023`, `2024`) and uploads each file into the
  matching album. Overrides `album` when enabled. Defaults to `false`.
- **`delete_remote_on_local_delete`**: (Optional) If `true`, deleting a locally tracked file also deletes the
  corresponding asset on the Immich server. Defaults to `false` so that an accidental local deletion never
  destroys the server-side backup unless explicitly opted in.

#### `watchdog`

- **`directories`**: A list of directories the client will monitor for media files. Files in these directories will be
  automatically uploaded to the Immich server.
- **`recursive`**: (Optional) If `true`, the initial upload scans subdirectories as well. Defaults to `false`
  (top-level files only). Live monitoring for new files always includes subdirectories regardless of this setting.

### Example Configuration

Below is an example `config.yaml` file:

````yaml
api:
  key: apikey12345
  url: https://immich.domain.test/api
  album: Oida
  album_by_year: false
  delete_remote_on_local_delete: false
watchdog:
  recursive: false
  directories:
    - C:\Users\test\Images and Videos\
    - C:\Users\test\Screenshots\

````

## Build it yourself

1. run ``pyinstaller -n immich-dsektop-client -F src/main.py``
2. run ``resources\installer-script.iss`` with Inno Setup

## Versioning & Releases

The app version is a single source of truth in the `VERSION` file at the repository root (plain text, e.g. `1.0.0`).
It is bundled into the app (shown in the window title) and read directly by the Inno Setup script for the
installer's version info.

Pushing a commit to `master`/`main` that increases the `VERSION` file triggers
[`.github/workflows/release.yml`](.github/workflows/release.yml), which builds the executable and installer and
publishes them as a GitHub Release tagged `v<version>`. Pushing without changing `VERSION`, or lowering/repeating
it, does not create a release.
