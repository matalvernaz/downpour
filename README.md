# Downpour

A free, open-source download manager for Windows, built from the first line
to work with a screen reader. It does the things Internet Download Manager is
known for, adds some IDM does not, and costs nothing.

Status: early development, version 0.1. It works, and it is being tested.

## Why another download manager

- Free and open source, with no licence nags, no ads and no telemetry.
- Native Windows controls throughout, checked against what NVDA actually
  sees. Live progress never makes the screen reader chatter: figures update
  silently and you hear them when you ask.
- Speaks through your own NVDA (your voice and settings) when a download
  finishes or fails, with sounds, and Windows notifications only when nothing
  was spoken, so you never hear the same thing twice.

## Features

Downloading

- Up to 32 connections per file with dynamic segmentation: a connection that
  finishes early takes over half of the biggest remaining piece, so the
  download ends with every connection busy.
- Pause and resume, also after closing Downpour, a crash or a power cut.
  Progress is only recorded once the bytes are safely on disk.
- Automatic retries with backoff; recovery from dropped connections; servers
  that limit connections or cap range sizes are handled gracefully.
- A queue with a limit on simultaneous downloads, a global speed limit and
  per-download limits, and a scheduler that starts and stops downloads at set
  times and can sleep, hibernate or shut down the computer when all are done.
- Renew an expired link without losing progress: change the address in
  Properties, or choose Refresh address from the browser and start the
  download again in your browser.

Video and audio

- Grab video or audio from over a thousand sites through yt-dlp: best quality
  MP4, any format, 1080p, 720p, 480p, audio only, or MP3, plus every format
  the site lists, and whole playlists.
- Downpour uses the yt-dlp and FFmpeg you already have, or downloads the
  official builds for you after asking, verified against their published
  checksums. yt-dlp updates on its own schedule.

Browser and clipboard

- A browser extension for Firefox, Chrome and Edge hands downloads to
  Downpour, adds "Download with Downpour" to link menus, and can collect every
  link on a page into a checklist.
- Copy a link to a downloadable file and Downpour offers to download it.
- Batch downloads from an address pattern, such as img001.jpg to img100.jpg,
  and importing links from a text file.

Safety

- Adds the Mark of the Web to finished files, as browsers do, so SmartScreen
  and Office Protected View keep protecting you.
- Optional Microsoft Defender scan of every finished download.
- Checksum verification (MD5, SHA-1, SHA-256, SHA-512) before a file counts
  as finished, and a checksum window for any finished file.
- Cookies and passwords stored encrypted with Windows DPAPI.
- Uses the Windows proxy settings and certificate store.
- The download engine uses only Python's standard library.

## Keyboard

In the download list: Enter opens a finished file or shows the properties of
an unfinished one, Space pauses or resumes, Delete removes, Shift+Delete
removes and deletes the file, Ctrl+I speaks the progress, and the
Applications key opens the actions menu. Ctrl+N adds a download, Ctrl+M grabs
video or audio, Ctrl+Shift+V adds every link on the clipboard. F1 lists every
shortcut. NVDA+End reads the status bar, which always says what is running.

From the notification area (Windows+B, then the arrow keys), Enter opens
Downpour and the Applications key opens its menu.

## Command line

    downpour-cli get URL [-d FOLDER] [-o NAME] [-c CONNECTIONS] [-l LIMIT] [--checksum SUM]

Prints progress as whole lines a few seconds apart. Run the same command
again after an interruption to resume. `Downpour.exe URL` hands links to the
running window.

## Building from source

    py -3.13 -m venv --system-site-packages .venv
    .venv\Scripts\python -m pip install pytest pytest-timeout pyinstaller wxPython
    .venv\Scripts\python tools\fetch_nvda_client.py
    .venv\Scripts\python -m pytest
    build.cmd

The build lands in `dist\Downpour`. Nothing under `downpour\core` or
`downpour\cli` uses third-party packages, and a CI check makes sure neither
imports wx.

## Credits

Speech goes through NV Access's NVDA controller client (LGPL 2.1).
Video and audio come from yt-dlp and FFmpeg, run as separate programs.
