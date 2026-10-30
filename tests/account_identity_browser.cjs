const {chromium} = require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.SIMPLEMAIL_CHROMIUM ? {executablePath: process.env.SIMPLEMAIL_CHROMIUM} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.ownerOpened = 0;
      let activeAccount = 'old';
      const accounts = [
        {id: 'old', label: 'hello@example.invalid', identity: 'Hello@example.invalid', email: 'hello@example.invalid', color: '#2563eb'},
        {id: 'cloud:mail-example', provider: 'cloud', label: 'hello@example.invalid', identity: 'hello@example.invalid', email: 'hello@example.invalid', color: '#2563eb'},
        {id: 'other', label: 'Other', identity: 'other@example.invalid', email: 'other@example.invalid', color: '#059669'},
      ];
      window.pywebview = {api: {
        get_config: async () => ({accounts, active_account: activeAccount, cloud_configured: true}),
        set_active_account: async id => {activeAccount = id;}, check_update: async () => ({available: false}),
        get_folders: async () => ({folders: [{key: 'inbox', name: 'Inbox', server: 'INBOX', unread: 0}]}),
        list_messages: async () => ({envelopes: []}), list_compose_drafts: async () => [],
        open_cloud_owner_settings: async () => {window.ownerOpened++; return {opened: true};},
      }};
    });
    await page.goto(require('node:url').pathToFileURL(path.join(__dirname, '../web/index.html')).href);
    await page.locator('#accounts button').nth(2).waitFor({state: 'attached'});
    assert.deepEqual(await page.locator('#accounts .acct-provider').allTextContents(), ['Existing mailbox', 'Cloud']);
    assert.equal(await page.locator('#accounts button').count(), 3);
    await page.locator('#account-switch').click(); await page.locator('#accounts button').nth(1).click();
    await page.waitForFunction(() => state.activeAccountId === 'cloud:mail-example');
    await page.locator('#settings-btn').click();
    await page.locator('#settings-backdrop.show').waitFor();
    assert.deepEqual(await page.locator('#set-account-picker option').allTextContents(), [
      'hello@example.invalid — Existing mailbox', 'hello@example.invalid — Cloud', 'Other']);
    assert.equal(await page.locator('#set-account-picker').inputValue(), '1');
    await page.locator('#cloud-owner-settings').click();
    await page.waitForFunction(() => window.ownerOpened === 1);
    assert.match(await page.locator('#toast').innerText(), /Owner settings opened/);
    if (process.env.SIMPLEMAIL_NATIVE_HEALTH_SCREENSHOT) await page.screenshot({path: process.env.SIMPLEMAIL_NATIVE_HEALTH_SCREENSHOT});
    await page.locator('#settings-cancel').click();
    await page.locator('#account-switch').click(); await page.locator('#accounts button').first().click();
    await page.waitForFunction(() => state.activeAccountId === 'old');
    assert.equal(await page.locator('#accounts button').count(), 3);
    assert.deepEqual(errors, []);
    console.log('Native account identity: duplicate-provider badges, both account selections preserved, settings picker and fixed owner link passed');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
