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
import re
from calendar import monthrange
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote_plus

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
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = self._browser.new_context(
            user_agent=self.session.headers.get("User-Agent", ""),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id=YosemiteConfig.YOSEMITE_TIMEZONE,
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
        self._page.goto(YosemiteConfig.SEARCH_PAGE_URL, wait_until="networkidle")
        # Wait for the visible search widget to be ready
        self._page.wait_for_selector(
            "#box-widget_InitialProductSelection", timeout=30000
        )
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

    def _get_inventory_count(
        self,
        multiprop_code: str,
        month: int,
        year: int,
    ) -> list:
        """
        Get inventory count by selecting a property from the dropdown
        and intercepting the GetInventoryCountData API response.

        The AHLS widget does NOT expose month/year <select> elements.
        Instead, the target month is controlled via the ``StartDate``
        query parameter in the API URL.  We use Playwright route
        interception to rewrite that parameter before the request
        leaves the browser, so the page's own reCAPTCHA Enterprise
        token stays valid while we get data for the month we want.

        Parameters
        ----------
        multiprop_code: str
            Property code (e.g., 'H' for Housekeeping Camp)
        month: int
            Month number (1-12)
        year: int
            Year (e.g., 2026)

        Returns
        -------
        list
            List of dicts with 'DateKey' and 'AvailableCount'
        """
        self._ensure_browser()

        max_attempts = 3
        initial_value = (
            f"{YosemiteConfig.YOSEMITE_RECREATION_AREA_ID}:{multiprop_code}"
        )

        # Build target StartDate and EndDate using the browser's JS
        # engine.  The AHLS widget uses Date.toDateString() format
        # (e.g. "Wed Jul 01 2026") and sends a 3-month window.
        # Both must be rewritten or the server rejects the range.
        target_start_str = self._page.evaluate(
            f"new Date({year}, {month - 1}, 1).toDateString()"
        )
        # EndDate = last day of the month 2 months after StartDate
        # (mirroring the widget's 3-month window: Mar 1 → May 31).
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
            "Target date range: %s -> %s", target_start_str, target_end_str
        )

        for attempt in range(1, max_attempts + 1):
            # Reload the page to reset to the landing state
            self._page.goto(
                YosemiteConfig.SEARCH_PAGE_URL, wait_until="networkidle"
            )
            self._page.wait_for_selector(
                "#box-widget_InitialProductSelection", timeout=30000
            )

            try:
                # ----------------------------------------------------------
                # Phase A: Passthrough — let the original request go
                # unmodified so we can confirm the API works at all and
                # capture the full original URL for comparison.
                # ----------------------------------------------------------
                captured_url = {}

                def _log_passthrough(route):
                    url = route.request.url
                    if "GetInventoryCountData" in url:
                        captured_url["original"] = url
                        logger.debug("Passthrough request URL:\n  %s", url)
                    route.continue_()

                self._page.route("**/*", _log_passthrough)

                with self._page.expect_response(
                    lambda r: "GetInventoryCountData" in r.url,
                    timeout=30000,
                ) as passthrough_info:
                    self._page.select_option(
                        "#box-widget_InitialProductSelection",
                        value=initial_value,
                    )

                self._page.unroute("**/*")
                passthrough_resp = passthrough_info.value

                logger.debug(
                    "Passthrough response: status=%s",
                    passthrough_resp.status,
                )

                if passthrough_resp.status == 200:
                    # The unmodified request succeeded.  Parse the
                    # original StartDate so we can see what month the
                    # widget defaulted to.
                    orig_match = re.search(
                        r"StartDate=([^&]*)",
                        captured_url.get("original", ""),
                    )
                    orig_date = orig_match.group(1) if orig_match else "?"
                    logger.info(
                        "Passthrough OK (default StartDate=%s)", orig_date
                    )

                    # Check if we even need to rewrite — does the
                    # default month already match our target?
                    passthrough_text = passthrough_resp.text()
                    passthrough_data = self._parse_jsonp(passthrough_text)
                    if isinstance(passthrough_data, list) and passthrough_data:
                        first_key = passthrough_data[0].get("DateKey", "")
                        # DateKey is "YYYY-MM-DD"; check month/year
                        if first_key.startswith(f"{year}-{month:02d}"):
                            logger.debug(
                                "Passthrough data already matches "
                                "target month — using it directly"
                            )
                            return passthrough_data

                    # -------------------------------------------------------
                    # Phase B: Rewrite — now that we know the API works,
                    # reload and intercept with our target StartDate.
                    # -------------------------------------------------------
                    self._page.goto(
                        YosemiteConfig.SEARCH_PAGE_URL,
                        wait_until="networkidle",
                    )
                    self._page.wait_for_selector(
                        "#box-widget_InitialProductSelection", timeout=30000
                    )

                    def _rewrite_dates(route):
                        url = route.request.url
                        if "GetInventoryCountData" not in url:
                            route.continue_()
                            return
                        new_url = url
                        # Rewrite StartDate
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
                        # Rewrite EndDate
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
                        logger.debug(
                            "Rewrote dates: StartDate=%s, EndDate=%s",
                            quote_plus(target_start_str)[:25],
                            quote_plus(target_end_str)[:25],
                        )
                        route.continue_(url=new_url)

                    self._page.route(
                        "**/GetInventoryCountData*", _rewrite_dates
                    )

                    with self._page.expect_response(
                        lambda r: "GetInventoryCountData" in r.url,
                        timeout=30000,
                    ) as rewrite_info:
                        self._page.select_option(
                            "#box-widget_InitialProductSelection",
                            value=initial_value,
                        )

                    self._page.unroute("**/GetInventoryCountData*")
                    rewrite_resp = rewrite_info.value
                    logger.debug(
                        "Rewrite response: status=%s", rewrite_resp.status
                    )

                    if rewrite_resp.status == 200:
                        text = rewrite_resp.text()
                        data = self._parse_jsonp(text)
                        if isinstance(data, list):
                            logger.debug(
                                "Got %s inventory items for %s/%s",
                                len(data),
                                month,
                                year,
                            )
                        return data
                    else:
                        try:
                            body = rewrite_resp.text()[:500]
                        except Exception:
                            body = "(could not read)"
                        logger.warning(
                            "Rewrite request returned %s (attempt %s/%s)"
                            "\n  body: %s",
                            rewrite_resp.status,
                            attempt,
                            max_attempts,
                            body,
                        )
                        continue
                else:
                    # Passthrough itself failed — likely reCAPTCHA block
                    try:
                        body = passthrough_resp.text()[:500]
                    except Exception:
                        body = "(could not read)"
                    logger.warning(
                        "Passthrough returned %s (attempt %s/%s)"
                        "\n  body: %s",
                        passthrough_resp.status,
                        attempt,
                        max_attempts,
                        body,
                    )
                    continue

            except Exception as e:
                if attempt < max_attempts:
                    logger.info(
                        "Attempt %s/%s failed (%s), retrying...",
                        attempt,
                        max_attempts,
                        e,
                    )
                else:
                    raise
            finally:
                try:
                    self._page.unroute("**/*")
                except Exception:
                    pass
                try:
                    self._page.unroute("**/GetInventoryCountData*")
                except Exception:
                    pass

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

            booking_url = self._build_booking_url(prop_code)
            for item in available_dates:
                date_str = item["DateKey"]
                booking_date = datetime.strptime(date_str, "%Y-%m-%d")
                # Skip dates outside our actual range
                if booking_date.date() < start_date or booking_date.date() > end_date:
                    continue
                booking_end = booking_date + timedelta(days=booking_nights)
                campsite = AvailableCampsite(
                    campsite_id=f"{prop_code}_{date_str}",
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
