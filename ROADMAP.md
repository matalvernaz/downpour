# Roadmap

Priorities come from research into what download-manager users asked for in
2025 and 2026: Reddit threads, the issue trackers of AB Download Manager,
Gopeed, XDM and others, IDM's own changelog, and what blind users say on
mailing lists and forums. The short version: video grabbing and reliable
browser capture lead demand, and no current download manager is
recommended by blind users. That gap is Downpour's reason to exist.

## Done in 0.1

- Video and audio from pages via yt-dlp, HLS and DASH included, with
  quality presets, MP3 and playlists. YouTube gets a JavaScript runtime
  (Deno, else Node or Bun), which yt-dlp has needed since November 2025.
- Browser capture through native messaging (never a local web server, which
  is how a 2026 bug in another downloader let any website write files), with
  cookies, referrer and user agent passed along, a per-site off switch, and
  a keyboard shortcut for the focused link.
- Resume across dropped connections, restarts and expired links (Properties
  address, or Refresh address from the browser).
- Batch downloads, pasting many links, importing and exporting lists.
- Multi-connection downloads into one sparse file, so there is no slow
  joining step at the end, which IDM users complain about with large files.
- Queue, scheduler, speed limits, and sleep or shut down when done.
- About 60 MB of memory; AB Download Manager users cite 300 MB as its worst
  flaw.
- Free, no nags, no telemetry, no bundled offers.

## Done since

- The Firefox extension is signed by Mozilla (unlisted), so it installs
  permanently.
- Detailed logs of every part, with secrets kept out (docs/logging.md).

## Next

- The Chrome Web Store listing (unlisted), then an installer that sets up
  Downpour and points each browser at its signed or store extension.
- A list of the media found on the current page, to pick from, in addition
  to handing the page to yt-dlp.
- Recent folders in the new-download dialog.
- Sites whose bot checks reject non-browser clients: "Download with the
  browser instead" exists; consider a browser-like TLS client for them.
- Downpour's own update check, once releases are public.

## Considered, not planned

- Torrents. Requested, but contested ("just use qBittorrent"). Worth
  revisiting for blind users specifically, since qBittorrent's torrent list
  is reported unreadable with screen readers.
- Premium accounts and captchas for file-hosting sites: a niche whose
  plugins break constantly, and image captchas lock blind users out anyway.
- Copy-protected (DRM) streams: not legal to break.
- ED2K and Thunder links, RSS, WebDAV, themes, plugin stores, "turbo" modes
  with dozens of connections.
