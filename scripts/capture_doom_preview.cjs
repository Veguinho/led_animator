// Capture the real browser engine's 48×48 canvas on an isolated, board-free server.
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.DOOM_PLAYWRIGHT_MODULE ||
  path.resolve(__dirname, '../.build/ui-check/node_modules/playwright-core'));

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.DOOM_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.goto(process.argv[2] + '/#doom');
    const frame = page.frameLocator('#doom-player');
    await frame.locator('#play').click();
    await frame.locator('#status').filter({hasText: 'Playing'}).waitFor({timeout: 30000});
    await frame.locator('#game').click();
    for (let i = 0; i < 3; i++) {
      await page.keyboard.press('Enter');
      await page.waitForTimeout(400);
    }
    await page.waitForTimeout(1200);
    const fps = Number(process.argv[4]);
    const count = fps * Number(process.argv[5]);
    const frames = [];
    await page.keyboard.down('ArrowUp');
    await page.keyboard.down('Control');
    const started = Date.now();
    for (let i = 0; i < count; i++) {
      frames.push(await frame.locator('#game').evaluate(canvas =>
        [...canvas.getContext('2d').getImageData(0, 0, 48, 48).data]
          .filter((_, index) => index % 4 !== 3)));
      if (i === Math.floor(count / 2)) await page.keyboard.down('ArrowRight');
      await page.waitForTimeout(Math.max(0, started + (i + 1) * 1000 / fps - Date.now()));
    }
    fs.writeFileSync(process.argv[3], JSON.stringify(frames));
    await frame.locator('#stop').click();
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
