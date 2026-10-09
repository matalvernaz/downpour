# Downpour

A free, open-source download manager for Windows, built to be fully usable
with a screen reader. It aims to do everything Internet Download Manager does
and more, without the licence fee.

Status: early development. The download engine works; the window and the
browser extension are being built.

## What works now

- Multi-connection downloads with dynamic segmentation: up to 32 connections
  per file, and a connection that finishes early takes over half of the
  biggest remaining piece, so downloads finish with every connection busy.
- Pause and resume, including after closing the app or a crash. Progress is
  only recorded once the bytes are safely on disk.
- Automatic retries with backoff, recovery from dropped connections, and
  graceful handling of servers that limit connections or cap range sizes.
- A download queue with a limit on simultaneous downloads.
- Global and per-download speed limits.
- File names from the server (including international names), sorting into
  category folders, and safe handling of duplicate names.
- Checksum verification (MD5, SHA-1, SHA-256, SHA-512).
- The Mark of the Web on finished files, as browsers add, so SmartScreen and
  Office Protected View keep working.
- Cookies and passwords stored encrypted with Windows DPAPI.
- Uses the Windows proxy settings and certificate store.

## Command line

    py -m downpour get URL [-d FOLDER] [-o NAME] [-c CONNECTIONS] [-l LIMIT] [--checksum SUM]

Run the same command again after an interruption to resume.

## Development

    py -3.13 -m venv --system-site-packages .venv
    .venv\Scripts\python -m pip install pytest pytest-timeout
    .venv\Scripts\python -m pytest

Nothing under `downpour/core` or `downpour/cli` uses third-party packages.
