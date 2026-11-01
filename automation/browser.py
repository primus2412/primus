"""
automation/browser.py

Browser automation module for PRIMUS.
Uses Playwright for web automation with RBAC-enforced permission checks.
Playwright must be installed separately: pip install playwright && playwright install chromium
"""
from loguru import logger
from profiles.rbac import rbac
from audit.trail import audit

# Playwright is an optional dependency — lazy-imported to avoid startup crash
_playwright_available = False

try:
    from playwright.async_api import async_playwright
    _playwright_available = True
except ImportError:
    logger.warning("Playwright not installed. Browser automation unavailable. "
                   "Install with: pip install playwright && playwright install chromium")


class BrowserAgent:
    """
    Playwright-based browser automation with RBAC permission enforcement.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.is_active = False

    async def start(self):
        """Start the Playwright browser (Chromium, headless)."""
        if not _playwright_available:
            logger.error("Cannot start browser: Playwright not installed.")
            return

        rbac.validate_permission("BrowserAgent", "web_browse")
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()
            self.is_active = True
            logger.info("Browser agent started.")
            audit.log_event("BROWSER_STARTED", {})
        except Exception as e:
            logger.error(f"Browser startup failed: {e}")
            raise

    async def stop(self):
        """Stop the Playwright browser and release resources."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.error(f"Browser stop error: {e}")
        finally:
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None
            self.is_active = False
            logger.info("Browser agent stopped.")
            audit.log_event("BROWSER_STOPPED", {})

    def _is_blocked_url(self, url: str) -> bool:
        """Check if URL is blocked (local network, metadata services, etc.)."""
        blocked_prefixes = [
            "file://", "localhost", "127.0.0.1", "0.0.0.0",
            "[::1]", "169.254.",  # link-local / AWS metadata
        ]
        url_lower = url.lower()
        return any(prefix in url_lower for prefix in blocked_prefixes)

    async def navigate_and_read(self, url: str) -> str:
        """Navigate to a URL and return the page HTML content."""
        if not _playwright_available:
            return "Browser automation unavailable — Playwright not installed."

        if self._is_blocked_url(url):
            logger.warning(f"Blocked attempt to access local URL: {url}")
            audit.log_event("BROWSER_BLOCKED", {"url": url, "reason": "local_network"})
            return "Access to local network addresses is blocked."

        rbac.validate_permission("BrowserAgent", "web_browse")

        if not self._page:
            logger.error("Browser not started. Call start() first.")
            return "Browser not active."

        try:
            await self._page.goto(url, timeout=30000)
            content = await self._page.content()
            # Limit response size for HUD display
            return content[:10000] if len(content) > 10000 else content
        except Exception as e:
            logger.error(f"Browser navigation failed: {e}")
            return f"Navigation error: {e}"

    async def search_web(self, query: str) -> str:
        """Perform a web search using the default search engine."""
        rbac.validate_permission("BrowserAgent", "web_search")
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        return await self.navigate_and_read(search_url)
