"""Headless Chromium rendering: cold path (v1) or persistent warm-page pool (v2 / chart_v2.html)."""
from __future__ import annotations
import asyncio, logging, time
from playwright.async_api import async_playwright

from .config import ROOT

log = logging.getLogger("signals.renderer")

# JPEG is much faster to encode than PNG and remains legible for Gemini vision.
_V2_SHOT = {"type": "jpeg", "quality": 82}


def _chart_url(payload: dict) -> str:
    name = (payload.get("style") or {}).get("template") or "chart.html"
    name = name.replace("\\", "/").split("/")[-1]
    if name not in {"chart.html", "chart_v2.html"}:
        name = "chart.html"
    return (ROOT / name).as_uri()


def _is_v2(payloads: list[dict]) -> bool:
    return bool(payloads) and all((p.get("style") or {}).get("template") == "chart_v2.html" for p in payloads)


async def _render_one_cold(browser, payload: dict) -> bytes:
    w, h = payload["style"]["width"], payload["style"]["height"]
    page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
    try:
        await page.goto(_chart_url(payload))
        await page.wait_for_function("typeof window.renderChart === 'function'")
        await page.evaluate("p => window.renderChart(p)", payload)
        await page.wait_for_function("window.__rendered === true", timeout=15000)
        return await page.locator("#capture").screenshot(type="png")
    finally:
        await page.close()


async def render_all_cold(payloads: list[dict]) -> dict[str, bytes]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            pngs = await asyncio.gather(*[_render_one_cold(browser, p) for p in payloads])
        finally:
            await browser.close()
    return {p["symbol"]: png for p, png in zip(payloads, pngs)}


class ChartPool:
    """One Chromium + one warm page per symbol, reusing setData via window.updateChart."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._pages: dict[str, object] = {}
        self._url = (ROOT / "chart_v2.html").as_uri()
        self._lock = asyncio.Lock()
        self._timings: dict[str, float] = {}

    @property
    def ready(self) -> bool:
        return self._browser is not None and bool(self._pages)

    async def start(self, symbols: list[str], width: int = 1280, height: int = 720) -> None:
        async with self._lock:
            if self._browser is not None:
                # ensure pages exist for any new symbols
                for sym in symbols:
                    if sym not in self._pages:
                        await self._new_page(sym, width, height)
                return
            t0 = time.monotonic()
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            await asyncio.gather(*[self._new_page(sym, width, height) for sym in symbols])
            log.info("chart pool warm: %d pages in %.0fms", len(self._pages), (time.monotonic() - t0) * 1000)

    async def _new_page(self, symbol: str, width: int, height: int) -> None:
        page = await self._browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        await page.goto(self._url)
        await page.wait_for_function("typeof window.renderChart === 'function' && typeof window.updateChart === 'function'")
        self._pages[symbol] = page

    async def render(self, payloads: list[dict]) -> dict[str, bytes]:
        if not payloads:
            return {}
        w = payloads[0]["style"]["width"]
        h = payloads[0]["style"]["height"]
        await self.start([p["symbol"] for p in payloads], w, h)

        async def one(p: dict) -> tuple[str, bytes, float]:
            t0 = time.monotonic()
            page = self._pages[p["symbol"]]
            # Single round-trip: update + wait for rAF render-done inside the page.
            await page.evaluate(
                """async p => {
                    window.__rendered = false;
                    if (window.__chartState) window.updateChart(p);
                    else window.renderChart(p);
                    await new Promise((resolve, reject) => {
                        const t0 = performance.now();
                        const tick = () => {
                            if (window.__rendered) return resolve();
                            if (performance.now() - t0 > 10000) return reject(new Error('render timeout'));
                            requestAnimationFrame(tick);
                        };
                        tick();
                    });
                }""",
                p,
            )
            shot = await page.locator("#capture").screenshot(
                type="jpeg", quality=78, animations="disabled", caret="hide")
            return p["symbol"], shot, (time.monotonic() - t0) * 1000

        results = await asyncio.gather(*[one(p) for p in payloads])
        out = {}
        self._timings = {}
        for sym, blob, ms in results:
            out[sym] = blob
            self._timings[sym] = round(ms, 1)
        return out

    async def close(self) -> None:
        async with self._lock:
            for page in list(self._pages.values()):
                try:
                    await page.close()
                except Exception:
                    pass
            self._pages.clear()
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None


_POOL: ChartPool | None = None


def get_pool() -> ChartPool:
    global _POOL
    if _POOL is None:
        _POOL = ChartPool()
    return _POOL


async def render_all(payloads: list[dict]) -> dict[str, bytes]:
    """v2 template -> warm pool + JPEG; otherwise cold PNG path (unchanged for live v1 loop)."""
    if _is_v2(payloads):
        pool = get_pool()
        try:
            out = await pool.render(payloads)
        except Exception as e:  # browser/page crashed (TargetClosedError etc.): rebuild the pool once
            log.warning("chart pool failed (%s: %s) - restarting Chromium", type(e).__name__, str(e)[:200])
            await pool.close()
            out = await pool.render(payloads)
        if pool._timings:
            log.info("render_per_ms %s", pool._timings)
        return out
    return await render_all_cold(payloads)
