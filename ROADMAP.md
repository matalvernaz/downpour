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
- A setup program, and updates from GitHub releases that install themselves
  after asking.
- Grab offers the audio and video a page actually played, in a checklist,
  for players that stream from private addresses.
- Version 0.1.2 (2026-10-10), from the research below: search, sort and
  columns in the list; rename or move in Properties; keep awake; Recycle
  Bin; server file dates; skip existing files; crawl recovery; a night
  speed limit; custom sounds; a command after each download; Alt+Shift+K
  lets the browser keep the next download; categories by site; and for
  video and audio: audio-track language, subtitles and transcripts,
  SponsorBlock, tags and chapters, clipping by time, and MP3, M4A, Opus,
  FLAC and WAV presets. yt-dlp is pointed at Node or Bun before Deno,
  which is being wound down.

## Next

Two research passes on 2026-10-10 fed this list: what nine other download
managers already ship (IDM, Free Download Manager, JDownloader, AB Download
Manager, Gopeed, XDM, Motrix, Persepolis, Neat Download Manager), each gap
checked against Downpour's code, and what users asked for in 2025-2026
(the most-upvoted GitHub requests, forums, and download tools made by blind
developers).

Small, and worth doing first:

- Recent folders in the new-download dialogs.
- Dark mode (wx.App.MSWEnableDarkMode is in wxPython 4.3).

Video and audio, mostly yt-dlp options:

- Search YouTube and SoundCloud from Downpour (ytsearch, scsearch) into the
  existing checklist.
- Follow a channel or playlist and fetch only what is new.

Bigger:

- The interface in other languages (eight of the nine have it).
- Several named queues, each with its own limit and schedule.
- Control the running app from the command line over the existing pipe:
  list, add, pause, resume.
- Per-site settings: saved logins, connection caps, user agent, headers.
- SOCKS proxies, proxy scripts (PAC), and Windows sign-in (NTLM, Kerberos).
- A site grabber inside the app: fetch a page or crawl a site, then pick.
- FTP downloads.
- Extract archives after download (ZIP built in, 7-Zip when installed).
- Several addresses (mirrors) for one file.
- Preview media while it downloads; convert finished files.
- Import IDM's exported download list (.ef2).
- Drag links and files onto the window.
- Sites whose bot checks reject non-browser clients: "Download with the
  browser instead" exists; consider a browser-like TLS client for them.


## Considered, not planned

- Torrents. Requested, but contested ("just use qBittorrent"). Worth
  revisiting for blind users specifically, since qBittorrent's torrent list
  is reported unreadable with screen readers. Still the loudest request
  after video in 2026 (AB Download Manager's top issue).
- Premium accounts and captchas for file-hosting sites: a niche whose
  plugins break constantly, and image captchas lock blind users out anyway.
- Copy-protected (DRM) streams: not legal to break.
- ED2K and Thunder links, RSS, WebDAV, themes, plugin stores, "turbo" modes
  with dozens of connections. RSS and podcasts come up mostly in media
  players made by blind developers, not in download-manager trackers.
