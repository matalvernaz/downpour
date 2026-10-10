# Logs and troubleshooting

Downpour keeps logs so that a problem can be explained after it happened,
without having to make it happen again.

## Where they are

Open Downpour's Help menu and choose Open the log folder. The same button is
on the General tab of Options. The folder is
`%LOCALAPPDATA%\Downpour\logs`, or `data\logs` beside `Downpour.exe` in a
portable copy.

The files:

- `downpour.log`: the Downpour window and everything it does. Each download,
  the queue, requests from the browser, settings changes, every error message
  you were shown, and which menu commands you used.
- `native-host.log`: the browser helper, which carries messages between the
  browser extension and Downpour. One short line or two per message.
- `extension.log`: what the browser extension saw and decided, such as which
  downloads it offered to Downpour and why it left others to the browser.
  The extension sends these lines through the browser helper.
- `cli.log`: runs of `downpour-cli get`.
- `crash.log`: written only if Downpour dies inside Windows or wxWidgets code,
  where nothing else gets to write. It holds the stack of every thread. The
  next start renames it to `crash.log.1` and mentions it in `downpour.log`.

Files are limited in size. `downpour.log` and `cli.log` start a new file at
5 MB and keep five old ones (`downpour.log.1` and so on). The browser helper
and extension logs start afresh at 1 MB and keep one old file.

## Detailed logging

Detailed logging is on by default. Turn it off on the General tab of Options
("Write detailed logs for troubleshooting"); the logs then keep starts and
stops, warnings and errors only.

Detailed logs add:

- every request to a server and its answer: status, size, ranges, type,
  file name, redirects, and the start of any error page, which often says why
  a server refused ("Request has expired");
- each connection opening and closing, and each retry with its wait;
- requests between the browser helper, the command line and the window;
- yt-dlp's command line and output, and its progress every 10 seconds;
- what Downpour said through NVDA and which menu commands were used.

To get detailed logs whatever Options says, start Downpour with `--debug`,
or set the environment variable `DOWNPOUR_DEBUG=1` (the browser helper
honours it too).

## What is never logged

- Cookie names or values. Logs only count cookies and name their domains.
- Passwords, the values of custom headers, and the proxy address.
- Secret parts of web addresses. Addresses lose any user name and password,
  and query values that look like tokens or signatures become `<hidden>` or
  `<N characters>`. Parameter names stay, so a log still shows that a link was
  signed and when it expired.
- The browser helper's own log has no addresses, file names or cookie
  domains at all; `downpour.log` holds the download details instead.

## Reading the logs

Every line starts with the time, the process ID and the thread. Downloads
run on threads named `download-12` (the controller) and `download-12-conn`
(its connections), and their lines say `download 12:`, so searching for
`download 12:` gives one download's whole story: why it started, each
request, each failure and retry, and how it ended.

The first lines of each start say which version and build is running, the
Windows version, which settings differ from the defaults, whether NVDA is
running, and whether the browser helper is registered with each browser.

If the window stops responding for 30 seconds, `downpour.log` records it,
with what every thread was doing at that moment, and records again when the
window responds.

## The browser extension's log

The extension's lines reach `extension.log` a few seconds after they happen.
When the browser helper itself is broken, they cannot get there; then open
the Downpour panel in the browser (Alt+Shift+D) and press Copy the
diagnostic log, which copies the extension's latest 300 lines to the
clipboard.
