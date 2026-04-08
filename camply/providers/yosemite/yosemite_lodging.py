"""
Yosemite National Park Lodging Provider

Uses the Aramark/AHLS booking system at reservations.ahlsmsworld.com
to check availability for Yosemite lodging properties.

Automates the search page via Playwright — selects properties and months
in the calendar widget, and intercepts the GetInventoryCountData API
responses. This lets the page handle reCAPTCHA Enterprise internally.

Requires Playwright:
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

# JavaScript snippet that locates the year <select> on the AHLS calendar.
# Uses a regex (/^20\d{2}$/) so it works across year boundaries without
# hard-coding specific values like '2026'.
_JS_FIND_YEAR_SELECT = """() => {
    const selects = [...document.querySelectorAll('select')];
    return selects.find(s =>
        [...s.options].some(o => /^20\\d{2}$/.test(o.value))
    ) || null;
}"""

# JavaScript snippet that locates the month <select> by checking for
# standard 3-letter English month abbreviations in option text.
_JS_FIND_MONTH_SELECT = """() => {
    const selects = [...document.querySelectorAll('select')];
    return selects.find(s => {
        const texts = [...s.options].map(o => o.text);
        return texts.some(t =>
            ['Jan','Feb','Mar','Apr','May','Jun',
             'Jul','Aug','Sep','Oct','Nov','Dec'].includes(t)
        );
    }) || null;
}"""

# JavaScript snippet that checks whether both calendar selects are present.
_JS_CALENDAR_READY = """() => {
    const selects = [...document.querySelectorAll('select')];
    const hasYear = selects.some(s =>
        [...s.options].some(o => /^20\\d{2}$/.test(o.value))
    );
    const hasMonth = selects.some(s => {
        const texts = [...s.options].map(o => o.text);
        return texts.some(t =>
            ['Jan','Feb','Mar','Apr','May','Jun',
             'Jul','Aug','Sep','Oct','Nov','Dec'].includes(t)
        );
    });
    return hasYear && hasMonth;
}"""


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

    def _log_page_selects(self) -> None:
        """
        Log all <select> elements on the page for debugging.
        Helps diagnose when calendar selects cannot be found.
        """
        try:
            selects_info = self._page.evaluate(
                """() => {
                    return [...document.querySelectorAll('select')].map(s => ({
                        id: s.id,
                        name: s.name,
                        visible: s.offsetParent !== null,
                        optionCount: s.options.length,
                        sampleOptions: [...s.options].slice(0, 5).map(o => ({
                            value: o.value, text: o.text
                        })),
                    }));
                }"""
            )
            logger.warning(
                "Page select elements:\n%s",
                json.dumps(selects_info, indent=2),
            )
        except Exception as exc:
            logger.debug("Could not log page selects: %s", exc)

    def _get_inventory_count(
        self,
        multiprop_code: str,
        month: int,
        year: int,
    ) -> list:
        """
        Get inventory count by selecting a property in the page's
        InitialProductSelection dropdown, then navigating the calendar
        and intercepting the GetInventoryCountData API response.

        The page handles reCAPTCHA Enterprise internally when the
        dropdown and calendar changes trigger the search widget.

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
        target_month_val = str(month - 1)  # Page uses 0-indexed months
        target_year_val = str(year)

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
                # Step 1: Select property.
                # This triggers reCAPTCHA and an API call for the *default*
                # month.  We MUST consume this response so it does not leak
                # into Step 3's expect_response (which would then return
                # data for the wrong month).
                # ----------------------------------------------------------
                with self._page.expect_response(
                    lambda r: "GetInventoryCountData" in r.url,
                    timeout=30000,
                ) as initial_resp_info:
                    self._page.select_option(
                        "#box-widget_InitialProductSelection",
                        value=initial_value,
                    )

                initial_resp = initial_resp_info.value
                logger.debug(
                    "Initial API call: status=%s url=%s",
                    initial_resp.status,
                    initial_resp.url[:200],
                )

                if initial_resp.status != 200:
                    logger.warning(
                        "Initial API call returned %s — reCAPTCHA may have "
                        "blocked the request (attempt %s/%s)",
                        initial_resp.status,
                        attempt,
                        max_attempts,
                    )
                    continue

                # ----------------------------------------------------------
                # Step 2: Wait for calendar month/year <select> elements.
                # After property selection the page transitions to a detail
                # view with a calendar widget.  We wait for the year and
                # month selects to appear rather than using a fixed timeout.
                # ----------------------------------------------------------
                try:
                    self._page.wait_for_function(
                        _JS_CALENDAR_READY, timeout=10000
                    )
                except Exception:
                    logger.warning(
                        "Calendar selects did not appear after property "
                        "selection (attempt %s/%s)",
                        attempt,
                        max_attempts,
                    )
                    self._log_page_selects()
                    continue

                # Read current calendar state so we only change what's needed.
                cal_state = self._page.evaluate(
                    """() => {
                        const selects = [...document.querySelectorAll('select')];
                        let yearVal = null, monthVal = null;
                        for (const s of selects) {
                            if ([...s.options].some(o => /^20\\d{2}$/.test(o.value)))
                                yearVal = s.value;
                        }
                        for (const s of selects) {
                            const texts = [...s.options].map(o => o.text);
                            if (texts.some(t =>
                                ['Jan','Feb','Mar','Apr','May','Jun',
                                 'Jul','Aug','Sep','Oct','Nov','Dec'].includes(t)
                            ))
                                monthVal = s.value;
                        }
                        return {yearVal: yearVal, monthVal: monthVal};
                    }"""
                )
                logger.debug(
                    "Calendar state: year=%s month=%s (target: year=%s month=%s)",
                    cal_state.get("yearVal"),
                    cal_state.get("monthVal"),
                    target_year_val,
                    target_month_val,
                )

                # If the default calendar already shows our target, the
                # initial response has the data we need.
                if (
                    cal_state.get("yearVal") == target_year_val
                    and cal_state.get("monthVal") == target_month_val
                ):
                    return self._parse_jsonp(initial_resp.text())

                # ----------------------------------------------------------
                # Step 3: Navigate the calendar to the target month/year.
                #
                # KEY FIX: Use Playwright's select_option() on ElementHandles
                # instead of JavaScript dispatchEvent(new Event('change')).
                # Playwright triggers *trusted* native browser events
                # (isTrusted=true) which framework event handlers (jQuery,
                # React, Angular) recognise.  The old dispatchEvent approach
                # created *untrusted* synthetic events that frameworks may
                # silently ignore, preventing the widget from making the API
                # call.
                # ----------------------------------------------------------
                year_handle = self._page.evaluate_handle(_JS_FIND_YEAR_SELECT)
                month_handle = self._page.evaluate_handle(_JS_FIND_MONTH_SELECT)

                year_element = year_handle.as_element()
                month_element = month_handle.as_element()

                if not year_element or not month_element:
                    logger.warning(
                        "Could not get element handles for calendar selects "
                        "(attempt %s/%s)",
                        attempt,
                        max_attempts,
                    )
                    self._log_page_selects()
                    continue

                # 3a. Change year first (if needed).
                # Changing the year may or may not trigger its own API call
                # (depends on the widget implementation).  If it does, we
                # consume the response so it doesn't leak into 3b.
                if cal_state.get("yearVal") != target_year_val:
                    try:
                        with self._page.expect_response(
                            lambda r: "GetInventoryCountData" in r.url,
                            timeout=10000,
                        ):
                            year_element.select_option(value=target_year_val)
                        logger.debug("Year change triggered an API call (consumed)")
                    except Exception:
                        # Year change alone didn't trigger an API call — that's
                        # fine, the month change will trigger the combined call.
                        logger.debug(
                            "Year change did not trigger a separate API call"
                        )

                # 3b. Change month and capture the response.
                # This is the response that contains the target month/year data.
                with self._page.expect_response(
                    lambda r: "GetInventoryCountData" in r.url,
                    timeout=30000,
                ) as target_resp_info:
                    month_element.select_option(value=target_month_val)

                response = target_resp_info.value
                if response.status != 200:
                    logger.warning(
                        "Calendar API call returned %s (attempt %s/%s)",
                        response.status,
                        attempt,
                        max_attempts,
                    )
                    continue

                text = response.text()
                return self._parse_jsonp(text)

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
