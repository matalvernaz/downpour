/*
 * Downpour browser integration: the toolbar popup.
 *
 * Settings live in storage.local. Everything that talks to Downpour goes
 * through the background script, which owns the native-messaging host.
 */
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = Object.freeze({ capture: true, capturePrivate: false });
const ALL_SITES = { origins: ["<all_urls>"] };

const byId = (id) => document.getElementById(id);
const status = byId("status");

function say(text) {
  status.textContent = text;
}

async function ask(message) {
  const reply = await api.runtime.sendMessage(message);
  return reply || { ok: false, text: "Downpour's extension did not answer." };
}

async function loadSettings() {
  const settings = { ...DEFAULTS, ...(await api.storage.local.get(DEFAULTS)) };
  for (const key of Object.keys(DEFAULTS)) {
    const box = byId(key);
    box.checked = Boolean(settings[key]);
    box.addEventListener("change", () => {
      api.storage.local.set({ [key]: box.checked }).catch((error) => say(`Could not save that setting: ${error.message}`));
    });
  }
}

// "Capture downloads on example.com": unchecking it adds the site to
// ignoredSites, which the background script consults for every download.
async function setupSiteToggle() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  let host = "";
  try {
    const url = new URL(tab && tab.url);
    if (url.protocol === "http:" || url.protocol === "https:") host = url.hostname.toLowerCase();
  } catch {
    return;
  }
  if (!host) return;
  const box = byId("captureSite");
  byId("captureSiteLabel").textContent = `Capture downloads on ${host}`;
  const { ignoredSites } = await api.storage.local.get({ ignoredSites: [] });
  box.checked = !ignoredSites.includes(host);
  byId("siteField").hidden = false;
  box.addEventListener("change", async () => {
    const current = (await api.storage.local.get({ ignoredSites: [] })).ignoredSites.filter((site) => site !== host);
    if (!box.checked) current.push(host);
    await api.storage.local.set({ ignoredSites: current });
    say(box.checked ? `Downpour will capture downloads on ${host}.` : `Downloads on ${host} will stay in the browser.`);
  });
}

async function checkPrivateAccess() {
  try {
    byId("privateNote").hidden = await api.extension.isAllowedIncognitoAccess();
  } catch {
    // Unknown; leave the note hidden.
  }
}

async function checkSiteAccess() {
  try {
    byId("siteAccess").hidden = await api.permissions.contains(ALL_SITES);
  } catch {
    // Unknown; leave the button hidden.
  }
}

async function refreshStatus() {
  try {
    say((await ask({ cmd: "status" })).text);
  } catch (error) {
    say(`Could not check Downpour: ${error.message}`);
  }
}

// permissions.request must run straight from the click, before any await.
byId("allowSites").addEventListener("click", () => {
  api.permissions.request(ALL_SITES).then(
    (granted) => {
      say(granted ? "Downpour may now read all websites." : "Downpour was not allowed to read all websites.");
      checkSiteAccess();
    },
    (error) => say(`Could not ask for access: ${error.message}`)
  );
});

byId("grab").addEventListener("click", async () => {
  say("Sending this page to Downpour.");
  try {
    const [tab] = await api.tabs.query({ active: true, currentWindow: true });
    if (!tab) {
      say("There is no page to grab from.");
      return;
    }
    say((await ask({ cmd: "grab", tabId: tab.id })).text);
  } catch (error) {
    say(`Could not send the page to Downpour: ${error.message}`);
  }
});

byId("open").addEventListener("click", async () => {
  say("Opening Downpour.");
  try {
    say((await ask({ cmd: "show" })).text);
  } catch (error) {
    say(`Could not open Downpour: ${error.message}`);
  }
});

loadSettings().catch((error) => say(`Could not load settings: ${error.message}`));
setupSiteToggle().catch(() => {});
checkPrivateAccess();
checkSiteAccess();
refreshStatus();
