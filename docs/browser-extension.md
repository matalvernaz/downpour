# Installing the Downpour browser extension

The extension hands downloads, links and media from your browser to Downpour.
It works in Firefox, Chrome, Edge and Brave. Setting it up takes two parts:

1. The browser helper, a small link between the browser and Downpour. You
   install it once, from Downpour.
2. The extension itself, added to each browser you use.

The extension is in the `extension` folder inside the Downpour folder,
`%LOCALAPPDATA%\Programs\Downpour\extension` when installed with
`tools\install.ps1`. In a source checkout it is the `extension` folder at
the top of the repository.

## Part 1: install the browser helper

1. Open Downpour.
2. Open the Options menu and choose Options. The shortcut is Ctrl+Shift+O.
3. Press Ctrl+Tab until you reach the Browser tab.
4. Press the Install browser helper button (Alt+I).
5. The Browser helper status box now says "registered" for each browser.

Do this again if you move the Downpour folder. When that is needed, the
status box says to install the browser helper again.

## Part 2, Chrome, Edge or Brave: load the extension unpacked

These browsers install extensions from outside their stores only in
developer mode.

1. Press Ctrl+L to go to the address bar.
2. Type the extensions address and press Enter:
   - Chrome: `chrome://extensions`
   - Edge: `edge://extensions`
   - Brave: `brave://extensions`
3. Find the Developer mode switch and turn it on with Space. In Chrome and
   Brave it is near the top of the page. In Edge it is in the left-hand pane.
4. Press the Load unpacked button. A folder picker opens.
5. Type the path of the `extension` folder into the Folder box and press
   Enter. Tip: in Downpour's Options, on the Browser tab, the Open the
   extension folder button opens that folder in File Explorer, where Alt+D
   then Ctrl+C copies its path.
6. Downpour now appears in the list of extensions. Its ID is
   `biniilicelamnmgkghjfbhajjmamlldn`. If you see a different ID, the browser
   helper will not talk to it.

Optional:

- To capture downloads in private windows, open the extension's Details and
  turn on Allow in Incognito (Chrome, Brave) or Allow in InPrivate (Edge).
  Then, in the Downpour panel, check Also capture in private windows.
- Alt+Shift+D opens the Downpour panel. To change the key, go to
  `chrome://extensions/shortcuts` (or `edge://extensions/shortcuts`).
- Edge may show a message about extensions in developer mode when it starts.
  Close it and keep the extension turned on.

## Part 2, Firefox, for testing: load the extension temporarily

A temporary extension stays loaded until Firefox closes. Use this to try the
extension before signing it.

1. Press Ctrl+L, type `about:debugging#/runtime/this-firefox` and press Enter.
2. Press the Load Temporary Add-on button. A file picker opens.
3. Go to the `extension` folder, choose `manifest.json` and press Open.
4. Downpour appears under Temporary Extensions.
5. Press Alt+Shift+D to open the Downpour panel. If it shows an Allow
   Downpour on all websites button, press it and accept. Without that access
   Downpour cannot capture downloads.

## Part 2, Firefox, permanently: sign the extension as unlisted

Regular Firefox only keeps extensions that Mozilla has signed. You can have
Mozilla sign a private copy without publishing it on addons.mozilla.org.
This is called an unlisted add-on.

### Once: create your signing keys

1. Go to `https://addons.mozilla.org/developers/` and sign in with a Mozilla
   account, or create one.
2. Accept the Firefox Add-on Distribution Agreement when the site asks.
3. Go to `https://addons.mozilla.org/developers/addon/api/key/` (Manage API
   Keys) and press Generate new credentials.
4. Copy the JWT issuer (it looks like `user:12345:678`) and the JWT secret.
   Keep the secret private. Never put it in the repository.

### Each time: sign and install

1. Open PowerShell in the Downpour folder.
2. Give it your keys for this session:

   ```powershell
   $env:WEB_EXT_API_KEY = "user:12345:678"
   $env:WEB_EXT_API_SECRET = "your JWT secret"
   ```

3. Run:

   ```powershell
   npx web-ext sign --channel=unlisted --source-dir extension
   ```

4. Wait for the command to finish. Mozilla checks the extension
   automatically, which usually takes a few minutes. The signed `.xpi` file is
   saved in the `web-ext-artifacts` folder.
5. In Firefox, press Ctrl+Shift+A to open the Add-ons Manager.
6. Press the Tools for all add-ons button and choose Install Add-on From File.
7. Choose the `.xpi` file, press Open, then press Add.
8. To capture downloads in private windows, open Downpour in the Add-ons
   Manager and set Run in Private Windows to Allow. Then, in the Downpour
   panel, check Also capture in private windows.

Notes:

- Mozilla never signs the same version twice. Before signing again, raise
  `"version"` in `extension/manifest.json`, for example from 0.1.0 to 0.1.1.
- The first signing reserves the ID `downpour@matalvernaz.github.io` for your
  account.
- The extension already declares that it collects no data, which Mozilla
  requires of new extensions.

## Using it

- Downloads: while Capture downloads is checked in the Downpour panel, each
  download the browser starts is offered to Downpour. Downpour takes the file
  types and sizes chosen on its Browser integration tab in Options. Everything else
  carries on in the browser. If Downpour is closed, it starts in the
  notification area.
- Context menu: on a link, a selection, a video, an image or the page itself,
  press Shift+F10 or the Applications key. The Downpour items are:
  - Download link with Downpour
  - Download selected links with Downpour
  - Download all links with Downpour (Downpour then lets you pick which)
  - Download this media with Downpour
  - Grab video or audio from this page with Downpour
- Keyboard shortcuts, which work on any page:
  - Alt+Shift+L downloads the focused link. Move to the link with Tab first;
    in NVDA's browse mode, arrowing onto a link only focuses it when
    "Automatically set system focus to focusable elements" is on in NVDA's
    Browse Mode settings. With no focused link, the selected links are used.
  - Alt+Shift+M grabs the video or audio on the page.
  - Alt+Shift+K lets the browser keep the next download, without turning
    capture off.
  - Alt+Shift+D opens the Downpour panel.
- The Downpour panel says whether Downpour is running. It has the capture
  checkboxes, including "Capture downloads on" the current site, which you
  uncheck to leave that site's downloads to the browser, and three buttons:
  Grab video or audio from this page, Open Downpour, and Copy the diagnostic
  log.

When something goes wrong after you chose a context-menu item, the page shows
a message box starting with "Downpour:", which your screen reader reads.

## Troubleshooting

- "Downpour's browser helper is not installed": do Part 1. In Chrome, Edge
  and Brave, also check that the extension's ID is the one given in Part 2.
- "Downpour's browser helper did not start": the Downpour folder probably
  moved. Do Part 1 again.
- Some downloads stay in the browser on purpose:
  - files Downpour's options leave to the browser;
  - downloads from private windows, unless you allowed them;
  - downloads that answer a form you submitted, because Downpour could not
    repeat the form;
  - in Firefox, very small files, which finish before Downpour can take them.
- Logs: in Downpour, Help menu, Open the log folder. `extension.log` says
  what the extension did with each download and why; `native-host.log` says
  whether the browser helper ran and reached Downpour. If the helper is broken,
  the extension's lines cannot reach the folder: press Copy the diagnostic log
  in the Downpour panel instead. See [logging.md](logging.md).
