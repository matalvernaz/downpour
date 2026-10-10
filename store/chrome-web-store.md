# Chrome Web Store submission

Everything the developer dashboard asks for, in its order. Build the files
first with `python tools/make_store_assets.py`; they land in `dist\store`.

Dashboard: https://chrome.google.com/webstore/devconsole

## Package

Press New item and choose `dist\store\downpour-chrome-0.1.0.zip`. The
package leaves out the manifest's `key` (the store assigns its own) and the
Firefox-only settings.

## Store listing tab

Description:

```
Downpour for Chrome hands your downloads to Downpour, a free download manager for Windows made for screen reader users. It needs the Downpour app for Windows; this extension connects Chrome to it.

What it does:
- Downloads you start in Chrome go to Downpour, for the file types you choose in Downpour. Everything else stays in Chrome.
- On a link, a selection or a page, the context menu (Shift+F10 or the Applications key) offers: Download link with Downpour, Download selected links with Downpour, Download all links with Downpour (you choose from a checklist), Download this media with Downpour, and Grab video or audio from this page with Downpour.
- Keyboard shortcuts: Alt+Shift+L downloads the focused link, Alt+Shift+M grabs video or audio from the page, and Alt+Shift+D opens the Downpour panel.
- The panel says whether Downpour is running, turns capturing on or off for every site or just the current one, and can copy a diagnostic log.

Accessible by design: every control is a real, labelled button or checkbox, and problems are announced as alerts your screen reader reads.

Private by design: the extension talks only to the Downpour app on your own computer, through Chrome's native messaging. It has no server, and nothing is sent to the developer or anyone else.
```

- Category: Tools
- Language: English
- Store icon: `dist\store\icon-128.png`
- Screenshot: `dist\store\screenshot-1280x800.png`
- Small promo tile: `dist\store\promo-440x280.png`
- Homepage URL: https://github.com/matalvernaz/downpour
- Support URL: https://github.com/matalvernaz/downpour/issues
- Marquee promo tile: leave empty

## Privacy tab

Single purpose:

```
Send downloads, links and media from Chrome to the Downpour download manager running on the user's own Windows computer.
```

Permission justifications:

- downloads:
  ```
  To notice downloads the user starts, hold each one while asking the Downpour app whether it takes it, then cancel Chrome's copy only when Downpour has taken over, or let Chrome continue.
  ```
- cookies:
  ```
  Many sites only serve a file to signed-in visitors. The extension reads the cookies for the download's own address and passes them to the Downpour app on the same computer, which sends them only to that site, exactly as Chrome would.
  ```
- contextMenus:
  ```
  Adds "Download link with Downpour" and related items to the context menu for links, selections, media and pages.
  ```
- storage:
  ```
  Keeps the user's capture settings (on or off, sites to leave alone) and a short diagnostic log of the extension's own decisions on the device.
  ```
- scripting:
  ```
  When the user asks to download a selection or all links, or presses the focused-link shortcut, reads the links on the current page to send them to Downpour, and shows an alert when a request fails.
  ```
- activeTab:
  ```
  Gives access to the current tab only when the user uses the context menu, a keyboard shortcut or the panel.
  ```
- nativeMessaging:
  ```
  The extension's only channel: it sends download requests to the Downpour desktop app on the same computer and reads its answers.
  ```
- webRequest:
  ```
  Notes which page addresses answered a form submission (POST), so downloads that depend on the form's data stay in Chrome instead of being requested again without it. Only addresses are kept, in memory, for one minute.
  ```
- Host permission:
  ```
  Downloads can come from any site. Access to all sites lets the extension read the cookies that belong to each download's address and see form submissions, so Downpour fetches the same file the browser would.
  ```

Remote code: No, I am not using remote code.

Data usage: check Authentication information (cookies), Web history (the
addresses of downloads and pages) and Website content (links collected
when the user asks). Then check all three certifications.

Privacy policy URL: https://gist.github.com/matalvernaz/8a85b46d09ac8675ab8a93d106a2ccc6
(the public copy of `store/privacy-policy.md`; update the gist when the policy changes).

## Distribution tab

- Payments: Free of charge
- Visibility: Unlisted
- Regions: all regions

## Test instructions, if the dashboard asks

```
This extension is the companion of the Downpour desktop app for Windows. Without the app, the panel says Downpour's browser helper is not installed and every download stays in Chrome. With it, downloads of the file types chosen in Downpour are handed to the app, and the context menu items send links to it.
```

## Store item

ID `papnanikngmdnacmhecbigmbkbjfaboa`, listed in `CHROMIUM_EXTENSION_IDS`
in `downpour/browser/ids.py` so the browser helper accepts the store copy
as well as the unpacked one. A Downpour installed before that change
needs installing again.
