"""Rasterise the existing SVG for Android installation; requires Playwright."""
from pathlib import Path


def main() -> None:
    from playwright.sync_api import Error, sync_playwright

    assets = Path(__file__).resolve().parent
    svg = (assets / "solar_power.svg").read_text(encoding="utf-8")
    with sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(headless=True)
        except Error:
            browser = driver.chromium.launch(channel="msedge", headless=True)
        try:
            for size in (192, 512):
                page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
                page.set_content(f'<body style="margin:0;display:grid;place-items:center;width:100vw;height:100vh"><div style="width:80%;height:80%">{svg}</div></body>')
                page.screenshot(path=str(assets / f"solar-app-{size}.png"), omit_background=True)
                page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
