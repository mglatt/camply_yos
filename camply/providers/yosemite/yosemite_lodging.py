"""
Yosemite National Park Lodging Provider

Uses the Aramark/AHLS booking system at reservations.ahlsmsworld.com
to check availability for Yosemite lodging properties.

Requires Playwright for reCAPTCHA token generation and API calls:
    pip install playwright && python -m playwright install chromium
"""

import json
import logging
import re
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

    All API calls run inside a headless browser to handle reCAPTCHA
    Enterprise token generation natively.
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
        self._recaptcha_ready = False

    def _ensure_browser(self) -> None:
        """
        Launch headless browser and navigate to the search page
        so that reCAPTCHA is loaded and ready to generate tokens.
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
        # Extract the reCAPTCHA action string from the page's own JavaScript
        self._page.evaluate(
            """() => {
                // Monkeypatch execute to capture the action string the page uses
                window.__capturedRecaptchaAction = null;
                window.__capturedRecaptchaSiteKey = null;
                const patchExec = (obj, name) => {
                    const orig = obj[name].bind(obj);
                    obj[name] = function(siteKey, options) {
                        window.__capturedRecaptchaSiteKey = siteKey;
                        if (options && options.action) {
                            window.__capturedRecaptchaAction = options.action;
                        }
                        return orig(siteKey, options);
                    };
                };
                if (typeof grecaptcha.enterprise !== 'undefined' &&
                    typeof grecaptcha.enterprise.execute === 'function') {
                    patchExec(grecaptcha.enterprise, 'execute');
                } else if (typeof grecaptcha.execute === 'function') {
                    patchExec(grecaptcha, 'execute');
                }
            }"""
        )
        logger.info("Browser ready - reCAPTCHA loaded.")
        self._recaptcha_ready = True

    def _close_browser(self) -> None:
        """Clean up browser resources."""
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None
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
        Call the GetInventoryCountData API from inside the browser context.

        This makes the fetch call from the same browser session that loaded
        reCAPTCHA, so token generation, cookies, and headers are all handled
        natively by the browser — no need to sync cookies or pass tokens
        through an external HTTP client.

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
        self._ensure_browser()
        start_str = self._format_date_for_api(start_date)
        end_str = self._format_date_for_api(end_date)

        result = self._page.evaluate(
            """async ([multiPropCode, cresPropCode, startDate, endDate, searchPath]) => {
                // Determine which execute function to use
                const isEnterprise = (typeof grecaptcha.enterprise !== 'undefined' &&
                    typeof grecaptcha.enterprise.execute === 'function');
                const execFn = isEnterprise
                    ? grecaptcha.enterprise.execute.bind(grecaptcha.enterprise)
                    : grecaptcha.execute.bind(grecaptcha);

                // Extract site key from the recaptcha script tag
                let siteKey = null;
                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                for (const s of scripts) {
                    const m = s.src.match(/[?&]render=([^&]+)/);
                    if (m && m[1] !== 'explicit') { siteKey = m[1]; break; }
                }
                if (!siteKey) {
                    const el = document.querySelector('[data-sitekey]');
                    if (el) siteKey = el.getAttribute('data-sitekey');
                }
                if (!siteKey) throw new Error('Could not find reCAPTCHA site key on page');

                // Use captured action if available, otherwise try common actions
                const action = window.__capturedRecaptchaAction || 'submit';

                // Generate token
                const token = await execFn(siteKey, {action: action});

                // Build the API URL
                const params = new URLSearchParams({
                    CresPropCode: cresPropCode,
                    MultiPropCode: multiPropCode,
                    UnitTypeCode: '',
                    StartDate: startDate,
                    EndDate: endDate,
                    RecaptchaToken: token,
                    _: Date.now().toString()
                });

                const response = await fetch(searchPath + '?' + params.toString(), {
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'application/json, text/javascript, */*; q=0.01'
                    }
                });

                if (!response.ok) {
                    const body = await response.text().catch(() => '');
                    throw new Error('HTTP ' + response.status + ': ' + response.statusText + ' - ' + body.substring(0, 200));
                }

                const text = await response.text();
                // Parse JSONP wrapper if present
                const match = text.match(/^[^(\\[{]+\\((.+)\\);?\\s*$/s);
                if (match) return JSON.parse(match[1]);
                return JSON.parse(text);
            }""",
            [
                multiprop_code,
                YosemiteConfig.CRES_PROP_CODE,
                start_str,
                end_str,
                YosemiteConfig.API_SEARCH_PATH,
            ],
        )
        return result

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
