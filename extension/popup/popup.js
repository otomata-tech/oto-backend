// Popup controller. Talks to background via runtime messages.

import { DEFAULT_CONFIG, getConfig, setConfig, redirectUri } from "../lib/config.js";

const $ = (id) => document.getElementById(id);

function show(id) { $(id).hidden = false; }
function hide(id) { $(id).hidden = true; }

function flash(message, kind = "success") {
  const el = $("flash");
  el.textContent = message;
  el.className = kind;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 3500);
}

function send(type, payload = {}) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type, ...payload }, (resp) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (resp?.error) return reject(new Error(resp.error));
      resolve(resp?.result);
    });
  });
}

async function refreshLinkedinStatus() {
  try {
    const me = await send("me");
    const li = me.linkedin || {};
    const status = $("li-status");
    const label = $("li-status-label");
    if (li.configured) {
      const when = li.set_at ? new Date(li.set_at.replace(" ", "T") + "Z").toLocaleString("fr-FR") : "";
      status.className = "status status-green";
      label.textContent = `Session côté serveur · ${when}`;
    } else {
      status.className = "status status-red";
      label.textContent = "Aucune session côté serveur";
    }
  } catch (err) {
    const status = $("li-status");
    const label = $("li-status-label");
    status.className = "status status-red";
    label.textContent = `Erreur: ${err.message}`;
  }
}

async function renderAuth() {
  const { authed, user } = await send("authStatus");
  if (!authed) {
    show("signed-out");
    hide("signed-in");
    return;
  }
  hide("signed-out");
  show("signed-in");
  $("user-name").textContent = user?.name || user?.email || user?.sub || "—";
  $("user-email").textContent = user?.email || "";
  await refreshLinkedinStatus();
}

async function openSettings() {
  const cfg = await getConfig();
  $("cfg-logto-endpoint").value = cfg.logtoEndpoint;
  $("cfg-logto-app-id").value = cfg.logtoAppId;
  $("cfg-mcp-base").value = cfg.mcpBase;
  $("cfg-logto-resource").value = cfg.logtoResource;
  $("redirect-uri").textContent = redirectUri();
  hide("signed-out");
  hide("signed-in");
  show("settings");
}

async function saveSettings() {
  await setConfig({
    logtoEndpoint: $("cfg-logto-endpoint").value.trim() || DEFAULT_CONFIG.logtoEndpoint,
    logtoAppId: $("cfg-logto-app-id").value.trim(),
    mcpBase: $("cfg-mcp-base").value.trim() || DEFAULT_CONFIG.mcpBase,
    logtoResource: $("cfg-logto-resource").value.trim() || DEFAULT_CONFIG.logtoResource,
  });
  flash("Paramètres enregistrés.");
  hide("settings");
  await renderAuth();
}

function bind() {
  $("login").addEventListener("click", async () => {
    try {
      $("login").disabled = true;
      await send("login");
      flash("Connecté.");
      await renderAuth();
    } catch (err) {
      flash(err.message, "error");
    } finally {
      $("login").disabled = false;
    }
  });

  $("logout").addEventListener("click", async () => {
    await send("logout");
    flash("Déconnecté.");
    await renderAuth();
  });

  $("sync").addEventListener("click", async () => {
    try {
      $("sync").disabled = true;
      await send("sync");
      flash("Session LinkedIn synchronisée.");
      await refreshLinkedinStatus();
    } catch (err) {
      if (err.message.includes("li_at")) {
        flash("Pas connecté à LinkedIn — connecte-toi d'abord.", "error");
      } else {
        flash(err.message, "error");
      }
    } finally {
      $("sync").disabled = false;
    }
  });

  $("clear").addEventListener("click", async () => {
    if (!confirm("Effacer ta session LinkedIn côté serveur ?")) return;
    try {
      await send("clear");
      flash("Session effacée.");
      await refreshLinkedinStatus();
    } catch (err) {
      flash(err.message, "error");
    }
  });

  $("settings-toggle").addEventListener("click", openSettings);
  $("settings-save").addEventListener("click", saveSettings);
  $("settings-close").addEventListener("click", async () => {
    hide("settings");
    await renderAuth();
  });
}

bind();
renderAuth();
