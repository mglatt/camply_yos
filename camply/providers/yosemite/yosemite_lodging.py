"""
Yosemite National Park Lodging Provider

Uses the Aramark/AHLS booking system at reservations.ahlsmsworld.com
to check availability for Yosemite lodging properties.

Requires Playwright for reCAPTCHA v3 token generation:
    pip install playwright && python -m playwright install chromium
"""

import json
import logging
import re
import time
from calendar import monthrange
from datetime import datetime, timedelta
from typing import List, Optional

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
        self._browser_cookies_synced = False
        self._recaptcha_ready = False
        self._use_enterprise = False
        self._site_key = None
        self.session.headers.update(
            {
                "Accept": "text/javascript, application/javascript, "
                "application/ecmascript, application/x-ecmascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": YosemiteConfig.SEARCH_PAGE_URL,
                "Host": "reservations.ahlsmsworld.com",
            }
        )

    def _ensure_browser(self) -> None:
        """
        Launch headless browser and navigate to the search page
        so that reCAPTCHA v3 is loaded and ready to generate tokens.
        """
        if self._page is not None and self._recaptcha_ready:
            return
        # Clean up any broken previous state
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
        logger.info("Launching headless browser for reCAPTCHA token generation...")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = self._browser.new_context(
            user_agent=self.session.headers.get("User-Agent", ""),
        )
        self._page = context.new_page()
        # Hide webdriver flag to avoid headless detection
        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._page.goto(YosemiteConfig.SEARCH_PAGE_URL, wait_until="networkidle")
        # Wait for reCAPTCHA — handle both standard and enterprise APIs
        self._page.wait_for_function(
            """() => {
                if (typeof grecaptcha !== 'undefined') {
                    if (typeof grecaptcha.execute === 'function') return true;
                    if (grecaptcha.enterprise && typeof grecaptcha.enterprise.execute === 'function') return true;
                }
                return false;
            }""",
            timeout=60000,
        )
        # Detect which API variant is available
        self._use_enterprise = self._page.evaluate(
            "typeof grecaptcha.enterprise !== 'undefined' "
            "&& typeof grecaptcha.enterprise.execute === 'function'"
        )
        # Extract site key from the page instead of using hardcoded value
        self._site_key = self._page.evaluate(
            """() => {
                // Try the render= parameter in the recaptcha script URL
                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                for (const s of scripts) {
                    const m = s.src.match(/[?&]render=([^&]+)/);
                    if (m && m[1] !== 'explicit') return m[1];
                }
                // Try data-sitekey attributes
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                return null;
            }"""
        )
        if not self._site_key:
            # Fall back to config value
            self._site_key = YosemiteConfig.RECAPTCHA_SITE_KEY
        api_type = "enterprise" if self._use_enterprise else "standard"
        logger.info(
            f"Browser ready - reCAPTCHA loaded ({api_type} API, "
            f"site key: {self._site_key[:8]}...)."
        )
        self._recaptcha_ready = True

    def _get_recaptcha_token(self) -> str:
        """
        Generate a fresh reCAPTCHA v3 token using the browser session.
        Tokens are single-use and expire in ~2 minutes.
        """
        self._ensure_browser()
        site_key = self._site_key
        if self._use_enterprise:
            token = self._page.evaluate(
                f"grecaptcha.enterprise.execute('{site_key}', {{action: 'submit'}})"
            )
        else:
            token = self._page.evaluate(
                f"grecaptcha.execute('{site_key}', {{action: 'submit'}})"
            )
        return token

    def _sync_browser_cookies(self) -> None:
        """
        Copy cookies from the Playwright browser context to the requests session.
        """
        if self._browser_cookies_synced:
            return
        self._ensure_browser()
        cookies = self._page.context.cookies()
        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        self._browser_cookies_synced = True

    def _close_browser(self) -> None:
        """Clean up browser resources."""
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None
        self._browser_cookies_synced = False
        self._recaptcha_ready = False

    def __del__(self):
        self._close_browser()

    @staticmethod
    def _parse_jsonp(text: str) -> object:
        """
        Strip JSONP callback wrapper and parse the JSON payload.

        Handles both named callbacks like:
            callbackName([{...}])
        and jQuery-style:
            jQuery123456_789({...})
        """
        match = re.match(r"^[^(\[{]+\((.+)\);?\s*$", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        return json.loads(text)

    @staticmethod
    def _format_date_for_api(dt: datetime) -> str:
        """
        Format a date for the GetInventoryCountData API.

        The API expects dates like: 'Fri May 01 2026'
        (JavaScript Date toString-style format)
        """
        return dt.strftime("%a %b %d %Y")

    def _get_inventory_count(
        self,
        multiprop_code: str,
        start_date: datetime,
        end_date: datetime,
    ) -> list:
        """
        Call the GetInventoryCountData API for a property and date range.

        Parameters
        ----------
        multiprop_code: str
            Property code (e.g., 'H' for Housekeeping Camp)
        start_date: datetime
            Start of date range
        end_date: datetime
            End of date range

        Returns
        -------
        list
            List of dicts with 'DateKey' and 'AvailableCount'
        """
        self._sync_browser_cookies()
        token = self._get_recaptcha_token()
        params = {
            "callback": "camply_callback",
            "CresPropCode": YosemiteConfig.CRES_PROP_CODE,
            "MultiPropCode": multiprop_code,
            "UnitTypeCode": "",
            "StartDate": self._format_date_for_api(start_date),
            "EndDate": self._format_date_for_api(end_date),
            "RecaptchaToken": token,
            "_": str(int(time.time() * 1000)),
        }
        url = f"{YosemiteConfig.API_BASE_URL}{YosemiteConfig.API_SEARCH_PATH}"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return self._parse_jsonp(response.text)

    def _build_booking_url(self, property_code: str) -> str:
        """Build a browser-loadable booking URL for a property."""
        slug = YosemiteConfig.YOSEMITE_PROPERTY_SLUGS.get(property_code, "")
        return f"{YosemiteConfig.BOOKING_BASE_URL}/{slug}"

    def get_monthly_campsites(
        self,
        month: datetime,
        nights: Optional[int] = None,
    ) -> List[AvailableCampsite]:
        """
        Return all available campsites for a given month across all properties.

        Parameters
        ----------
        month: datetime
            Month to search (day is ignored, uses 1st of month)
        nights: Optional[int]
            Number of consecutive nights (used for booking_nights field)

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

        for prop_code, prop_name in YosemiteConfig.YOSEMITE_PROPERTIES.items():
            logger.info(
                f"Searching Yosemite Lodging Availability: "
                f"{prop_name} - {month.strftime('%B, %Y')}"
            )
            try:
                inventory = self._get_inventory_count(
                    multiprop_code=prop_code,
                    start_date=start_date,
                    end_date=end_date,
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
