# Browser integration protocol

Three hops carry a download from the browser to Downpour:

1. The WebExtension in `extension/` sends a native message.
2. The browser starts the native-messaging host (`downpour.browser.native_host`),
   which relays the message over the per-user named pipe (`downpour.core.ipc`).
3. The running app answers. If it is not running, the host starts it with
   `--tray` and waits for it, except for `hello`, which never starts the app.

Every message is a JSON object with a `type`. Every reply has `ok` (boolean)
and, when `ok` is false, an `error` string.

## Native host name and extension IDs

- Host name: `io.github.matalvernaz.downpour`
- Chromium (Chrome, Edge, Brave, Vivaldi): the ID is fixed by the `key` in
  `extension/manifest.json`; see `downpour/browser/ids.py`.
- Firefox: `downpour@matalvernaz.github.io`.

## Item

The unit the extension hands over. Only `url` is required.

| field       | meaning                                                         |
|-------------|-----------------------------------------------------------------|
| url         | http or https URL as the page requested it                      |
| finalUrl    | URL after redirects, when the browser knows it                   |
| referrer    | the Referer the browser would have sent                          |
| filename    | suggested base name, no folders                                  |
| mime        | Content-Type reported by the browser                             |
| size        | total bytes, or -1 / absent when unknown                         |
| cookies     | list of cookie objects (see below) that apply to url / finalUrl  |
| userAgent   | the browser's `navigator.userAgent`                              |
| pageUrl     | URL of the tab the download came from                            |
| pageTitle   | title of that tab                                                |
| text        | link text, for links collected from a page                       |
| incognito   | true when the download came from a private window                |

Cookie objects carry the browser's own field names: `name`, `value`,
`domain`, `path`, `secure`, `httpOnly`, `hostOnly`, and `expirationDate`
(seconds since the epoch, absent for session cookies).

## Messages

`hello` - popup status check. Never starts the app.
Reply: `{"ok": true, "app": "Downpour", "version": "0.1.0", "capture": true}`,
or `{"ok": false, "running": false}` when the app is not running.

`offer` - the browser started a download: `{"type": "offer", "download": Item}`.
The extension pauses the browser download first, then sends the offer.
Reply: `{"ok": true, "accept": true}` means Downpour took it, so the extension
cancels and erases the browser download. `accept: false`, `ok: false`, or no
reply means the extension resumes the browser download.

`add` - user picked "Download with Downpour" on one or more links:
`{"type": "add", "items": [Item, ...], "pick": false}`. With `pick: true`
(every link on a page) the app shows a checklist so the user chooses which to
take. Reply: `{"ok": true}`.

`media` - grab the video or audio on a page:
`{"type": "media", "page": Item, "found": [Stream, ...]}` where the item's
`url` is the page URL. `found` (newest first, possibly empty) lists what the
page actually played, which matters for players that stream from a private
address: each Stream is `{"url", "kind", "mime", "size", "cookies"}`, with
`kind` `file` (a whole file an audio or video element loaded), `hls` or
`dash` (a playlist). Downpour offers those first and yt-dlp on the page as
the last choice. Reply: `{"ok": true}`.

`show` - bring the Downpour window to the front. Reply: `{"ok": true}`.

`pass` - let the browser keep the next download it offers (the extension's
Alt+Shift+K). Reply: `{"ok": true}`.

`log` - lines from the extension's own log, which the host appends to
`logs/extension.log` itself. Never starts the app.
`{"type": "log", "version": "0.1.0", "browser": "Chrome 141", "entries": [{"t": "2026-10-10T07:42:01.123Z", "level": "info", "msg": "..."}]}`.
`level` is `debug`, `info`, `warn` or `error`; at most 500 entries of 2000
characters each are kept. Reply: `{"ok": true, "written": 1}`. See
[logging.md](logging.md).
