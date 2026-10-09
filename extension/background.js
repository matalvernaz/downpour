/*
 * Downpour browser integration: the background script.
 *
 * Chrome and Edge run this file as a service worker and Firefox runs it as an
 * event page, so both may stop it whenever it is idle. Every listener is
 * therefore registered synchronously at the top level (a restarted script
 * still receives the event that woke it), and the only state that has to
 * survive a restart is kept in storage.session.
 *
 * Messages reach Downpour through the native-messaging host
 * io.github.matalvernaz.downpour; docs/browser-protocol.md describes them.
 */
"use strict";

const api = globalThis.browser ?? globalThis.chrome;

const HOST = "io.github.matalvernaz.downpour";
const OFFER_TIMEOUT_MS = 25000; // then the browser keeps the download after all
const HELLO_TIMEOUT_MS = 10000;
const ACTION_TIMEOUT_MS = 90000; // add, media and show may have to start Downpour first
const POST_MEMORY_MS = 60000;
const POST_MEMORY_SIZE = 500;
const FIRST_BYTE_WAIT_MS = 5000;
const MISSING_HOST_BACKOFF_MS = 60000;
const MAX_LINKS = 5000;
const HELD_PREFIX = "held:";
const DEFAULTS = Object.freeze({ capture: true, capturePrivate: false, ignoredSites: [] });

// Chromium lets an extension hold a download while its file name is being
// decided, before any Save As dialog or download bubble appears. Firefox has
// no such event, so there the download is paused after it has started.
const CAN_HOLD = Boolean(api.downloads.onDeterminingFilename);

const HELPER_MISSING =
  "Downpour's browser helper is not installed - open Downpour, Options, Browser integration";
const HELPER_BROKEN =
  "Downpour's browser helper did not start - open Downpour, Options, Browser integration, and install it again";

class HostError extends Error {
  // kind: "missing", "broken", "timeout", "refused" (Downpour said no) or "failed"
  constructor(message, kind) {
    super(message);
    this.kind = kind;
  }
}

// An error whose message is already a sentence for the user.
class Problem extends Error {}

// ---------------------------------------------------------------------------
// Small helpers

function isWebUrl(url) {
  try {
    const { protocol } = new URL(url);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

function withoutHash(url) {
  const at = url.indexOf("#");
  return at < 0 ? url : url.slice(0, at);
}

function baseName(path) {
  return String(path || "").split(/[\\/]/).pop();
}

// Drop absent and empty fields so messages stay small.
function compact(object) {
  return Object.fromEntries(
    Object.entries(object).filter(([, value]) => value !== undefined && value !== null && value !== "")
  );
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// The Referer a browser sends by default (strict-origin-when-cross-origin):
// the whole page address within one origin, only the origin across origins,
// and nothing from https to http.
function referrerFor(pageUrl, targetUrl) {
  if (!isWebUrl(pageUrl) || !isWebUrl(targetUrl)) return undefined;
  const page = new URL(pageUrl);
  const target = new URL(targetUrl);
  if (page.protocol === "https:" && target.protocol === "http:") return undefined;
  if (page.origin !== target.origin) return `${page.origin}/`;
  page.hash = "";
  page.username = "";
  page.password = "";
  return page.href;
}

async function getSettings() {
  return { ...DEFAULTS, ...(await api.storage.local.get(DEFAULTS)) };
}

// ---------------------------------------------------------------------------
// Talking to Downpour

function classifyError(error) {
  if (error instanceof HostError) return error;
  const text = String((error && error.message) || error || "unknown error");
  // Chromium: "Specified native messaging host not found.", "Access to the
  // specified native messaging host is forbidden." Firefox: "No such native
  // application ...", "... does not have permission ...".
  if (/not found|no such native application|not registered|forbidden|does not have permission/i.test(text)) {
    return new HostError(text, "missing");
  }
  // Chromium: "Failed to start native messaging host.", "Native host has
  // exited." Firefox reports a host that died as "An unexpected error occurred".
  if (/failed to start|has exited|unexpected error/i.test(text)) {
    return new HostError(text, "broken");
  }
  return new HostError(text, "failed");
}

function problemText(error) {
  if (error instanceof Problem) return error.message;
  const e = classifyError(error);
  switch (e.kind) {
    case "missing":
      return HELPER_MISSING;
    case "broken":
      return HELPER_BROKEN;
    case "timeout":
      return "Downpour did not answer in time.";
    case "refused":
      return e.message;
    default:
      return `Something went wrong talking to Downpour: ${e.message}`;
  }
}

function sendToHost(message, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new HostError("Downpour did not answer in time", "timeout")),
      timeoutMs
    );
    api.runtime.sendNativeMessage(HOST, message).then(
      (reply) => {
        clearTimeout(timer);
        if (reply && typeof reply === "object") resolve(reply);
        else reject(new HostError("the browser helper sent an empty answer", "broken"));
      },
      (error) => {
        clearTimeout(timer);
        reject(classifyError(error));
      }
    );
  });
}

// After a "helper missing" failure, leave downloads alone for a minute
// instead of pausing every one of them only to resume it again.
let hostMissingUntil = 0;

function noteHostWorking() {
  hostMissingUntil = 0;
  api.storage.session
    .get("problem")
    .then(({ problem }) => (problem ? setProblem("") : undefined))
    .catch(() => {});
}

function noteHostProblem(error) {
  const e = classifyError(error);
  if (e.kind === "missing" || e.kind === "broken") {
    hostMissingUntil = Date.now() + MISSING_HOST_BACKOFF_MS;
    setProblem(problemText(e)).catch(() => {});
  }
}

// For things the user asked for: add, media, show.
async function request(message) {
  let reply;
  try {
    reply = await sendToHost(message, ACTION_TIMEOUT_MS);
  } catch (error) {
    noteHostProblem(error);
    throw error;
  }
  noteHostWorking();
  if (!reply.ok) throw new HostError(reply.error || "Downpour turned the request down.", "refused");
  return reply;
}

async function helloStatus() {
  try {
    const reply = await sendToHost({ type: "hello" }, HELLO_TIMEOUT_MS);
    noteHostWorking(); // the helper itself answered, whatever it said
    if (reply.ok) {
      let text = `Downpour is running, version ${reply.version || "unknown"}`;
      if (reply.capture === false) text += ". Taking over downloads is turned off in Downpour's options";
      return { state: "running", text };
    }
    if (reply.running === false) return { state: "stopped", text: "Downpour is not running" };
    const reason = String(reply.error || "no reason given");
    return { state: "error", text: reason.startsWith("Downpour") ? reason : `Could not check Downpour: ${reason}` };
  } catch (error) {
    noteHostProblem(error);
    return { state: classifyError(error).kind, text: problemText(error) };
  }
}

// ---------------------------------------------------------------------------
// Toolbar button: its name says whether capture is on, or what is wrong.

async function setProblem(text) {
  await api.storage.session.set({ problem: text });
  await refreshToolbar();
}

async function refreshToolbar() {
  const [{ problem }, settings] = await Promise.all([api.storage.session.get("problem"), getSettings()]);
  let title = settings.capture ? "Downpour - capturing downloads" : "Downpour - not capturing downloads";
  if (problem) title = `Downpour - ${problem}`;
  await api.action.setTitle({ title });
  await api.action.setBadgeText({ text: problem ? "!" : "" });
  if (problem) await api.action.setBadgeBackgroundColor({ color: "#B00020" });
}

// A failure of something the user just asked for. A page alert is spoken by
// screen readers straight away; pages that refuse scripts get the badge.
function tellUser(tabId, message) {
  const text = `Downpour: ${message}`;
  if (typeof tabId === "number" && tabId >= 0) {
    api.scripting
      .executeScript({ target: { tabId }, func: (words) => alert(words), args: [text] })
      .catch(() => setProblem(message).catch(() => {}));
  } else {
    setProblem(message).catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Form submissions. Downpour can only repeat a GET, so a download that
// answered a POST stays with the browser. The record is short-lived and
// only ever holds addresses.

const recentPosts = new Map(); // address without #fragment -> time seen

function rememberRequest(details) {
  if (details.method !== "POST") return;
  const now = Date.now();
  const key = withoutHash(details.url);
  recentPosts.delete(key); // re-insert so the map stays oldest first
  recentPosts.set(key, now);
  for (const [url, seen] of recentPosts) {
    if (now - seen <= POST_MEMORY_MS && recentPosts.size <= POST_MEMORY_SIZE) break;
    recentPosts.delete(url);
  }
}

function wasPosted(url) {
  if (!url) return false;
  const seen = recentPosts.get(withoutHash(url));
  return seen !== undefined && Date.now() - seen <= POST_MEMORY_MS;
}

api.webRequest.onBeforeRequest.addListener(rememberRequest, {
  urls: ["<all_urls>"],
  types: ["main_frame", "sub_frame"],
});

// ---------------------------------------------------------------------------
// Cookies

async function cookieStoreFor({ incognito, cookieStoreId }) {
  if (cookieStoreId) return cookieStoreId; // Firefox: private windows and containers
  if (!incognito) return undefined; // the normal store
  try {
    const stores = await api.cookies.getAllCookieStores();
    const store = stores.find((s) => s.incognito === true) || stores.find((s) => s.id === "1");
    if (store) return store.id;
  } catch {
    // fall through
  }
  return null; // never send a private download the normal profile's cookies
}

function slimCookie(cookie) {
  const out = {
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain,
    path: cookie.path,
    secure: cookie.secure,
    httpOnly: cookie.httpOnly,
    hostOnly: cookie.hostOnly,
  };
  if (!cookie.session && typeof cookie.expirationDate === "number") out.expirationDate = cookie.expirationDate;
  return out;
}

async function cookiesForUrl(url, storeId, cache) {
  // Which cookies apply depends on scheme, host and path, never the query,
  // so links that differ only after "?" share one lookup.
  const parsed = new URL(url);
  const key = `${storeId || ""} ${parsed.protocol}//${parsed.host}${parsed.pathname}`;
  if (cache && cache.has(key)) return cache.get(key);
  const query = storeId ? { url, storeId } : { url };
  let cookies = [];
  try {
    cookies = await api.cookies.getAll(query);
  } catch (error) {
    // Firefox with first-party isolation insists on a firstPartyDomain.
    if (/firstPartyDomain/.test(String(error && error.message))) {
      cookies = await api.cookies.getAll({ ...query, firstPartyDomain: null }).catch(() => []);
    }
  }
  const slim = cookies.map(slimCookie);
  if (cache) cache.set(key, slim);
  return slim;
}

async function cookiesFor(urls, context, cache) {
  const storeId = await cookieStoreFor(context);
  if (storeId === null) return [];
  const found = new Map();
  for (const url of new Set(urls.filter(isWebUrl))) {
    for (const cookie of await cookiesForUrl(url, storeId, cache)) {
      found.set(`${cookie.domain}\t${cookie.path}\t${cookie.name}`, cookie);
    }
  }
  return [...found.values()];
}

// ---------------------------------------------------------------------------
// Capturing downloads

const held = new Set(); // download ids this copy of the script is deciding on

function markHeld(id) {
  return api.storage.session.set({ [HELD_PREFIX + id]: Date.now() });
}

function unmarkHeld(id) {
  return api.storage.session.remove(HELD_PREFIX + id);
}

async function findDownload(id) {
  try {
    const [item] = await api.downloads.search({ id });
    return item;
  } catch {
    return undefined;
  }
}

async function tryPause(id) {
  try {
    await api.downloads.pause(id);
    return true;
  } catch {
    return false;
  }
}

async function resumeQuietly(id) {
  try {
    await api.downloads.resume(id);
  } catch (error) {
    console.warn("Downpour: could not resume download", id, error);
  }
}

// If an earlier copy of this script stopped in the middle of an offer, its
// downloads are still paused. Resume them.
async function releaseOrphans() {
  const stored = await api.storage.session.get(null);
  for (const key of Object.keys(stored)) {
    if (!key.startsWith(HELD_PREFIX)) continue;
    const id = Number(key.slice(HELD_PREFIX.length));
    if (held.has(id)) continue;
    await api.storage.session.remove(key);
    const item = await findDownload(id);
    if (item && item.paused) await resumeQuietly(id);
  }
}

// Checks that need nothing asynchronous; returns why to skip, or "".
function skipReason(item) {
  if (item.byExtensionId) return "started by an extension";
  if (item.state && item.state !== "in_progress") return "not in progress";
  if (!isWebUrl(item.url) || (item.finalUrl && !isWebUrl(item.finalUrl))) return "not an http or https address";
  if (wasPosted(item.url) || wasPosted(item.finalUrl)) return "the answer to a form submission";
  if (Date.now() < hostMissingUntil) return "the browser helper is missing";
  if (held.has(item.id)) return "already being offered";
  return "";
}

async function hasSiteAccess(urls) {
  const origins = [...new Set(urls.filter(isWebUrl).map((url) => `${new URL(url).protocol}//${new URL(url).hostname}/*`))];
  try {
    return await api.permissions.contains({ origins });
  } catch {
    return false;
  }
}

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }
}

// The user switched capture off for a site in the panel. The page the
// download came from counts as well as the download's own address.
function onIgnoredSite(settings, urls) {
  const ignored = (settings.ignoredSites || []).map((site) => String(site).toLowerCase());
  return urls.some((url) => {
    const host = hostOf(url);
    return host && ignored.some((site) => host === site || host.endsWith(`.${site}`));
  });
}

async function wantsCapture(item) {
  const settings = await getSettings();
  if (!settings.capture) return false;
  if (item.incognito && !settings.capturePrivate) return false;
  if (onIgnoredSite(settings, [item.url, item.finalUrl, item.referrer])) return false;
  // Without access to the site there are no cookies and no view of form
  // submissions, so Downpour could fetch the wrong thing.
  return hasSiteAccess([item.url, item.finalUrl]);
}

async function sourceTab(incognito) {
  try {
    const [tab] = await api.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && Boolean(tab.incognito) === Boolean(incognito) && isWebUrl(tab.url)) return tab;
  } catch {
    // no window
  }
  return undefined;
}

async function describeDownload(item) {
  const context = { incognito: Boolean(item.incognito), cookieStoreId: item.cookieStoreId };
  const tab = await sourceTab(item.incognito);
  return compact({
    url: item.url,
    finalUrl: item.finalUrl,
    referrer: item.referrer ? referrerFor(item.referrer, item.finalUrl || item.url) : undefined,
    filename: baseName(item.filename),
    mime: item.mime,
    size: item.totalBytes > 0 ? item.totalBytes : -1,
    cookies: await cookiesFor([item.url, item.finalUrl], context),
    userAgent: navigator.userAgent,
    pageUrl: tab && tab.url,
    pageTitle: tab && tab.title,
    incognito: Boolean(item.incognito),
  });
}

async function offer(item) {
  try {
    const reply = await sendToHost({ type: "offer", download: await describeDownload(item) }, OFFER_TIMEOUT_MS);
    noteHostWorking();
    return reply.ok === true && reply.accept === true;
  } catch (error) {
    noteHostProblem(error);
    return false;
  }
}

// Downpour took the download: cancel the browser's copy and drop it from the
// download list. False if the browser's copy is somehow still alive.
async function takeOver(id) {
  try {
    await api.downloads.cancel(id);
  } catch {
    // finished or gone meanwhile; the search below tells which
  }
  const item = await findDownload(id);
  if (item && (item.paused || item.state === "in_progress")) return false;
  if (item && item.state === "interrupted") await api.downloads.erase({ id }).catch(() => {});
  return true;
}

// Firefox keeps no partial file for a download paused before its first byte,
// and such a download can never be resumed. Wait for that byte.
async function waitForFirstByte(id) {
  const deadline = Date.now() + FIRST_BYTE_WAIT_MS;
  for (;;) {
    const item = await findDownload(id);
    if (!item || item.state !== "in_progress" || item.paused) return undefined;
    if (item.bytesReceived > 0) return item;
    if (Date.now() > deadline) return undefined;
    await sleep(100);
  }
}

// Pause, offer, then cancel and erase or resume. `release` is Chromium's
// suggest(): until it is called the download cannot finish, show a Save As
// dialog or appear in the download bubble. It is called on every path
// except a successful takeover, so "ask where to save" keeps working.
async function offerDownload(item, release) {
  const holding = typeof release === "function";
  let paused = false;
  let tookOver = false;
  held.add(item.id);
  try {
    if (!(await wantsCapture(item))) return;
    let current = item;
    if (!holding) {
      current = await waitForFirstByte(item.id);
      if (!current) return; // finished, failed or stalled: the browser keeps it
    }
    await markHeld(item.id);
    paused = await tryPause(item.id);
    if (!paused && !holding) return; // it finished meanwhile, so Firefox keeps it
    if (await offer(current)) tookOver = await takeOver(item.id);
  } finally {
    if (paused && !tookOver) await resumeQuietly(item.id);
    held.delete(item.id);
    await unmarkHeld(item.id).catch(() => {});
    if (holding && !tookOver) release();
  }
}

if (CAN_HOLD) {
  api.downloads.onDeterminingFilename.addListener((item, suggest) => {
    if (skipReason(item)) return undefined; // the browser calls suggest() itself
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      try {
        suggest(); // no arguments: keep the browser's own name and location
      } catch (error) {
        console.warn("Downpour: could not release download", item.id, error);
      }
    };
    offerDownload(item, release).catch((error) => {
      console.error("Downpour: offering a download failed", error);
      release();
    });
    return true; // suggest() will be called asynchronously
  });
} else {
  api.downloads.onCreated.addListener((item) => {
    if (skipReason(item)) return;
    offerDownload(item, null).catch((error) => console.error("Downpour: offering a download failed", error));
  });
}

// ---------------------------------------------------------------------------
// Context menus

const MENUS = [
  { id: "downpour-link", title: "Download link with Downpour", contexts: ["link"] },
  { id: "downpour-selection", title: "Download selected links with Downpour", contexts: ["selection"] },
  { id: "downpour-page-links", title: "Download all links with Downpour", contexts: ["page", "frame"] },
  { id: "downpour-media", title: "Download this media with Downpour", contexts: ["video", "audio", "image"] },
  { id: "downpour-grab", title: "Grab video or audio from this page with Downpour", contexts: ["page", "frame"] },
];

async function createMenus() {
  await api.contextMenus.removeAll();
  for (const menu of MENUS) {
    api.contextMenus.create(menu, () => void api.runtime.lastError);
  }
}

// Runs inside the page, so it must not use anything from this file.
function collectLinks(selectionOnly, limit) {
  const tidy = (text) => String(text || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const address = (a) => {
    try {
      return new URL(typeof a.href === "string" ? a.href : a.getAttribute("href"), document.baseURI).href;
    } catch {
      return "";
    }
  };
  const imageAlt = (a) => {
    const image = a.querySelector("img[alt]");
    return image ? image.alt : "";
  };
  let anchors;
  if (selectionOnly) {
    const found = new Set();
    const selection = document.getSelection();
    for (let i = 0; selection && i < selection.rangeCount; i++) {
      const range = selection.getRangeAt(i);
      let root = range.commonAncestorContainer;
      if (root.nodeType !== Node.ELEMENT_NODE) root = root.parentElement;
      if (!root) continue;
      const around = root.closest("a[href], area[href]");
      if (around) found.add(around);
      for (const a of root.querySelectorAll("a[href], area[href]")) {
        if (range.intersectsNode(a)) found.add(a);
      }
    }
    anchors = [...found];
  } else {
    anchors = [...document.querySelectorAll("a[href], area[href]")];
  }
  return anchors.slice(0, limit).map((a) => ({
    url: address(a),
    text: tidy(
      a.innerText || a.textContent || a.getAttribute("aria-label") || a.getAttribute("title") || a.getAttribute("alt") || imageAlt(a)
    ),
  }));
}

async function linksInPage(tab, frameId, selectionOnly) {
  if (!tab || typeof tab.id !== "number") throw new Problem("Downpour cannot read this page.");
  let results;
  try {
    results = await api.scripting.executeScript({
      target: { tabId: tab.id, frameIds: [frameId || 0] },
      func: collectLinks,
      args: [selectionOnly, MAX_LINKS * 2],
    });
  } catch {
    throw new Problem("The browser does not let extensions read the links on this page.");
  }
  const result = results && results[0] && results[0].result;
  return Array.isArray(result) ? result : [];
}

function pageContext(info, tab) {
  const url = (tab && tab.url) || info.pageUrl || "";
  return {
    url,
    // Links inside a frame are fetched with the frame as their referrer.
    referrerBase: (info.frameId && info.frameUrl) || url,
    title: tab && tab.title,
    incognito: Boolean(tab && tab.incognito),
    cookieStoreId: tab && tab.cookieStoreId,
    tabId: tab && tab.id,
  };
}

async function sendLinks(links, page, pick) {
  const own = withoutHash(page.url);
  const unique = new Map();
  for (const link of links) {
    if (!isWebUrl(link.url) || unique.has(link.url)) continue;
    if (pick && withoutHash(link.url) === own) continue; // "#top" and other in-page jumps
    unique.set(link.url, link);
    if (unique.size >= MAX_LINKS) break;
  }
  if (!unique.size) {
    if (links.length) throw new Problem("Downpour can only download addresses that start with http or https.");
    throw new Problem(pick ? "There are no links on this page." : "No links are selected.");
  }
  const context = { incognito: page.incognito, cookieStoreId: page.cookieStoreId };
  const cache = new Map();
  const items = [];
  for (const link of unique.values()) {
    items.push(
      compact({
        url: link.url,
        referrer: referrerFor(page.referrerBase, link.url),
        cookies: await cookiesFor([link.url], context, cache),
        userAgent: navigator.userAgent,
        pageUrl: page.url,
        pageTitle: page.title,
        text: link.text,
        incognito: page.incognito,
      })
    );
  }
  await request({ type: "add", items, pick });
}

async function grabMedia(page) {
  if (!isWebUrl(page.url)) throw new Problem("Downpour can only grab video or audio from web pages.");
  const context = { incognito: page.incognito, cookieStoreId: page.cookieStoreId };
  const item = compact({
    url: page.url,
    pageUrl: page.url,
    pageTitle: page.title,
    cookies: await cookiesFor([page.url], context),
    userAgent: navigator.userAgent,
    incognito: page.incognito,
  });
  await request({ type: "media", page: item });
}

async function sendMedia(info, page) {
  if (isWebUrl(info.srcUrl)) return sendLinks([{ url: info.srcUrl }], page, false);
  // Streaming players feed the element from a blob: address; the page
  // itself is what the grabber needs.
  if (info.mediaType === "video" || info.mediaType === "audio") return grabMedia(page);
  throw new Problem("This image is part of the page itself, so it has no address Downpour can download.");
}

async function handleMenu(info, tab) {
  const page = pageContext(info, tab);
  switch (info.menuItemId) {
    case "downpour-link":
      return sendLinks([{ url: info.linkUrl, text: info.linkText }], page, false);
    case "downpour-selection":
      return sendLinks(await linksInPage(tab, info.frameId, true), page, false);
    case "downpour-page-links":
      return sendLinks(await linksInPage(tab, info.frameId, false), page, true);
    case "downpour-media":
      return sendMedia(info, page);
    case "downpour-grab":
      return grabMedia(page);
    default:
      return undefined;
  }
}

api.contextMenus.onClicked.addListener((info, tab) => {
  handleMenu(info, tab).catch((error) => tellUser(tab && tab.id, problemText(error)));
});

// ---------------------------------------------------------------------------
// Keyboard shortcuts. Screen-reader users move through links with Tab, so
// "the focused link" is the natural target, the way the context menu's
// "Download link" is for mouse users.

// Runs inside the page, so it must not use anything from this file.
function focusedLink() {
  let element = document.activeElement;
  while (element && element.shadowRoot && element.shadowRoot.activeElement) {
    element = element.shadowRoot.activeElement;
  }
  const link = element && element.closest ? element.closest("a[href], area[href]") : null;
  if (!link) return null;
  let url = "";
  try {
    url = new URL(link.getAttribute("href"), document.baseURI).href;
  } catch {
    return null;
  }
  const text = String(link.innerText || link.textContent || link.getAttribute("aria-label") || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 300);
  return { url, text, focused: document.hasFocus() };
}

async function focusedLinkIn(tab) {
  if (!tab || typeof tab.id !== "number") throw new Problem("Downpour cannot read this page.");
  let results;
  try {
    results = await api.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: focusedLink });
  } catch {
    throw new Problem("The browser does not let extensions read this page.");
  }
  const found = (results || []).map((r) => r && r.result).filter((r) => r && r.url);
  return found.find((r) => r.focused) || found[0];
}

async function handleCommand(command, tab) {
  if (!tab) [tab] = await api.tabs.query({ active: true, lastFocusedWindow: true });
  const page = pageContext({}, tab);
  if (command === "grab-page-media") return grabMedia(page);
  if (command === "download-focused-link") {
    const link = await focusedLinkIn(tab);
    if (link) return sendLinks([link], page, false);
    const selected = await linksInPage(tab, 0, true).catch(() => []);
    if (selected.length) return sendLinks(selected, page, false);
    throw new Problem("No link has focus. Move to a link with Tab, then press Alt+Shift+L again.");
  }
  return undefined;
}

api.commands.onCommand.addListener((command, tab) => {
  handleCommand(command, tab).catch((error) => tellUser(tab && tab.id, problemText(error)));
});

// ---------------------------------------------------------------------------
// The popup

async function popupCommand(message) {
  switch (message.cmd) {
    case "status":
      await setProblem(""); // the popup now says what is wrong, so clear the badge
      return helloStatus();
    case "grab": {
      const tab = await api.tabs.get(message.tabId);
      await grabMedia(pageContext({}, tab));
      return { ok: true, text: "Sent this page to Downpour." };
    }
    case "show":
      await request({ type: "show" });
      return { ok: true, text: "Downpour is open." };
    default:
      return { ok: false, text: "Unknown command." };
  }
}

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== api.runtime.id || !message || typeof message.cmd !== "string") return false;
  popupCommand(message).then(sendResponse, (error) => sendResponse({ ok: false, text: problemText(error) }));
  return true; // answering asynchronously
});

// ---------------------------------------------------------------------------
// Start-up

api.runtime.onInstalled.addListener(() => {
  createMenus().catch((error) => console.error("Downpour: could not create menus", error));
});
api.runtime.onStartup.addListener(() => {
  createMenus().catch((error) => console.error("Downpour: could not create menus", error));
});
api.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "capture" in changes) refreshToolbar().catch(() => {});
});

refreshToolbar().catch(() => {});
releaseOrphans().catch((error) => console.warn("Downpour: could not check paused downloads", error));
