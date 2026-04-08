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

    def _get_inventory_count(
        self,
        multiprop_code: str,
        month: int,
        year: int,
    ) -> list:
        """
        Get inventory count by selecting a property from the dropdown
        and intercepting the GetInventoryCountData API response.

        Uses Playwright route interception to rewrite the StartDate and
        EndDate query parameters before the request leaves the browser,
        so the page's own reCAPTCHA Enterprise token stays valid while
        we get data for the month we want.

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

        max_attempts = 4
        initial_value = (
            f"{YosemiteConfig.YOSEMITE_RECREATION_AREA_ID}:{multiprop_code}"
        )

        # Build target StartDate and EndDate using the browser's JS
        # engine.  The AHLS widget uses Date.toDateString() format
        # (e.g. "Wed Jul 01 2026") and sends a 3-month window.
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
            "Target date range: %s -> %s", target_start_str, target_end_str
        )

        for attempt in range(1, max_attempts + 1):
            # Reload the page to get a fresh reCAPTCHA token
            self._page.goto(
                YosemiteConfig.SEARCH_PAGE_URL, wait_until="networkidle"
            )
            self._page.wait_for_selector(
                "#box-widget_InitialProductSelection", timeout=30000
            )

            try:
                # Intercept the outgoing API request and rewrite dates
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
                        "Rewriting dates: StartDate=%s, EndDate=%s",
                        quote_plus(target_start_str)[:25],
                        quote_plus(target_end_str)[:25],
                    )
                    route.continue_(url=new_url)

                self._page.route("**/*", _rewrite_dates)

                with self._page.expect_response(
                    lambda r: "GetInventoryCountData" in r.url,
                    timeout=30000,
                ) as resp_info:
                    self._page.select_option(
                        "#box-widget_InitialProductSelection",
                        value=initial_value,
                    )

                self._page.unroute("**/*")
                resp = resp_info.value

                if resp.status == 200:
                    text = resp.text()
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
                    logger.warning(
                        "API returned %s (attempt %s/%s)",
                        resp.status,
                        attempt,
                        max_attempts,
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
                    logger.warning(
                        "All %s attempts failed for %s/%s: %s",
                        max_attempts,
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

    def _search_room_types(
        self,
        multiprop_code: str,
        checkin_iso: str,
        checkout_iso: str,
    ) -> List[str]:
        """
        Perform a full search via form submission to get room type names.

        Fills the search form (property + dates), clicks CHECK
        AVAILABILITY, waits for the results page, and extracts room
        type names from the HTML.

        Parameters
        ----------
        multiprop_code: str
            Property code (e.g., 'D' for Curry Village)
        checkin_iso: str
            Check-in date in ISO format (e.g., '2026-07-06')
        checkout_iso: str
            Check-out date in ISO format (e.g., '2026-07-07')

        Returns
        -------
        List[str]
            List of room type names (e.g., ['Heated Canvas Tent Cabin
            - 1 Double Bed', 'Unheated Canvas Tent Cabin - 1 Double
            Bed', ...])
        """
        self._ensure_browser()

        initial_value = (
            f"{YosemiteConfig.YOSEMITE_RECREATION_AREA_ID}:{multiprop_code}"
        )

        # Navigate fresh
        self._page.goto(
            YosemiteConfig.SEARCH_PAGE_URL, wait_until="networkidle"
        )
        self._page.wait_for_selector(
            "#box-widget_InitialProductSelection", timeout=30000
        )

        # Select property — this reveals the date form
        self._page.select_option(
            "#box-widget_InitialProductSelection",
            value=initial_value,
        )
        self._page.wait_for_timeout(2000)

        # Fill dates via JavaScript — the native date inputs (_nd) are
        # hidden behind a custom datepicker UI, so Playwright's fill()
        # can't interact with them directly.  Set values on both the
        # hidden native inputs and the visible text inputs, then
        # dispatch change events to trigger the widget's JS.
        # Text inputs use M/D/YYYY format; native inputs use YYYY-MM-DD.
        parts = checkin_iso.split("-")
        checkin_text = f"{int(parts[1])}/{int(parts[2])}/{parts[0]}"
        parts = checkout_iso.split("-")
        checkout_text = f"{int(parts[1])}/{int(parts[2])}/{parts[0]}"

        self._page.evaluate(
            f"""() => {{
                // Set native date inputs
                const arrNd = document.querySelector('#box-widget_ArrivalDate_nd');
                const depNd = document.querySelector('#box-widget_DepartureDate_nd');
                if (arrNd) {{ arrNd.value = '{checkin_iso}'; arrNd.dispatchEvent(new Event('change', {{bubbles: true}})); }}
                if (depNd) {{ depNd.value = '{checkout_iso}'; depNd.dispatchEvent(new Event('change', {{bubbles: true}})); }}
                // Set visible text inputs
                const arr = document.querySelector('#box-widget_ArrivalDate');
                const dep = document.querySelector('#box-widget_DepartureDate');
                if (arr) {{ arr.value = '{checkin_text}'; arr.dispatchEvent(new Event('change', {{bubbles: true}})); }}
                if (dep) {{ dep.value = '{checkout_text}'; dep.dispatchEvent(new Event('change', {{bubbles: true}})); }}
            }}"""
        )
        logger.debug(
            "Form search: %s checkin=%s checkout=%s",
            multiprop_code,
            checkin_text,
            checkout_text,
        )

        self._page.wait_for_timeout(1000)

        # Click the non-disabled CHECK AVAILABILITY submit button
        button = self._page.query_selector(
            "input[type='submit'][value='Check Availability']:not([disabled])"
        )
        if not button:
            logger.warning("No enabled CHECK AVAILABILITY button found")
            return []

        button.click()

        # Wait for navigation to the results page
        try:
            self._page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        self._page.wait_for_timeout(2000)

        # Extract room type names from the results page.
        # The results page shows room cards with <h3> or bold names.
        # Try several selectors that might match the room type headings.
        room_types = []

        # Try common patterns for room type cards
        for selector in [
            ".unit-type-name",
            ".room-name",
            ".result-item h3",
            ".search-result h3",
            "h3.unit-name",
            ".product-title",
            ".wxa-search-result-unit-name",
        ]:
            elements = self._page.query_selector_all(selector)
            if elements:
                room_types = [
                    el.inner_text().strip() for el in elements
                    if el.inner_text().strip()
                ]
                logger.debug(
                    "Found %s room types via '%s': %s",
                    len(room_types),
                    selector,
                    room_types,
                )
                break

        # Fallback: parse room type names from page text using patterns
        if not room_types:
            page_text = self._page.inner_text("body")
            logger.debug(
                "No room types found via selectors, parsing page text "
                "(first 3000 chars):\n%s",
                page_text[:3000],
            )
            # Look for "Showing X to Y of Z" to confirm results exist
            showing_match = re.search(
                r"Showing \d+ to \d+ of (\d+)", page_text
            )
            if showing_match:
                logger.debug(
                    "Results page shows %s results", showing_match.group(1)
                )

            # Try to extract room names from the HTML directly
            # Room cards typically have bold headings
            html = self._page.content()
            # Look for h-tags or strong tags that contain room type names
            heading_matches = re.findall(
                r"<(?:h[2-4]|strong)[^>]*>\s*([^<]+(?:Cabin|Room|Suite|"
                r"Lodge|Camp|Tent|Cottage|Bungalow|House)[^<]*)\s*"
                r"</(?:h[2-4]|strong)>",
                html,
                re.IGNORECASE,
            )
            if heading_matches:
                room_types = [m.strip() for m in heading_matches]
                logger.debug(
                    "Found %s room types via HTML parsing: %s",
                    len(room_types),
                    room_types,
                )

        if not room_types:
            logger.debug("Could not find room type names on results page")

        return room_types

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
                continue

            # For the first available date in the window, do a form
            # search to discover room type names. Room types are the
            # same for a property regardless of date, so one lookup
            # is enough.
            room_types = []
            first_date = window_dates[0]
            checkout_d = first_date + timedelta(days=booking_nights)
            try:
                room_types = self._search_room_types(
                    multiprop_code=prop_code,
                    checkin_iso=first_date.isoformat(),
                    checkout_iso=checkout_d.isoformat(),
                )
            except Exception as e:
                logger.warning(
                    "Room type search failed for %s: %s", prop_name, e
                )

            # If we got room types, create one campsite per room type
            # per date.  Otherwise fall back to one per date.
            booking_url = self._build_booking_url(prop_code)
            if room_types:
                for d in window_dates:
                    for room_name in room_types:
                        date_str = d.isoformat()
                        booking_date = datetime.combine(
                            d, datetime.min.time()
                        )
                        booking_end = booking_date + timedelta(
                            days=booking_nights
                        )
                        campsite = AvailableCampsite(
                            campsite_id=f"{prop_code}_{date_str}_{room_name}",
                            booking_date=booking_date,
                            booking_end_date=booking_end,
                            booking_nights=booking_nights,
                            campsite_site_name=room_name,
                            campsite_loop_name=prop_name,
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
            else:
                # Fallback: one campsite per date (no room type detail)
                for d in window_dates:
                    date_str = d.isoformat()
                    booking_date = datetime.combine(
                        d, datetime.min.time()
                    )
                    booking_end = booking_date + timedelta(
                        days=booking_nights
                    )
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
