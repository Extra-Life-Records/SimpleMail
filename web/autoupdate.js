/* ---------------- updates install themselves ----------------
   SimpleMail looks for a new release when it starts and every few hours. It installs it on its own
   (the app closes and reopens within seconds) once nothing is being written: no compose window or
   other dialog open, no text sitting in the box you're in, and no typing or clicking for two minutes.
   "Check for updates" in Settings still shows the release notes and asks. */

const AUTO_UPDATE_CHECK_MS = 3 * 60 * 60 * 1000;
const AUTO_UPDATE_QUIET_MS = 2 * 60 * 1000;
const AUTO_UPDATE_RETRY_MS = 60 * 1000;
// Any of these open means someone is writing or deciding something (update-backdrop: the asking dialog).
const BUSY_WINDOWS = ["compose-backdrop", "settings-backdrop", "update-backdrop", "rescue-backdrop",
                      "senders-backdrop", "agent-backdrop"];

let lastInputAt = Date.now();
let autoUpdateTimer = null;
let pendingUpdate = null;
let autoUpdating = false;

function isWritingSomething(now = Date.now()) {
  if (now - lastInputAt < AUTO_UPDATE_QUIET_MS) return true;
  if (BUSY_WINDOWS.some(id => document.getElementById(id)?.classList.contains("show"))) return true;
  const el = document.activeElement;
  if (el && (el.isContentEditable || ((el.tagName === "INPUT" || el.tagName === "TEXTAREA") && el.value))) return true;
  return false;
}

function scheduleAutoUpdate(ms) {
  clearTimeout(autoUpdateTimer);
  autoUpdateTimer = setTimeout(autoUpdateTick, ms);
}

async function autoUpdateTick() {
  if (autoUpdating) return;
  if (!pendingUpdate) {
    try {
      const res = await api.check_update();
      if (res && res.available) pendingUpdate = res;
    } catch (e) { /* offline: try again later */ }
  }
  if (!pendingUpdate) return scheduleAutoUpdate(AUTO_UPDATE_CHECK_MS);
  if (isWritingSomething()) return scheduleAutoUpdate(AUTO_UPDATE_RETRY_MS);
  autoUpdating = true;
  toast("Updating SimpleMail to " + pendingUpdate.new_version + ". It reopens in a few seconds.");
  try {
    await api.apply_update();  // the app exits and the new version starts
  } catch (e) {
    autoUpdating = false;
    pendingUpdate = null;      // look again later; a broken download must not loop every minute
    scheduleAutoUpdate(AUTO_UPDATE_CHECK_MS);
  }
}

function startAutoUpdate() {
  for (const ev of ["keydown", "mousedown", "wheel", "input", "paste"]) {
    document.addEventListener(ev, () => { lastInputAt = Date.now(); }, true);
  }
  scheduleAutoUpdate(0);
}
