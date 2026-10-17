"""Optional Chromium regression for SDL cropping on Retina / zoomed displays."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from audio_palette_controls import PaletteControls, PaletteServer


ROOT = Path(__file__).resolve().parents[1]
PLAYWRIGHT = Path(os.environ.get(
    "DOOM_PLAYWRIGHT_MODULE", ROOT / ".build/ui-check/node_modules/playwright-core"))
CHROME = os.environ.get(
    "DOOM_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@unittest.skipUnless(shutil.which("node") and PLAYWRIGHT.exists() and Path(CHROME).exists(),
                     "requires Node, playwright-core and Chrome; see doom_game/README.md")
class DoomBrowserTests(unittest.TestCase):
    def test_complete_frame_at_normal_fractional_and_retina_densities(self):
        server = PaletteServer(PaletteControls(size=48), 0, video_library={})
        server.start()
        self.addCleanup(server.close)
        result = subprocess.run(
            ["node", str(ROOT / "tests/doom_browser_check.cjs"), server.url,
             str(ROOT / "doom_game/assets/freedoom2.wad")],
            env=os.environ | {"DOOM_PLAYWRIGHT_MODULE": str(PLAYWRIGHT.resolve()),
                              "DOOM_CHROME": CHROME},
            capture_output=True, text=True, timeout=90, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
