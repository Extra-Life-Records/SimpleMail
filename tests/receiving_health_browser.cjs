const {chromium} = require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const assets = ['ai.html', 'ai.js', 'health.js', 'style.css'];
  const server = http.createServer((req, res) => {
    const name = req.url.split('?')[0].split('/').pop();
    if (!assets.includes(name)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.resolve('cloud/src/static', name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, ...(process.env.SIMPLEMAIL_CHROMIUM ? {executablePath: process.env.SIMPLEMAIL_CHROMIUM} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    const pending = {status: 'pending', checked_at: new Date().toISOString(), expires_after_seconds: 600, delivery_verified: false, checks: [
      {kind: 'queue', name: 'Receiver failures', status: 'clear', counts: {visible: 0, in_flight: 0, delayed: 0}},
      {kind: 'domain', name: 'example.com', status: 'pending', verification: 'PENDING', dkim: 'PENDING'},
      {kind: 'rules', name: 'Receiving routes', status: 'pending', rules: [{name: 'Existing AWS inboxes', enabled: true, status: 'clear'}, {name: 'Migrated mailboxes', enabled: false, status: 'pending'}], migration_notice: 'Migration receiving is not activated. Imported history does not confirm new mail delivery.'},
      {kind: 'sending', name: 'Sending', status: 'clear', flags: {general: 'disabled', manual: 'disabled', ai: 'disabled', ai_transport: 'disabled'}}]};
    let value = pending, status = 200;
    await page.addInitScript(() => sessionStorage.setItem('refresh', 'fixture-only'));
    await page.route('**/config.json', route => route.fulfill({json: {auth_url: 'https://fixture.amazoncognito.com', client_id: 'owner'}}));
    await page.route('https://fixture.amazoncognito.com/oauth2/token', route => route.fulfill({json: {access_token: 'fixture-only', expires_in: 900}}));
    await page.route('**/ai/*', route => {
      requests.push({method: route.request().method(), path: new URL(route.request().url()).pathname});
      const suffix = new URL(route.request().url()).pathname.split('/').pop();
      route.fulfill({json: suffix === 'connections' ? {connections: [], client_ids: []} : suffix === 'mailboxes' ? {mailboxes: []} : {drafts: []}});
    });
    await page.route('**/mailboxes/operational-health', route => {
      requests.push({method: route.request().method(), path: new URL(route.request().url()).pathname});
      route.fulfill({status, json: value});
    });
    await page.goto('http://127.0.0.1:' + server.address().port + '/ui/ai.html');
    await page.getByText('Receiving setup is pending', {exact: true}).waitFor();
    await page.getByText(pending.checks[2].migration_notice, {exact: true}).waitFor();
    assert.match(await page.locator('#health-details').innerText(), /Sending: Manual: disabled; AI: disabled/);
    if (process.env.SIMPLEMAIL_HEALTH_SCREENSHOT) await page.screenshot({path: process.env.SIMPLEMAIL_HEALTH_SCREENSHOT, fullPage: true});
    value = {...pending, status: 'attention', checks: [{kind: 'queue', name: '<img src=x onerror=alert(1)>', status: 'attention', counts: {visible: 1, in_flight: 0, delayed: 0}}]};
    await page.locator('#health-refresh').click(); await page.getByText('Receiving needs attention', {exact: true}).waitFor();
    assert.equal(await page.locator('#receiving-health img').count(), 0);
    value = {...pending, checked_at: new Date(Date.now() - 660000).toISOString()};
    await page.locator('#health-refresh').click(); await page.getByText('Receiving health is out of date', {exact: true}).waitFor();
    status = 503; value = {error: 'private provider details'};
    await page.locator('#health-refresh').click(); await page.getByText('Receiving health is unavailable', {exact: true}).waitFor();
    assert.equal(await page.locator('#health-details').innerText(), '');
    assert.doesNotMatch(await page.locator('#receiving-health').innerText(), /private provider details/);
    status = 403; await page.locator('#health-refresh').click(); await page.locator('#receiving-health').waitFor({state: 'hidden'});
    assert(requests.every(request => request.method === 'GET'));
    assert.deepEqual(errors, []);
    console.log('Receiving health browser: pending migration, failure, stale, unavailable, owner denial, safe rendering and GET-only checks passed');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
