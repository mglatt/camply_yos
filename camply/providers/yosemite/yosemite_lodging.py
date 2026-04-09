"""
Yosemite National Park Lodging Provider

Uses the Aramark/AHLS booking system at reservations.ahlsmsworld.com
to check availability for Yosemite lodging properties.

Automates the search page via Playwright — selects properties from the
dropdown, intercepts the outgoing GetInventoryCountData API request,
rewrites the StartDate parameter to the target month, and captures the
response.  The page handles reCAPTCHA Enterprise internally.

Requires Playwright:
    pip install playwright && python -m playwright install chromium
"""

import json
import logging
import os
import re
import time
from calendar import monthrange
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote_plus, urlparse

from camply.config.api_config import YosemiteConfig
from camply.containers import AvailableCampsite, CampgroundFacility, RecreationArea
from camply.providers.base_provider import BaseProvider
from camply.utils import logging_utils

logger = logging.getLogger(__name__)


class YosemiteLodging(BaseProvider):
    """
    Scanner for Lodging in Yosemite National Park

    Searches the Aramark/AHLS reservation system for availability
    at Yosemite lodging properties (Curry Village, Housekeeping Camp,
    The Ahwahnee, Tuolumne Meadows Lodge, Yosemite Valley Lodge).

    Uses Playwright to automate the search page UI and intercept
    API responses, letting the page handle reCAPTCHA Enterprise natively.
    """

    recreation_area = RecreationArea(
        recreation_area=YosemiteConfig.YOSEMITE_RECREATION_AREA_FULL_NAME,
        recreation_area_id=YosemiteConfig.YOSEMITE_RECREATION_AREA_ID,
        recreation_area_location="USA",
    )

    def __init__(self):
        super().__init__()
        self._playwright = None
        self._browser = None
        self._page = None
        self._browser_ready = False

    def _ensure_browser(self) -> None:
        """
        Launch headless browser and navigate to the search page.
        """
        if self._page is not None and self._browser_ready:
            return
        if self._page is not None:
            self._close_browser()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is required for the YosemiteLodging provider. "
                "Install it with: pip install playwright && "
                "python -m playwright install chromium"
            )
        logger.info("Launching headless browser for Yosemite search...")
        self._playwright = sync_playwright().start()
        # Configure proxy if present in environment — Chromium needs
        # explicit proxy auth, it won't parse user:pass from env vars.
        launch_kwargs = dict(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy_url:
            parsed = urlparse(proxy_url)
            proxy_cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_cfg["username"] = parsed.username
            if parsed.password:
                proxy_cfg["password"] = parsed.password
            launch_kwargs["proxy"] = proxy_cfg
            logger.debug("Using proxy: %s:%s", parsed.hostname, parsed.port)
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        context = self._browser.new_context(
            user_agent=self.session.headers.get("User-Agent", ""),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id=YosemiteConfig.YOSEMITE_TIMEZONE,
            # Accept proxy CA certs when behind an intercepting proxy
            ignore_https_errors=bool(proxy_url),
        )
        self._page = context.new_page()
        # Basic stealth: hide common automation indicators.
        self._page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            window.chrome = { runtime: {} };
            """
        )
        # Optional enhanced stealth via playwright-stealth package.
        try:
            from playwright_stealth import stealth_sync

            stealth_sync(self._page)
            logger.debug("playwright-stealth applied")
        except ImportError:
            pass
        # Capture the widget config response during page load — it may
        # contain room type definitions we need for per-type results.
        self._widget_config = None

        def _capture_config(response):
            if "GetWidgetConfigData" in response.url and response.status == 200:
                try:
                    text = response.text()
                    self._widget_config = self._parse_jsonp(text)
                    logger.debug(
                        "Captured widget config (%s chars)",
                        len(text),
                    )
                except Exception as exc:
                    logger.debug("Could not parse widget config: %s", exc)

        self._page.on("response", _capture_config)
        self._page.goto(YosemiteConfig.SEARCH_PAGE_URL, wait_until="networkidle")
        # Wait for the visible search widget to be ready
        self._page.wait_for_selector(
            "#box-widget_InitialProductSelection", timeout=30000
        )
        if self._widget_config is not None:
            logger.debug("Widget config captured successfully")
        else:
            logger.debug("No widget config captured during page load")
        logger.info("Browser ready - search page loaded.")
        self._browser_ready = True

    def _close_browser(self) -> None:
        """Clean up browser resources."""
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None
        self._browser_ready = False

    def __del__(self):
        self._close_browser()

    @staticmethod
    def _parse_jsonp(text: str) -> object:
        """
        Strip JSONP callback wrapper and parse the JSON payload.
        """
        match = re.match(r"^[^(\[{]+\((.+)\);?\s*$", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        return json.loads(text)

    def _needs_date_rewrite(self, month: int, year: int) -> bool:
        """
        Check whether the target month falls outside the widget's natural
        3-month window.  The widget defaults to a window starting from
        the current calendar month, so searches within ~3 months of today
        don't need date rewriting.
        """
        now = datetime.now()
        natural_end_month = now.month + 2
        natural_end_year = now.year
        if natural_end_month > 12:
            natural_end_month -= 12
            natural_end_year += 1
        return (year, month) > (natural_end_year, natural_end_month)

    def _select_property(self, initial_value: str) -> None:
        """
        Select a property from the dropdown, resetting first if
        the same value is already selected (to ensure a change event).
        """
        current = self._page.evaluate(
            "document.querySelector('#box-widget_InitialProductSelection').value"
        )
        if current == initial_value:
            # Reset to first option to guarantee a change event fires
            self._page.select_option(
                "#box-widget_InitialProductSelection", index=0
            )
            self._page.wait_for_timeout(500)
        self._page.select_option(
            "#box-widget_InitialProductSelection", value=initial_value
        )

    def _get_inventory_count(
        self,
        multiprop_code: str,
        month: int,
        year: int,
    ) -> list:
        """
        Get inventory count by selecting a property from the dropdown
        and intercepting the GetInventoryCountData API response.

        Strategy for reliability:
        1. First attempt uses the existing page (no reload) — avoids
           burning an extra reCAPTCHA token.
        2. If the target month is within the widget's natural 3-month
           window, skip date rewriting entirely (passthrough mode).
           This keeps the request identical to what a real user would
           generate, maximising reCAPTCHA acceptance.
        3. Only rewrite dates when the target month is outside the
           natural window.
        4. On failure, reload with exponential backoff so reCAPTCHA
           Enterprise can reset its risk score.
        """
        self._ensure_browser()

        max_attempts = 5
        initial_value = (
            f"{YosemiteConfig.YOSEMITE_RECREATION_AREA_ID}:{multiprop_code}"
        )
        need_rewrite = self._needs_date_rewrite(month, year)

        # Build rewrite targets only when needed
        target_start_str = None
        target_end_str = None
        if need_rewrite:
            target_start_str = self._page.evaluate(
                f"new Date({year}, {month - 1}, 1).toDateString()"
            )
            end_month = month + 2
            end_year = year
            if end_month > 12:
                end_month -= 12
                end_year += 1
            _, end_last_day = monthrange(end_year, end_month)
            target_end_str = self._page.evaluate(
                f"new Date({end_year}, {end_month - 1}, {end_last_day}).toDateString()"
            )
            logger.debug(
                "Date rewrite: %s -> %s (target outside natural window)",
                target_start_str,
                target_end_str,
            )
        else:
            logger.debug(
                "Passthrough mode for %s/%s (within natural window)",
                month,
                year,
            )

        for attempt in range(1, max_attempts + 1):
            # First attempt: reuse existing page when the dropdown is
            # still visible.  After a previous property selection the
            # widget hides the dropdown, so we must reload to reset it.
            # Retries always reload with exponential backoff.
            need_reload = attempt > 1
            if attempt == 1:
                dropdown = self._page.query_selector(
                    "#box-widget_InitialProductSelection"
                )
                if dropdown is None or not dropdown.is_visible():
                    need_reload = True
                    logger.debug(
                        "Dropdown hidden (previous selection), reloading"
                    )

            if need_reload:
                if attempt > 1:
                    backoff = 3 * (2 ** (attempt - 2))  # 3, 6, 12, 24s
                    logger.debug(
                        "Waiting %ds before retry (attempt %d/%d)...",
                        backoff,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(backoff)
                self._page.goto(
                    YosemiteConfig.SEARCH_PAGE_URL,
                    wait_until="networkidle",
                )
                self._page.wait_for_selector(
                    "#box-widget_InitialProductSelection", timeout=30000
                )
                # Extra wait for reCAPTCHA Enterprise to initialise
                self._page.wait_for_timeout(2000)

            try:
                if need_rewrite:
                    def _rewrite_dates(route):
                        url = route.request.url
                        if "GetInventoryCountData" not in url:
                            route.continue_()
                            return
                        new_url = url
                        sd_match = re.search(
                            r"(StartDate=)([^&]*)", new_url
                        )
                        if sd_match:
                            enc_start = quote_plus(target_start_str)
                            new_url = (
                                new_url[: sd_match.start(2)]
                                + enc_start
                                + new_url[sd_match.end(2) :]
                            )
                        ed_match = re.search(
                            r"(EndDate=)([^&]*)", new_url
                        )
                        if ed_match:
                            enc_end = quote_plus(target_end_str)
                            new_url = (
                                new_url[: ed_match.start(2)]
                                + enc_end
                                + new_url[ed_match.end(2) :]
                            )
                        logger.debug("Rewriting dates in request")
                        route.continue_(url=new_url)

                    self._page.route("**/*", _rewrite_dates)

                with self._page.expect_response(
                    lambda r: "GetInventoryCountData" in r.url,
                    timeout=30000,
                ) as resp_info:
                    self._select_property(initial_value)

                if need_rewrite:
                    self._page.unroute("**/*")

                resp = resp_info.value

                if resp.status == 200:
                    text = resp.text()
                    data = self._parse_jsonp(text)
                    if isinstance(data, list):
                        logger.debug(
                            "Got %d inventory items for %d/%d",
                            len(data),
                            month,
                            year,
                        )
                    return data
                else:
                    # Log response body for diagnostics
                    body_preview = ""
                    try:
                        body_preview = resp.text()[:200]
                    except Exception:
                        pass
                    logger.warning(
                        "API returned %d (attempt %d/%d)%s",
                        resp.status,
                        attempt,
                        max_attempts,
                        f": {body_preview}" if body_preview else "",
                    )

            except Exception as e:
                if attempt < max_attempts:
                    logger.info(
                        "Attempt %d/%d failed (%s), retrying...",
                        attempt,
                        max_attempts,
                        e,
                    )
                else:
                    logger.warning(
                        "All %d attempts failed for %s %d/%d: %s",
                        max_attempts,
                        multiprop_code,
                        month,
                        year,
                        e,
                    )
            finally:
                try:
                    self._page.unroute("**/*")
                except Exception:
                    pass

        # All attempts exhausted
        return []

    def _build_booking_url(self, property_code: str) -> str:
        """Build a browser-loadable booking URL for a property."""
        slug = YosemiteConfig.YOSEMITE_PROPERTY_SLUGS.get(property_code, "")
        return f"{YosemiteConfig.BOOKING_BASE_URL}/{slug}"

    def get_monthly_campsites(
        self,
        month: datetime,
        nights: Optional[int] = None,
        property_codes: Optional[set] = None,
    ) -> List[AvailableCampsite]:
        """
        Return all available campsites for a given month.

        Parameters
        ----------
        month: datetime
            Month to search (day is ignored, uses 1st of month)
        nights: Optional[int]
            Number of consecutive nights (used for booking_nights field)
        property_codes: Optional[set]
            If provided, only search these property codes (e.g., {'H', 'D'}).
            If None, searches all properties.

        Returns
        -------
        List[AvailableCampsite]
        """
        now = datetime.now().date()
        start_date = month.replace(day=1)
        if start_date < now:
            start_date = now
        _, last_day = monthrange(start_date.year, start_date.month)
        end_date = start_date.replace(day=last_day)

        booking_nights = nights if nights is not None else 1
        all_campsites = []

        # Only search requested properties (or all if none specified)
        properties = {
            code: name
            for code, name in YosemiteConfig.YOSEMITE_PROPERTIES.items()
            if property_codes is None or code in property_codes
        }

        for prop_code, prop_name in properties.items():
            logger.info(
                f"Searching Yosemite Lodging Availability: "
                f"{prop_name} - {month.strftime('%B, %Y')}"
            )
            try:
                inventory = self._get_inventory_count(
                    multiprop_code=prop_code,
                    month=start_date.month,
                    year=start_date.year,
                )
            except Exception as e:
                logger.warning(
                    f"Error fetching availability for {prop_name}: {e}"
                )
                continue

            available_dates = [
                item
                for item in inventory
                if item.get("AvailableCount", 0) > 0
            ]

            logger.info(
                f"\t{logging_utils.get_emoji(available_dates)}\t"
                f"{len(available_dates)} available dates found for {prop_name}."
            )

            # Filter to dates in our search window
            window_dates = []
            for item in available_dates:
                date_str = item["DateKey"]
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                if start_date <= d <= end_date:
                    window_dates.append(d)

            if not window_dates:
                logger.debug(
                    "No dates in target window (%s to %s) for %s",
                    start_date,
                    end_date,
                    prop_name,
                )
                continue

            logger.debug(
                "%d dates in target window for %s (e.g. %s)",
                len(window_dates),
                prop_name,
                ", ".join(d.isoformat() for d in window_dates[:5]),
            )

            booking_url = self._build_booking_url(prop_code)
            for d in window_dates:
                booking_date = datetime.combine(d, datetime.min.time())
                booking_end = booking_date + timedelta(days=booking_nights)
                campsite = AvailableCampsite(
                    campsite_id=f"{prop_code}_{d.isoformat()}",
                    booking_date=booking_date,
                    booking_end_date=booking_end,
                    booking_nights=booking_nights,
                    campsite_site_name=prop_name,
                    campsite_loop_name=YosemiteConfig.YOSEMITE_LOOP_NAME,
                    campsite_type="LODGING",
                    campsite_occupancy=(1, 6),
                    campsite_use_type="Overnight",
                    availability_status=YosemiteConfig.CAMPSITE_AVAILABILITY_STATUS,
                    recreation_area=YosemiteConfig.YOSEMITE_RECREATION_AREA_NAME,
                    recreation_area_id=YosemiteConfig.YOSEMITE_RECREATION_AREA_ID,
                    facility_name=prop_name,
                    facility_id=prop_code,
                    booking_url=booking_url,
                    permitted_equipment=None,
                    campsite_attributes=None,
                )
                all_campsites.append(campsite)

        return all_campsites

    def find_campgrounds(self, **kwargs) -> List[CampgroundFacility]:
        """
        List the available Yosemite lodging properties.
        """
        logging_utils.log_sorted_response(
            YosemiteConfig.YOSEMITE_CAMPGROUND_OBJECTS
        )
        return YosemiteConfig.YOSEMITE_CAMPGROUND_OBJECTS
