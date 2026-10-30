// Exercise proposal follow-ups and dismissal against the isolated HUD preview.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const args = process.argv.slice(2);
  const url = args[args.indexOf('--url') + 1];
  const evidence = args[args.indexOf('--evidence') + 1];
  if (!args.includes('--url') || !args.includes('--evidence') || !url || !evidence) {
    throw new Error('Required: --url <isolated preview origin> --evidence <report directory>');
  }
  fs.mkdirSync(evidence, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    assert.equal((await page.request.post(url + '/hud-preview/reset')).ok(), true);
    await page.goto(url + '/app/');
    await page.locator('.hud-question').waitFor({ state: 'attached' });
    await page.locator('#hud-todos-tab').click();
    await page.locator('.hud-option').first().click();
    await page.getByRole('button', { name: '确认保存项目备注', exact: true }).click();
    await page.locator('.hud-proposal').waitFor({ state: 'detached' });
    await page.locator('#hud-input').fill('把简介改成用于测试的简介');
    await page.locator('#hud-send').click();
    await page.locator('.hud-proposal').waitFor();
    const index = await (await page.request.get(url + '/hud')).json();
    const sid = index.sessions.find(item => item.project === 'hud-preview').id;
    const getSession = async () => (await page.request.get(url + '/hud/' + sid)).json();
    const before = await getSession();
    await page.locator('#hud-input').fill('先解释这条提案的作用');
    await page.locator('#hud-send').click();
    await page.waitForFunction(() => document.querySelector('.hud-message-assistant:last-child')
      ?.textContent.includes('接下来想完善哪一部分'));
    const explained = await getSession();
    assert.deepEqual(explained.state.proposal, before.state.proposal);
    assert.equal(explained.status, 'waiting');
    await page.reload();
    if(await page.locator('#hud-toggle').getAttribute('aria-expanded')!=='true')await page.locator('#hud-toggle').click();
    await page.locator('.hud-proposal').waitFor({state:'attached'});
    await page.locator('#hud-todos-tab').click();
    await page.locator('.hud-proposal').waitFor();
    assert.equal((await getSession()).state.proposal.id, before.state.proposal.id);
    await page.screenshot({ path: path.join(evidence, 'pending-proposal-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(evidence, 'pending-proposal-mobile.png') });
    const requestPromise = page.waitForRequest(request => request.method() === 'POST'
      && request.url().endsWith('/hud/' + sid + '/answer'));
    await page.getByRole('button', { name: '暂不处理', exact: true }).click();
    assert.equal((await requestPromise).postDataJSON().dismiss_proposal, before.state.proposal.id);
    await page.locator('.hud-proposal').waitFor({ state: 'detached' });
    const dismissed = await getSession();
    assert.equal(dismissed.state.proposal, undefined);
    assert.equal(dismissed.generation, before.generation);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(evidence, 'browser.json'), JSON.stringify({
      passed: true, checks: ['followup retains proposal', 'reload retains proposal',
        'desktop and mobile', 'dismiss sends current proposal ID', 'dismiss releases proposal'], errors,
    }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
