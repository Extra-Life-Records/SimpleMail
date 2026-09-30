/* Lazy conversation history keeps the reader quiet until requested. */
"use strict";
let conversationView = null;

function resetConversation(accountId, uid, messageRef) {
  conversationView = {accountId, uid, messageRef, cursor:null, items:[], started:false, busy:false};
  $("conversation-history").open = false;
  $("conversation-history").hidden = !messageRef;
  $("conversation-items").textContent = "";
  $("conversation-more").hidden = true;
}

async function loadConversation() {
  const view = conversationView;
  if (!view?.messageRef || view.busy || (view.started && !view.cursor)) return;
  view.busy = true;
  $("conversation-more").disabled = true;
  if (!view.started) $("conversation-items").textContent = "Loading conversation…";
  try {
    const result = await api.get_conversation(view.accountId, view.messageRef, view.cursor);
    if (conversationView !== view || state.activeAccountId !== view.accountId || state.selectedUid !== view.uid) return;
    view.started = true;
    view.cursor = result.next_cursor;
    view.items.push(...result.items);
    const distinct = Array.from(new Map(view.items.map(item=>[item.message_ref,item])).values());
    distinct.sort((a,b)=>(Date.parse(a.date)||0)-(Date.parse(b.date)||0));
    $("conversation-items").innerHTML = distinct.length ? distinct.map(item=>
      `<article style="padding:12px;margin:8px 0"><strong>${escapeHtml(shortFrom(item.sender))}</strong>` +
      `<small> · ${escapeHtml(item.folder || "")} · ${escapeHtml(item.date || "")}</small>` +
      `<pre style="white-space:pre-wrap;font:inherit;margin:8px 0">${escapeHtml(item.text || "")}</pre>` +
      (item.truncated ? `<small>Message shortened. Open the original to read the full body.</small>` : "") + `</article>`
    ).join("") : "<p>No linked messages on this page.</p>";
    $("conversation-more").hidden = !view.cursor;
  } catch (error) {
    if (conversationView === view) {
      $("conversation-items").textContent = "Conversation could not be loaded: " + error;
      $("conversation-more").hidden = false;
    }
  } finally {
    view.busy = false;
    if (conversationView === view) $("conversation-more").disabled = false;
  }
}

$("conversation-history").addEventListener("toggle",()=>{
  if ($("conversation-history").open) loadConversation();
});
$("conversation-more").addEventListener("click",loadConversation);
