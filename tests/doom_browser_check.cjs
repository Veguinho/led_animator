// Optional real-browser regression, launched by test_doom_browser.py.
const {chromium} = require(process.env.DOOM_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');

function titlePixels(wadPath) {
  const wad = fs.readFileSync(wadPath);
  const lumps = {};
  for (let i = 0; i < wad.readInt32LE(4); i++) {
    const entry = wad.readInt32LE(8) + i * 16;
    const name = wad.toString('ascii', entry + 8, entry + 16).replace(/\0/g, '');
    lumps[name] = wad.subarray(wad.readInt32LE(entry), wad.readInt32LE(entry) + wad.readInt32LE(entry + 4));
  }
  const picture = lumps.TITLEPIC, palette = lumps.PLAYPAL;
  const width = picture.readUInt16LE(0), height = picture.readUInt16LE(2);
  const rgb = Buffer.alloc(width * height * 3);
  for (let x = 0; x < width; x++) {
    let offset = picture.readUInt32LE(8 + x * 4);
    while (picture[offset] !== 255) {
      const top = picture[offset], length = picture[offset + 1];
      for (let y = 0; y < length; y++) {
        const color = picture[offset + 3 + y];
        palette.copy(rgb, ((top + y) * width + x) * 3, color * 3, color * 3 + 3);
      }
      offset += length + 4;
    }
  }
  return {width, height, rgb: [...rgb]};
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.DOOM_CHROME, headless: true});
  try {
    const reference = titlePixels(process.argv[3]);
    for (const density of [1, 1.25, 1.5, 2, 3]) {
      const page = await browser.newPage({viewport: {width: 1200, height: 1100}, deviceScaleFactor: density});
      try {
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        let uploads = 0, payload;
        page.on('request', request => {
          if (request.url().endsWith('/api/doom/frame')) {
            uploads++;
            payload = request.postDataJSON();
          }
        });
        await page.goto(process.argv[2] + '/#doom');
        const frame = page.frameLocator('#doom-player');
        await frame.locator('#play').click();
        await frame.locator('#status').filter({hasText: 'Playing'}).waitFor();
        const compareTitle = async () => {
          const actualFrame = page.frames().find(f => f.url().includes('/doom/index.html'));
          await actualFrame.waitForFunction(expected => {
            const canvas = document.getElementById('engine');
            if (canvas.width !== expected.width || canvas.height !== expected.height) return false;
            const rgba = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            return expected.rgb.every((value, i) => Math.abs(value - rgba[Math.floor(i / 3) * 4 + i % 3]) <= 3);
          }, reference, {timeout: 8000});
        };
        // Compare every native pixel to the original WAD, not just canvas bounds.
        await compareTitle();
        assert.equal(await frame.locator('#game').evaluate(() => devicePixelRatio), density);
        assert.deepEqual(await frame.locator('#game').evaluate(c => [c.width, c.height]), [48, 48]);
        const before = uploads;
        await page.waitForTimeout(1100);
        assert.ok((uploads - before) / 1.1 > 27, '30 FPS browser uploads');
        // Verify the exact 48×48 image displayed is the image uploaded to LEDs.
        const displayed = await frame.locator('#game').evaluate(c => {
          const pixels = c.getContext('2d').getImageData(0, 0, 48, 48).data;
          return [...pixels].filter((_, i) => i % 4 !== 3);
        });
        assert.deepEqual([...Buffer.from(payload.rgb, 'base64')], displayed);
        await frame.locator('#fullscreen').click();
        await page.waitForTimeout(200);
        await compareTitle();
        assert.equal(await frame.locator('#screen').evaluate(c => document.fullscreenElement === c), true);
        const rect = await frame.locator('#game').evaluate(c => ({width: c.clientWidth, height: c.clientHeight, viewportWidth: innerWidth, viewportHeight: innerHeight}));
        assert.equal(rect.width, rect.height);
        assert.ok(rect.width <= Math.min(rect.viewportWidth, rect.viewportHeight));
        await frame.locator('#screen').evaluate(() => document.exitFullscreen());
        await page.setViewportSize({width: 390, height: 844});
        await compareTitle();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await frame.locator('#stop').click();
        assert.deepEqual(errors, []);
        console.log(`DPR ${density}: complete native title, 48×48 upload, fullscreen, resize, 30 FPS PASS`);
      } finally {
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
