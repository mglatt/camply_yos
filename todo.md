# Yosemite Provider — Development Recap & Session Context

## What This Is

A new provider for **camply** (campsite availability CLI) that checks lodging availability at Yosemite National Park via the Aramark/AHLS booking system at `reservations.ahlsmsworld.com`.

## Architecture

```
camply/config/api_config.py          → YosemiteConfig (URLs, property codes, constants)
camply/providers/yosemite/
  __init__.py                        → exports YosemiteLodging
  yosemite_lodging.py                → YosemiteLodging(BaseProvider) — THE main file
camply/search/search_yosemite.py     → SearchYosemite(BaseCampingSearch) — orchestrator
camply/search/__init__.py            → registers SearchYosemite
camply/providers/__init__.py         → exports YosemiteLodging  
camply/cli.py                        → YosemiteLodging in provider allowlists
```

**8 files changed, 759 insertions vs main.** No test files exist.

## How It Works (Current Approach)

Uses **Playwright headless Chromium** to automate the AHLS search page:

1. **Navigate** to `https://reservations.ahlsmsworld.com/Yosemite/Plan-Your-Trip/`
2. **Wait** for the property dropdown (`#box-widget_InitialProductSelection`)
3. **Select a property** (e.g., value `"2:D"` for Curry Village) — this triggers the page to make an API call with a fresh reCAPTCHA Enterprise token
4. **Intercept** the outgoing `GetInventoryCountData` request via `page.route()`
5. **Rewrite** `StartDate` and `EndDate` URL parameters to the target month (3-month window)
6. **Capture** the JSONP response, strip the callback wrapper, parse the JSON

### Why Playwright (not direct HTTP)?
The API requires a **reCAPTCHA Enterprise** token that's generated per-interaction on the page. Tokens are embedded in the API URL as a `RecaptchaToken` parameter. We can't generate these tokens externally, so we let the browser/page handle reCAPTCHA natively and only modify the date parameters.

### API Details
- **Endpoint**: `GetInventoryCountData` (JSONP)
- **JSONP callback**: `$.wxa.on_datepicker_general_availability_loaded`
- **Date format**: `Date.toDateString()` → e.g., `"Wed Jul 01 2026"` (NOT `Date.toString()`)
- **Date window**: 3 months (e.g., `StartDate=Jul 1` + `EndDate=Sep 30`)
- **Response**: Array of `{ "DateKey": "2026-07-01", "AvailableCount": 5, ... }`

### Properties (5 total)
| Code | Name | Dropdown Value |
|------|------|---------------|
| D | Curry Village | 2:D |
| H | Housekeeping Camp | 2:H |
| M | The Ahwahnee | 2:M |
| T | Tuolumne Meadows Lodge | 2:T |
| Y | Yosemite Valley Lodge | 2:Y |

## Git History (branch: `claude/fix-yosemite-availability-EiVgP`)

```
f80195d  🐛 Rewrite both StartDate and EndDate in route interception
e57c5d3  🔍 Two-phase approach: passthrough then rewrite for diagnosis
bceb6ba  🐛 Fix StartDate format and URL encoding in route interception
30f387c  🐛 Replace calendar navigation with route interception
d29f1f9  🔍 Add diagnostics for 400 API responses
371d6d9  🐛 Fix Yosemite provider calendar navigation and response handling
```

## What Was Tried & What Happened

### Approach 1: Calendar UI Navigation (ABANDONED)
Tried to find `<select>` elements for month/year on the calendar widget and use `select_option()` to navigate. **Discovery: No such selects exist on the page.** The page only has form selects (ProductSelection, Adults, Children, UnitCount). Calendar is purely JS-driven with no exposed controls.

### Approach 2: Route Interception (CURRENT)
Intercept the outgoing API request and surgically rewrite the `StartDate`/`EndDate` URL parameters via regex.

**Iteration history:**
1. **`parse_qs`/`urlencode` round-trip** → Corrupted the JSONP callback and reCAPTCHA token → **500 error**
2. **`Date.toString()` format** → Full datetime string with timezone — server rejected → **500 error**  
3. **`Date.toDateString()` format** → Correct format ("Wed Jul 01 2026") — but only rewrote `StartDate`, left `EndDate` at default (e.g., May 31) → **400 error** (start > end = invalid range)
4. **Rewrite both StartDate AND EndDate** → Commit `f80195d` — **UNTESTED**

### Diagnostic Finding
A passthrough (unmodified) request returns **200** — proving the headless browser is NOT being blocked. The issue was purely in how we were rewriting the URL parameters.

### Known reCAPTCHA Behavior  
~33% of first attempts get a **400** from reCAPTCHA scoring. Retry logic handles this (attempt 2+ typically succeeds). This is not a blocking issue.

## Current State & What's Broken

### The Code Has a Two-Phase Design (Intentionally Verbose for Debugging):
1. **Phase A (Passthrough)**: Let original request go unmodified, log the URL, check if default month matches target
2. **Phase B (Rewrite)**: Reload page, intercept and rewrite both StartDate and EndDate, capture response

### LATEST BLOCKER (as of most recent test session):
When we ran the test command:
```bash
camply campsites --provider YosemiteLodging --start-date 2026-07-06 --end-date 2026-07-07 --campground D --debug
```
The error was:
```
Page.wait_for_selector: Timeout 30000ms exceeded.
  waiting for locator("#box-widget_InitialProductSelection") to be visible
```

**The page's widget selector is not being found.** This could mean:
- The page structure has changed since earlier testing sessions
- A cookie consent banner / interstitial is blocking the page load
- The page loads differently in this environment vs the user's local machine
- The widget ID has changed

**This needs investigation before anything else.** The EndDate rewrite fix (commit `f80195d`) has never been tested because we can't get past page load.

## Immediate Next Steps

1. **Investigate why `#box-widget_InitialProductSelection` isn't found** — capture page HTML/screenshot after load to see what's actually rendered. Possible causes:
   - Page redesign (new widget IDs)
   - Cookie consent / privacy overlay blocking
   - Different page structure for headless browsers
   - Widget loaded in an iframe

2. **Once page loads**: Test whether the dual StartDate+EndDate rewrite returns 200

3. **If rewrite works**: Simplify the code — remove Phase A (passthrough), collapse to a single rewrite-only flow. The two-phase approach was only for debugging.

4. **Eventually**: Add `playwright` as an optional dependency in `pyproject.toml`

## Test Command
```bash
camply campsites --provider YosemiteLodging --start-date 2026-07-06 --end-date 2026-07-07 --campground D --debug
```

## Key Files to Read First
- `camply/providers/yosemite/yosemite_lodging.py` — the entire provider (512 lines)
- `camply/config/api_config.py` lines 207-258 — YosemiteConfig
- `camply/search/search_yosemite.py` — the search orchestrator (179 lines)
