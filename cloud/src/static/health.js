'use strict';
window.SimpleMailReceivingHealth = (() => {
  const labels = {clear: 'Configuration checks passed', pending: 'Receiving setup is pending',
    attention: 'Receiving needs attention', unknown: 'Receiving health is unavailable', stale: 'Receiving health is out of date'};
  function mount(request) {
    const section = document.getElementById('receiving-health');
    const summary = document.getElementById('health-summary');
    const details = document.getElementById('health-details');
    const checked = document.getElementById('health-checked');
    const refresh = document.getElementById('health-refresh');
    let snapshot = null, busy = false;
    const node = (tag, text) => { const item = document.createElement(tag); item.textContent = text; return item; };
    function render() {
      const age = snapshot ? Date.now() - Date.parse(snapshot.checked_at) : NaN;
      const stale = snapshot && (!Number.isFinite(age) || age > 600000 || age < -60000);
      const state = stale ? 'stale' : (snapshot?.status || 'unknown');
      summary.textContent = labels[state] || labels.unknown;
      section.dataset.state = state;
      checked.textContent = snapshot && Number.isFinite(age) ? 'Checked ' + new Date(snapshot.checked_at).toLocaleString() : 'No current check is available.';
      details.replaceChildren();
      if (!snapshot) return;
      if (snapshot.reason) details.append(node('p', snapshot.reason));
      for (const check of snapshot.checks || []) {
        let value = check.status;
        if (check.kind === 'queue' && check.counts) value = `${check.counts.visible} waiting, ${check.counts.in_flight} in progress, ${check.counts.delayed} delayed`;
        if (check.kind === 'alarm' && check.state) value = check.state;
        if (check.kind === 'domain' && check.verification) value = `${check.verification} · DKIM ${check.dkim || 'unknown'}`;
        if (check.kind === 'rules' && check.rules) value = check.rules.map(r => `${r.name}: ${r.enabled === true ? 'enabled' : r.enabled === false ? 'disabled' : 'unknown'} (${r.status})`).join('; ');
        if (check.kind === 'sending' && check.flags) {
          const combine = flags => flags.includes('unknown') || flags.some(v => !v) ? 'unknown' : flags.every(v => v === 'enabled') ? 'enabled' : 'disabled';
          value = `Manual: ${check.flags.manual || 'unknown'}; AI: ${combine([check.flags.ai, check.flags.ai_transport])}; other mailbox sending: ${check.flags.general || 'unknown'}`;
          if (Object.values(check.flags).some(v => v === 'enabled')) value += ' — a sending control is enabled; review required';
        }
        if (check.detail) value = check.detail;
        const row = node('li', ''); row.append(node('strong', check.name + ': '), node('span', value)); details.append(row);
        if (check.migration_notice) details.append(node('p', check.migration_notice));
      }
    }
    async function update() {
      if (busy) return;
      busy = true; refresh.disabled = true;
      try {
        snapshot = await request('GET', '/mailboxes/operational-health'); section.hidden = false;
      } catch (error) {
        snapshot = null;
        // No operational details or polling for a non-owner/AI session.
        section.hidden = error.status === 403;
      } finally { busy = false; refresh.disabled = false; render(); }
    }
    refresh.onclick = update;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible' && !section.hidden) update();
    }, 300000);
    const freshness = setInterval(render, 30000);
    document.addEventListener('visibilitychange', render);
    window.addEventListener('pagehide', () => { clearInterval(timer); clearInterval(freshness); }, {once: true});
    update();
    return {refresh: update};
  }
  return {mount};
})();
