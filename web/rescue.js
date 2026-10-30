/* Rescue: find the real mail in Junk (or Trash) and move it to Rescued. */
"use strict";

let rescueScan = null;  // {accountId, server, validity, items}

function rescueFolder() {
  const folder = state.folders.find((f) => f.key === state.currentFolder);
  return folder && ["junk", "trash"].includes(folder.key) && folder.server ? folder : null;
}

function updateRescueButton() {
  // .icon-btn sets display, which would override the hidden attribute
  $("rescue-btn").style.display = !rescueFolder() || activeAccount()?.provider === "cloud" ? "none" : "";
}

function renderRescue() {
  const box = $("rescue-list");
  box.innerHTML = "";
  const groups = [["real", "Real mail"], ["maybe", "Maybe real: check before moving"]];
  for (const [verdict, title] of groups) {
    const items = rescueScan.items.filter((item) => item.verdict === verdict);
    if (!items.length) continue;
    const head = document.createElement("div");
    head.className = "rescue-group";
    head.textContent = `${title} (${items.length})`;
    box.appendChild(head);
    for (const item of items) {
      const row = document.createElement("label");
      row.className = "rescue-item";
      row.innerHTML = `<input type="checkbox">
        <div class="r-main">
          <div class="r-from"></div><div class="r-subject"></div><div class="r-why"></div>
        </div>`;
      const box_ = row.querySelector("input");
      box_.checked = item.verdict === "real";
      box_.dataset.uid = item.uid;
      box_.addEventListener("change", updateRescueGo);
      const name = shortFrom(item.sender).replace(/"/g, "").trim();
      row.querySelector(".r-from").textContent =
        name && name.toLowerCase() !== item.address ? `${name} · ${item.address}` : item.address;
      row.querySelector(".r-subject").textContent = item.subject;
      row.querySelector(".r-why").textContent = item.reason + " · " + fmtDate(item.date);
      box.appendChild(row);
    }
  }
  if (!rescueScan.items.length) {
    box.innerHTML = '<div class="empty">Nothing here looks like real mail.</div>';
  }
  updateRescueGo();
}

function rescueChosen() {
  return [...document.querySelectorAll("#rescue-list input:checked")].map((input) => input.dataset.uid);
}

function updateRescueGo() {
  const count = rescueChosen().length;
  $("rescue-go").disabled = !count;
  $("rescue-go").textContent = count ? `Move ${count} to Rescued` : "Move to Rescued";
}

async function openRescue() {
  const folder = rescueFolder();
  if (!folder || !api) return;
  const accountId = state.activeAccountId;
  rescueScan = null;
  $("rescue-title").textContent = `Real mail in ${folder.name}`;
  $("rescue-intro").textContent = "Looking through who sent each email…";
  $("rescue-list").innerHTML = '<div class="empty">Scanning…</div>';
  $("rescue-go").disabled = true;
  $("rescue-backdrop").classList.add("show");
  try {
    const res = await api.find_real_mail(accountId, folder.server);
    if (!$("rescue-backdrop").classList.contains("show") || state.activeAccountId !== accountId) return;
    rescueScan = { accountId, server: folder.server, validity: res.validity, items: res.items };
    const real = res.items.filter((item) => item.verdict === "real").length;
    $("rescue-intro").textContent = res.items.length
      ? `Found ${real} from people you know and ${res.items.length - real} that might be real. ` +
        `Ticked mail moves to the ${res.target} folder; Undo is in Activity.`
      : "";
    renderRescue();
  } catch (e) {
    $("rescue-intro").textContent = "";
    $("rescue-list").innerHTML = '<div class="empty"></div>';
    $("rescue-list").firstChild.textContent = "Scan failed: " + e;
  }
}

async function doRescue() {
  if (!rescueScan) return;
  const uids = rescueChosen();
  if (!uids.length) return;
  const scan = rescueScan;
  const trust = scan.items.filter((item) => uids.includes(item.uid)).map((item) => item.address);
  $("rescue-go").disabled = true;
  $("rescue-go").textContent = "Moving…";
  try {
    const res = await mailAction(() => api.rescue_mail(scan.accountId, scan.server, uids, scan.validity, trust));
    $("rescue-backdrop").classList.remove("show");
    const failed = res.failed.length ? `, ${res.failed.length} could not move` : "";
    toast(`Moved ${res.moved.length} to ${res.target}${failed}`, !!res.failed.length);
    if (state.activeAccountId === scan.accountId) {
      await selectAccount(scan.accountId);  // shows the new Rescued folder
    }
  } catch (e) {
    toast("Rescue failed: " + e, true);
    updateRescueGo();
  }
}

$("rescue-btn").addEventListener("click", openRescue);
$("rescue-go").addEventListener("click", doRescue);
$("rescue-cancel").addEventListener("click", () => $("rescue-backdrop").classList.remove("show"));
