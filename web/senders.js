/* Clean-up: who sends the mail in this folder, with Unsubscribe / Trash / Block. */
"use strict";

let sendersView = null;  // {accountId, server, name, groups, state, timer}

function sendersFolder() {
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  return folder && folder.server && folder.key !== "drafts" ? folder : null;
}

function updateSendersButton() {
  $("senders-btn").style.display = !sendersFolder() || activeAccount()?.provider === "cloud" ? "none" : "";
}

function sendersProgress(res) {
  if (res.state === "running") {
    const pct = res.total ? Math.floor((res.done / res.total) * 100) : 0;
    return `Scanning… ${res.done.toLocaleString()} of ${res.total.toLocaleString()} (${pct}%)`;
  }
  if (res.state === "error") return "Scan stopped: " + res.error;
  return res.total ? `${res.total.toLocaleString()} emails scanned` : "";
}

function renderSenders() {
  const view = sendersView;
  const box = $("senders-list");
  const q = ($("senders-filter").value || "").toLowerCase().trim();
  const groups = view.groups.filter((g) => !q || g.site.includes(q) || g.name.toLowerCase().includes(q)
    || g.addresses.some((a) => a.includes(q)));
  box.innerHTML = "";
  if (!groups.length) {
    box.innerHTML = `<div class="empty">${view.state === "running" ? "Reading senders…" : q ? "No matches" : "No mail"}</div>`;
    return;
  }
  for (const g of groups) {
    const row = document.createElement("div");
    row.className = "sender-row";
    row.innerHTML = `<div class="s-count"></div>
      <div class="s-main"><div class="s-name"></div><div class="s-meta"></div></div>
      <div class="s-actions"></div>`;
    row.querySelector(".s-count").textContent = g.count.toLocaleString();
    const name = row.querySelector(".s-name");
    name.textContent = g.name === g.site ? g.site : `${g.name} · ${g.site}`;
    if (g.trusted || g.random) {
      const badge = document.createElement("span");
      badge.className = "s-badge" + (g.random ? " warn" : "");
      badge.textContent = g.trusted ? "Trusted" : "Looks like spam";
      name.appendChild(badge);
    }
    row.querySelector(".s-meta").textContent =
      `${g.unread.toLocaleString()} unread · newest ${g.newest ? fmtDate(new Date(g.newest * 1000).toISOString()) : "?"}`
      + ` · ${g.addresses.join(", ")}`;
    const actions = row.querySelector(".s-actions");
    const add = (label, cls, fn) => {
      const b = document.createElement("button");
      b.className = cls; b.textContent = label;
      b.onclick = () => fn(b);
      actions.appendChild(b);
    };
    if (g.can_unsubscribe) add("Unsubscribe", "outline", (b) => unsubscribeSender(g, b));
    add("Move to Trash", "outline secondary", (b) => clearSender(g, false, b));
    if (!g.trusted) add("Block", "secondary", (b) => clearSender(g, true, b));
    box.appendChild(row);
  }
}

async function pollSenders() {
  const view = sendersView;
  if (!view || !$("senders-backdrop").classList.contains("show")) return;
  try {
    const res = await api.sender_scan_status(view.accountId, view.server);
    if (view !== sendersView) return;
    view.groups = res.groups; view.state = res.state;
    $("senders-progress").textContent = sendersProgress(res);
    renderSenders();
    if (res.state === "running") view.timer = setTimeout(pollSenders, 2500);
  } catch (e) {
    $("senders-progress").textContent = "Scan failed: " + e;
  }
}

async function openSenders() {
  const folder = sendersFolder();
  if (!folder || !api) return;
  clearTimeout(sendersView?.timer);
  sendersView = { accountId: state.activeAccountId, server: folder.server, groups: [], state: "running" };
  $("senders-title").textContent = `Who sends your ${folder.name}`;
  $("senders-filter").value = "";
  $("senders-progress").textContent = "Starting…";
  $("senders-list").innerHTML = '<div class="empty">Reading senders…</div>';
  $("senders-backdrop").classList.add("show");
  try {
    await api.start_sender_scan(sendersView.accountId, sendersView.server);
  } catch (e) {
    $("senders-progress").textContent = "Scan failed: " + e;
    return;
  }
  pollSenders();
}

async function clearSender(g, block, button) {
  const view = sendersView;
  const what = block ? `Block ${g.site}?\n\nMoves its ${g.count.toLocaleString()} emails to Trash now, and its new mail to Trash from now on.`
    : `Move ${g.count.toLocaleString()} emails from ${g.site} to Trash?`;
  if (!confirm(what)) return;
  button.disabled = true; button.textContent = "Moving…";
  try {
    const res = await mailAction(() => api.clear_sender(view.accountId, view.server, g.site, block));
    const extra = res.uncertain ? `, ${res.uncertain} need checking in Activity` : "";
    toast(`${block ? "Blocked " + g.site + ": " : ""}moved ${res.moved.toLocaleString()} to ${res.target}${extra}`, !!res.uncertain);
    if (view === sendersView) {
      view.groups = view.groups.filter((x) => x.site !== g.site);
      renderSenders();
    }
    if (state.activeAccountId === view.accountId) loadMessages({ quiet: true });
  } catch (e) {
    toast("Clean-up failed: " + e, true);
    button.disabled = false; button.textContent = block ? "Block" : "Move to Trash";
  }
}

async function unsubscribeSender(g, button) {
  const view = sendersView;
  if (!confirm(`Unsubscribe from ${g.site}?\n\nSimpleMail tells the sender you want no more emails (one-click unsubscribe).`)) return;
  button.disabled = true; button.textContent = "Unsubscribing…";
  try {
    await api.unsubscribe_sender(view.accountId, view.server, g.site);
    button.textContent = "Unsubscribed";
    toast(`Unsubscribed from ${g.site}. You can also move its old mail to Trash.`);
  } catch (e) {
    toast("Unsubscribe failed: " + e, true);
    button.disabled = false; button.textContent = "Unsubscribe";
  }
}

$("senders-btn").addEventListener("click", openSenders);
$("senders-filter").addEventListener("input", () => sendersView && renderSenders());
$("senders-close").addEventListener("click", () => {
  clearTimeout(sendersView?.timer);
  $("senders-backdrop").classList.remove("show");
});
