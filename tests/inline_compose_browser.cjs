const {chromium} = require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const accounts = [
        {id: 'one', label: 'Studio', identity: 'studio@example.invalid', color: '#2563eb', signature: 'Best,<br>Alex<br><img alt="Studio logo" width="64" height="64" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=">'},
        {id: 'two', label: 'Other mailbox', identity: 'other@example.invalid', color: '#059669'},
      ];
      window.drafts = {};
      window.sent = [];
      window.failSave = false;
      window.pywebview = {api: {
        get_config: async () => ({accounts, active_account: 'one'}),
        set_active_account: async () => {}, check_update: async () => ({available: false}),
        get_folders: async () => ({folders: ['Inbox', 'Drafts', 'Sent'].map(name => ({key: name.toLowerCase(), name, server: name, unread: 0}))}),
        list_messages: async () => ({envelopes: [1, 2].map(n => ({uid: String(n), sender: 'Jamie <jamie@example.invalid>', subject: 'Project update ' + n, date: '2026-10-14', seen: true}))}),
        get_message: async (account, folder, uid) => ({subject: 'Project update ' + uid, sender: 'Jamie', to: account, date: 'Today', text: 'Original message ' + uid}),
        get_compose_context: async (account, folder, uid, all, forward) => {
          if (window.delayContext) await new Promise(resolve => {window.resolveContext = resolve;});
          return {to: 'jamie@example.invalid', cc: all ? 'team@example.invalid' : '', subject: 'Project update ' + uid,
            sender: 'Jamie', date: 'Today', text: 'Thanks for sharing your plans.\nHow can we help with the next release?',
            in_reply_to: forward ? '' : '<original@example.invalid>', references: '<thread@example.invalid>',
            attachments: forward ? [{id: 'forward-file', name: 'brief.txt', size: 50}] : []};
        },
        save_compose_draft: async (account, id, revision, to, subject, body, body_html, cc, bcc, in_reply_to, references, attachment_ids) => {
          if (window.failSave) throw Error('Disk unavailable');
          const draft = {account_id: account, id, revision: revision + 1, status: 'pending', updated_at: new Date().toISOString(),
            payload: {to, subject, body, body_html, cc, bcc, in_reply_to, references, attachment_ids},
            attachments: attachment_ids.map(id => ({id, name: 'brief.txt', size: 50}))};
          window.drafts[id] = draft;
          return draft;
        },
        get_compose_draft: async (account, id) => window.drafts[id],
        list_compose_drafts: async account => Object.values(window.drafts).filter(d => d.account_id === account && d.status === 'pending'),
        discard_compose_draft: async (account, id) => {delete window.drafts[id];},
        add_compose_attachment: async (account, name) => ({id: 'added-file', name, size: 50}),
        send_compose_draft: async (account, id) => {window.sent.push(window.drafts[id]); window.drafts[id].status = 'sent'; return {status: 'sent', sent_as: account};},
      }};
    });
    await page.goto(require('node:url').pathToFileURL(path.join(__dirname, '../web/index.html')).href);
    assert.equal(await page.title(), 'SimpleMail');
    await page.locator('.msg').first().click();
    await page.locator('#reply-btn').click();
    await page.locator('#compose-backdrop.show').waitFor();
    assert.equal(await page.locator('#compose-title').innerText(), 'Reply');
    assert.equal(await page.locator('#compose-to').inputValue(), 'jamie@example.invalid');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'compose-body');
    assert.equal(await page.locator('#read-pane > #compose-backdrop').count(), 1);
    assert.equal(await page.locator('#read-header').isVisible(), false);
    assert.equal(await page.locator('#msg-list').isVisible(), true);
    await page.keyboard.type('Hi Jamie, thanks for getting in touch.');
    let text = await page.locator('#compose-body').innerText();
    assert(text.startsWith('Hi Jamie'), text);
    assert(text.indexOf('Best,') < text.indexOf('On Today'), text);
    await page.locator('#compose-file-input').setInputFiles({name: 'brief.txt', mimeType: 'text/plain', buffer: Buffer.from('Fixture attachment')});
    await page.getByRole('button', {name: 'Remove brief.txt'}).waitFor();
    if (process.env.SIMPLEMAIL_SCREENSHOT_DIR) {
      await page.screenshot({path: path.join(process.env.SIMPLEMAIL_SCREENSHOT_DIR, 'inline-reply-desktop.png')});
    }
    const firstId = await page.evaluate(() => composeSession.id);
    await page.locator('.msg').nth(1).click();
    await page.locator('#read-subject').filter({hasText: 'Project update 2'}).waitFor();
    assert.equal(await page.locator('#compose-backdrop').isVisible(), false);
    const first = await page.evaluate(id => window.drafts[id], firstId);
    assert(first.payload.body.startsWith('Hi Jamie'));
    assert.equal(first.payload.in_reply_to, '<original@example.invalid>');
    assert.deepEqual(first.payload.attachment_ids, ['added-file']);
    await page.evaluate(id => resumeComposeDraft(id), firstId);
    assert.equal(await page.locator('#compose-body img').getAttribute('width'), '64');
    assert.equal(await page.locator('#compose-body img').getAttribute('height'), '64');
    assert.equal(await page.locator('#compose-body img').getAttribute('alt'), 'Studio logo');
    assert(await page.locator('#compose-body img').evaluate(img => img.complete && img.naturalWidth > 0));
    assert((await page.locator('#compose-body').innerText()).startsWith('Hi Jamie'));
    assert.equal(await page.getByRole('button', {name: 'Remove brief.txt'}).count(), 1);

    // A failed save must not hide or overwrite the current message.
    await page.evaluate(() => {window.failSave = true;});
    await page.locator('#compose-btn').click();
    await page.waitForFunction(() => !composeSession.busy);
    assert.equal(await page.evaluate(() => composeSession.id), firstId);
    await page.locator('#account-switch').click(); await page.locator('#accounts button').nth(1).click();
    await page.waitForFunction(() => !composeSession.busy);
    assert.equal(await page.evaluate(() => state.activeAccountId), 'one');
    assert.equal(await page.locator('#compose-backdrop').isVisible(), true);
    await page.evaluate(() => {window.failSave = false;});

    // Starting another message keeps the first as a draft, with To focused.
    await page.locator('#compose-btn').click();
    await page.waitForFunction(() => document.activeElement.id === 'compose-to');
    assert.notEqual(await page.evaluate(() => composeSession.id), firstId);
    assert.equal(await page.locator('#compose-body blockquote').count(), 0);
    await page.locator('#compose-to').fill('new@example.invalid');
    await page.locator('#compose-subject').fill('New project');
    await page.locator('#compose-body').fill('A new message');
    await page.locator('#account-switch').click(); await page.locator('#accounts button').nth(1).click();
    await page.waitForFunction(() => state.activeAccountId === 'two' && state.messages.length > 0);
    assert.equal(await page.evaluate(() => Object.values(window.drafts).find(d => d.payload.subject === 'New project').account_id), 'one');
    await page.locator('#account-switch').click(); await page.locator('#accounts button').first().click();
    await page.locator('.msg').first().click();
    await page.locator('#reply-all-btn').click();
    await page.locator('#compose-backdrop.show').waitFor();
    assert.equal(await page.locator('#compose-cc').inputValue(), 'team@example.invalid');
    await page.keyboard.type('Reply to everyone.');
    await page.locator('#send-btn').click();
    await page.locator('#compose-backdrop').waitFor({state: 'hidden'});
    assert.equal(await page.evaluate(() => window.sent.length), 1);
    assert((await page.evaluate(() => window.sent[0].payload.body)).startsWith('Reply to everyone.'));
    await page.locator('#forward-btn').click();
    await page.locator('#compose-backdrop.show').waitFor();
    assert.equal(await page.locator('#compose-title').innerText(), 'Forward');
    assert.equal(await page.locator('#compose-to').inputValue(), '');
    assert.equal(await page.getByRole('button', {name: 'Remove brief.txt'}).count(), 1);
    await page.locator('#cancel-btn').click();
    await page.locator('#compose-backdrop').waitFor({state: 'hidden'});

    // A late Reply response cannot overwrite a newer compose intent.
    await page.evaluate(() => {window.delayContext = true;});
    await page.locator('#reply-btn').click();
    await page.waitForFunction(() => Boolean(window.resolveContext));
    await page.locator('#compose-btn').click();
    await page.locator('#compose-backdrop.show').waitFor();
    await page.evaluate(() => {window.resolveContext(); window.delayContext = false;});
    assert.equal(await page.locator('#compose-title').innerText(), 'New message');
    await page.locator('#draft-btn').click();
    await page.locator('#compose-backdrop').waitFor({state: 'hidden'});
    await page.locator('#reply-btn').click();
    await page.locator('#compose-backdrop.show').waitFor();
    await page.keyboard.type('Thanks for the update.');
    await page.setViewportSize({width: 1050, height: 720});
    const editor = await page.locator('#compose-backdrop').boundingBox();
    const send = await page.locator('#send-btn').boundingBox();
    assert(editor.width > 400 && editor.x + editor.width <= 1051);
    assert(send.y >= 0 && send.y + send.height < 720);
    if (process.env.SIMPLEMAIL_SCREENSHOT_DIR) {
      await page.screenshot({path: path.join(process.env.SIMPLEMAIL_SCREENSHOT_DIR, 'inline-reply-small.png')});
    }
    await page.keyboard.press('Escape');
    await page.locator('#compose-backdrop').waitFor({state: 'hidden'});
    assert.deepEqual(errors, []);
    console.log('Inline compose: reply position, focus, attachments, safe navigation, save failure, resume, account binding, reply all, mocked send, forward, late response and compact layout passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
