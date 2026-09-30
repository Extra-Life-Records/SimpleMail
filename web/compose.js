/* Local recovery snapshots protect typing while durable saves are in flight. */
"use strict";
let composeSession = null;
let composeSaveTimer = null;

function setComposeBusy(session, busy) {
  session.busy = busy;
  document.querySelectorAll("#compose-backdrop button,#compose-backdrop input").forEach(control => { control.disabled = busy; });
  $("compose-body").contentEditable = busy ? "false" : "true";
}

function startComposeSession(accountId, draft = null) {
  clearTimeout(composeSaveTimer);
  composeSession = {id: draft?.id || crypto.randomUUID(), accountId, revision: draft?.revision || 0,
    generation: 0, queue: Promise.resolve(), busy: false, payload: null,
    inReplyTo: draft?.payload?.in_reply_to || "", references: draft?.payload?.references || "",
    attachments: draft?.attachments || []};
  $("compose-save-status").textContent = "Saving on this device…";
}

function recoveryKey(session) { return "simplemail-compose:" + session.accountId + ":" + session.id; }

function composeSnapshot() {
  return {to: $("compose-to").value, subject: $("compose-subject").value,
    body: composeText(), body_html: composeHtml(), cc: $("compose-cc").value, bcc: $("compose-bcc").value,
    in_reply_to: composeSession.inReplyTo, references: composeSession.references,
    attachment_ids: composeSession.attachments.map(item => item.id)};
}

function composeChanged() {
  const session = composeSession;
  if (!session || session.busy) return;
  session.payload = composeSnapshot();
  session.generation++;
  backupCompose(session);
  $("compose-save-status").textContent = "Saving on this device…";
  clearTimeout(composeSaveTimer);
  composeSaveTimer = setTimeout(() => persistCompose().catch(() => {}), 600);
}

function backupCompose(session) {
  try {
    localStorage.setItem(recoveryKey(session), JSON.stringify({id: session.id, account_id: session.accountId,
      revision: session.revision, payload: session.payload, attachments: session.attachments,
      updated_at: new Date().toISOString(), status: "pending"}));
  } catch {
    $("compose-save-status").textContent = "Recovery storage unavailable; save before closing";
  }
}

function clearComposeRecovery(session) {
  clearTimeout(composeSaveTimer);
  try { localStorage.removeItem(recoveryKey(session)); } catch {}
}

async function persistCompose() {
  clearTimeout(composeSaveTimer);
  const session = composeSession;
  if (!session) throw Error("No draft to save");
  const payload = composeSnapshot();
  session.payload = payload;
  const generation = session.generation;
  backupCompose(session);
  session.queue = session.queue.catch(() => {}).then(async () => {
    const draft = await api.save_compose_draft(session.accountId, session.id, session.revision,
      payload.to, payload.subject, payload.body, payload.body_html, payload.cc, payload.bcc,
      payload.in_reply_to, payload.references, payload.attachment_ids);
    session.revision = draft.revision;
    if (session.generation === generation) session.payload = payload;
    backupCompose(session);
    if (composeSession === session && session.generation === generation) {
      $("compose-save-status").textContent = "Saved on this device";
    }
    return draft;
  });
  try { return await session.queue; }
  catch (error) {
    if (composeSession === session) $("compose-save-status").textContent = "Not saved: " + error;
    throw error;
  }
}

async function closeCompose() {
  if (!composeSession || composeSession.busy) return;
  const session = composeSession;
  setComposeBusy(session, true);
  try {
    await persistCompose();
    $("compose-backdrop").classList.remove("show");
    if (state.currentFolder === "drafts") loadMessages();
  } catch (error) { toast("Keep this draft open until saving succeeds: " + error, true); }
  finally { setComposeBusy(session, false); }
}

async function discardCompose() {
  const session = composeSession;
  if (!session || session.busy) return;
  setComposeBusy(session, true);
  try {
    await persistCompose();
    await api.discard_compose_draft(session.accountId, session.id, session.revision);
    clearComposeRecovery(session);
    $("compose-backdrop").classList.remove("show");
    if (state.currentFolder === "drafts") loadMessages();
  } catch (error) { toast("Could not discard draft: " + error, true); }
  finally { setComposeBusy(session, false); }
}

function composeRecoveryDrafts(accountId, drafts) {
  const merged = new Map(drafts.map(draft => [draft.id, draft]));
  try {
    for (let n = 0; n < localStorage.length; n++) {
      const key = localStorage.key(n);
      if (!key.startsWith("simplemail-compose:" + accountId + ":")) continue;
      const cached = JSON.parse(localStorage.getItem(key));
      if (cached.account_id !== accountId || !cached.payload || typeof cached.payload.body !== "string") continue;
      if (!merged.has(cached.id)) merged.set(cached.id, cached);
    }
  } catch {}
  return Array.from(merged.values()).sort((a,b) => b.updated_at.localeCompare(a.updated_at));
}

async function resumeComposeDraft(draftId) {
  const accountId = state.activeAccountId;
  const account = activeAccount();
  let draft;
  let cached;
  try { cached = JSON.parse(localStorage.getItem(recoveryKey({accountId, id:draftId})) || "null"); } catch {}
  try { draft = await api.get_compose_draft(accountId, draftId); }
  catch (error) { if (cached?.account_id === accountId && cached.revision === 0) draft = cached; else { toast(String(error), true); return; } }
  if (accountId !== state.activeAccountId) return;
  if (draft.status !== "pending") {
    if (draft.status === "sent") clearComposeRecovery({accountId,id:draftId});
    toast(draft.status === "sent" ? "This draft was already sent" : "Delivery needs checking. Check Sent before creating another message.", true);
    return;
  }
  if (cached?.account_id === accountId && Date.parse(cached.updated_at) > Date.parse(draft.updated_at) &&
      (cached.revision === draft.revision || cached.revision + 1 === draft.revision)) {
    draft.payload = cached.payload;
    draft.attachments = cached.attachments || draft.attachments;
  }
  composeAccountId = accountId;
  startComposeSession(accountId, draft);
  $("compose-from").textContent = `${account.label} <${account.identity}>`;
  $("compose-to").value = draft.payload.to;
  $("compose-subject").value = draft.payload.subject;
  $("compose-cc").value = draft.payload.cc || "";
  $("compose-bcc").value = draft.payload.bcc || "";
  $("compose-copies").open = !!(draft.payload.cc || draft.payload.bcc);
  renderComposeAttachments();
  // Restore only inert formatting; never insert stored executable markup.
  const parsed = new DOMParser().parseFromString(draft.payload.body_html || "", "text/html");
  const allowed = new Set(["DIV","P","BR","SPAN","B","STRONG","I","EM","U","S","BLOCKQUOTE","PRE","UL","OL","LI","A","IMG","TABLE","TR","TD","TH","TBODY","THEAD"]);
  parsed.body.querySelectorAll("*").forEach(element => {
    if (!allowed.has(element.tagName)) { element.replaceWith(...element.childNodes); return; }
    const image = element.tagName === "IMG" ? element.getAttribute("src") : null;
    const link = element.tagName === "A" ? safeEmailLink(element.getAttribute("href")) : null;
    for (const attr of Array.from(element.attributes)) element.removeAttribute(attr.name);
    if (image && /^data:image\/(png|gif|jpeg|webp);base64,/i.test(image)) element.setAttribute("src", image);
    if (link) { element.setAttribute("href", link); element.setAttribute("rel", "noopener noreferrer"); }
  });
  $("compose-body").innerHTML = parsed.body.innerHTML || escapeHtml(draft.payload.body);
  $("compose-backdrop").classList.add("show");
  $("compose-body").focus();
  composeChanged();
}

function renderComposeAttachments() {
  const box = $("compose-attachments");
  box.innerHTML = (composeSession?.attachments || []).map((item, index) =>
    `<div class="att"><span class="name">${escapeHtml(item.name)} · ${fmtSize(item.size)}</span>` +
    `<button class="outline secondary" data-remove-attachment="${index}" aria-label="Remove ${escapeHtml(item.name)}">Remove</button></div>`).join("");
  box.querySelectorAll("[data-remove-attachment]").forEach(button => {
    button.onclick = () => {
      if (composeSession.busy) return;
      composeSession.attachments.splice(Number(button.dataset.removeAttachment), 1);
      renderComposeAttachments(); composeChanged();
    };
  });
}

async function addComposeFiles(files) {
  const session = composeSession;
  if (!session || session.busy) return;
  setComposeBusy(session, true);
  try {
    const incoming = Array.from(files);
    if (session.attachments.length + incoming.length > 20 || incoming.some(file => file.size > 10 * 1024 * 1024) ||
      incoming.reduce((sum, file) => sum + file.size, 0) + session.attachments.reduce((sum, item) => sum + item.size, 0) > 20 * 1024 * 1024) {
      throw Error("Select up to 20 files, at most 10 MB each and 20 MB together");
    }
    for (const file of incoming) {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = () => reject(Error("Could not read " + file.name));
        reader.readAsDataURL(file);
      });
      session.attachments.push(await api.add_compose_attachment(session.accountId, file.name, encoded));
    }
  } catch (error) { toast(String(error), true); }
  finally {
    setComposeBusy(session, false); renderComposeAttachments(); composeChanged();
    $("compose-file-input").value = "";
  }
}

["compose-to", "compose-cc", "compose-bcc", "compose-subject", "compose-body"].forEach(id => $(id).addEventListener("input", composeChanged));
$("compose-attach").addEventListener("click", () => $("compose-file-input").click());
$("compose-file-input").addEventListener("change", event => addComposeFiles(event.target.files));
