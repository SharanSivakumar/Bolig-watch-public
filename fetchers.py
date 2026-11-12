"""Playwright-based fetch layer for BoligWatch v2.1.

Supports:
1. Lejebolig.dk — Greater Copenhagen scan with two-stage detail extraction.
2. BoligPortal.dk — JSON store hydration endpoint with pagination across apartments.

FindBolig was retired in v3.11 (spec 10 section 7).
"""
import datetime
import json
import re
from bs4 import BeautifulSoup

import config

UA = config.USER_AGENT


# ==============================================================================
# 0. Coverage evidence (spec 06 section 4.4)
# ==============================================================================
class SearchResult(list):
    """The cards a search returned, plus what the scan can prove about coverage.

    A plain list of records everywhere it is consumed, so every existing caller
    and fixture keeps working, with one extra attribute: ``areas``, a list of
    per-area/per-query reports. Before this, an area that raised was logged and
    skipped and read downstream exactly like an area that was genuinely empty,
    which is how four zero-card Lejebolig scans expired 68 live listings.
    """

    def __init__(self, items=(), areas=None):
        super().__init__(items)
        self.areas = list(areas or [])


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def area_report(area_or_query: str, status: str, cards_seen: int = 0,
                expected_cards=None, pages_loaded: int = 0, error=None,
                started_at=None, finished_at=None) -> dict:
    return {
        "area_or_query": area_or_query,
        "status": status,
        "cards_seen": int(cards_seen or 0),
        "expected_cards": expected_cards,
        "pages_loaded": int(pages_loaded or 0),
        "error": (str(error)[:500] if error else None),
        "started_at": started_at or _now(),
        "finished_at": finished_at or _now(),
    }


def has_no_results_marker(text, site) -> bool:
    """True when a page states in so many words that it found nothing.

    Zero cards is evidence about the scan, not about the market, unless the
    portal itself rendered its "no results" state. Without this marker a zero
    card count is ``empty-unconfirmed`` and expires nothing.
    """
    if not text:
        return False
    blob = " ".join(str(text).lower().split())
    return any(marker in blob for marker in
               getattr(config, "NO_RESULTS_MARKERS", {}).get(site, []))


def _page_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""

# Lejebolig area -> postal code hint for DAWA geocoding
AREA_POSTAL = {
    "Sydhavnen": "2450", "København SV": "2450",
    "Amager": "2300", "Amager Øst": "2300", "København S": "2300",
    "Indre By": "1200", "København K": "1000", "København V": "1600",
    "Vesterbro": "1600", "Østerbro": "2100", "København Ø": "2100",
    "Nørrebro": "2200", "København N": "2200", "Nordvest": "2400",
    "København NV": "2400", "Valby": "2500", "Vanløse": "2720",
    "Frederiksberg": "2000", "Frederiksberg C": "1870", "Nordhavn": "2150",
    "Hellerup": "2900", "Taastrup": "2630", "Brøndby": "2605",
    "Brøndby Strand": "2660", "Hvidovre": "2650", "Rødovre": "2610",
    "Kongens Lyngby": "2800", "Glostrup": "2600", "Albertslund": "2620",
    "Ishøj": "2635", "Kastrup": "2770", "Dragør": "2791",
    "Søborg": "2860", "Ballerup": "2750", "Herlev": "2730",
    "Vallensbæk": "2665", "Gentofte": "2820", "Charlottenlund": "2920",
}


def _new_context(browser):
    ctx = browser.new_context(
        user_agent=UA,
        locale=config.LOCALE,
        viewport={"width": 1366, "height": 900},
        extra_http_headers={"Accept-Language": "da-DK,da;q=0.9,en;q=0.8"},
        ignore_https_errors=True,
    )
    try:
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    except Exception:
        pass
    return ctx


def _goto(page, url, wait_ms=2500):
    page.goto(url, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_timeout(wait_ms)


def _dismiss_cookies(page):
    for sel in ["#declineButton", "#updateButton", "button:has-text('OK')",
                "button:has-text('Begrænset')", "#acceptConsent",
                "button:has-text('Accepter alle')", "button:has-text('Afvis alle')"]:
        try:
            if page.locator(sel).count() and page.locator(sel).first.is_visible():
                page.locator(sel).first.click(timeout=2500)
                page.wait_for_timeout(600)
                break
        except Exception:
            pass


# ==============================================================================
# 1. Lejebolig.dk Fetcher (Two-Stage)
# ==============================================================================
def classify_lejebolig_card(text):
    """Label a Lejebolig search card; never drop it.

    This used to be ``_is_candidate_lejebolig_card``, a gate: a card whose text
    did not contain one of four phrases was discarded before anything was
    stored, so a rule change could never recover it. Under spec 06 the same
    wording only produces a label, and every card is stored whatever it says.

    Returns one of apartment | room | student | senior | unknown.
    """
    t = (text or "").lower()
    if any(p in t for p in ("kollegie", "studiebolig", "ungdomsbolig")):
        return "student"
    if any(p in t for p in ("ældrebolig", "aeldrebolig", "seniorbolig", "ældreboliger")):
        return "senior"
    # A size-marked room ("14m² værelse på...") is unambiguous: the square-metre
    # figure quantifies the room, not an apartment's room count (spec 07 3.1).
    if re.search(r"\d+[.,]?\d*\s*m[²2]?\s*(?:stort\s+|stor\s+)?(?:v[æa]relse)", t):
        return "room"
    if any(p in t for p in ("værelse i ", "vaerelse i ", "værelse på ", "vaerelse paa ",
                            "værelse til leje", "vaerelse til leje", "værelse til udleje",
                            "room for rent", "room for rent in", "a room for rent",
                            "room in ", "room rental", "rent a room")):
        return "room"
    if ("lejlighed i " in t or "lejlighed på " in t or "studio" in t or "1-værelses" in t
            or "1-vaerelses" in t or "bolig i " in t or "apartment" in t
            or " lejlighed" in t):
        return "apartment"
    return "unknown"


def _card_to_listing_type(card_class: str) -> str:
    """Map a card classification to a listing_type (spec 07 section 6.2).

    Stop hardcoding every Lejebolig card as 'lejlighed'; the search feed is
    '/alle-vaerelser/' (all rooms), so a card's own class must drive the type.
    An ambiguous card stays 'ukendt', never 'lejlighed' by assumption.
    """
    if card_class == "apartment":
        return "lejlighed"
    if card_class == "room":
        return "værelse"
    return "ukendt"


def _lejebolig_cards(page, seen):
    """Read newly rendered cards, deduplicating image and text links."""
    best = {}
    items = page.query_selector_all("a[href*='/lejebolig/']")
    for el in items:
        try:
            href = el.get_attribute("href") or ""
            if "/lejebolig/" not in href:
                continue
            m = re.search(r"/lejebolig/(\d+)/", href)
            if not m:
                continue
            lid = m.group(1)
            if lid in seen:
                continue
            text = (el.inner_text() or "").strip()
            if not text:
                continue
            m2 = re.search(r"([\d.]+),-", text)
            if m2:
                price_raw = m2.group(1).replace(".", "")
                try:
                    price = int(price_raw)
                except Exception:
                    price = None
            else:
                price = None

            cc = classify_lejebolig_card(text)
            facts = {}
            try:
                for spec in el.query_selector_all(".lease-spec"):
                    icon, value = spec.query_selector("img"), spec.query_selector("span")
                    if not icon or not value:
                        continue
                    src, label = icon.get_attribute("src") or "", value.inner_text().strip()
                    if "areal" in src:
                        facts["size_m2"] = float(label.replace(",", "."))
                    elif "vaerelser" in src:
                        facts["rooms"] = int(label)
                    elif "periode" in src:
                        facts["rental_period"] = label
            except (AttributeError, TypeError, ValueError):
                pass
            rec = {
                "site": "lejebolig",
                "listing_id": lid,
                "url": "https://www.lejebolig.dk" + href if href.startswith("/") else href,
                "card_text": text,
                "card_price": price,
                "card_class": cc,
                "listing_type": _card_to_listing_type(cc), **facts,
            }
            # Image links can contain only "See pictures". Prefer the actual
            # information link for the same advert before recording its card.
            quality = (price is not None, len(facts), len(text))
            if lid not in best or quality > best[lid][0]:
                best[lid] = (quality, rec)
        except Exception:
            continue
    seen.update(best)
    return [value[1] for value in best.values()]


def lejebolig_search(page, area, max_pages=None):
    """Load every results batch; retain cards if a later batch fails.

    Exhaust the public show-more control instead of treating its first 39
    listings as an area's entire market. Incomplete pagination records failed
    area coverage, so it cannot expire listings that we did not reach.
    """
    started = _now()
    url = f"https://www.lejebolig.dk/lejeboliger/koebenhavn/alle-vaerelser/{area}"
    _goto(page, url)
    seen = set()
    results = _lejebolig_cards(page, seen)
    pages_loaded, error, expected = 1, None, None
    max_pages = config.LEJEBOLIG_MAX_PAGES if max_pages is None else max_pages
    count = page.locator(".result-count .item-count")
    if count.count():
        digits = re.sub(r"\D", "", count.first.inner_text())
        expected = int(digits) if digits else None

    try:
        more = page.locator("#search-more a[href*='/Hunter/MoreResultsV2']")
        if more.count():
            _dismiss_cookies(page)
        while more.count() and more.first.is_visible():
            # The site keeps paginating into "Matcher næsten dine ønsker"
            # after exhausting the chosen area. Those areas have their own
            # searches; stop here instead of repeatedly fetching suggestions.
            if page.locator("#search-items .popular-area-header").count():
                break
            if pages_loaded >= max_pages:
                error = f"pagination capped at {max_pages} batches while more results remain"
                break
            before = page.locator("a[href*='/lejebolig/']").count()
            with page.expect_response(lambda response: "/Hunter/MoreResultsV2" in response.url,
                                      timeout=15000) as pending:
                # Scrolling this button into view also triggers the site's
                # auto-loader. Dispatch its click without scrolling to avoid
                # issuing two pagination requests for the same batch.
                more.first.dispatch_event("click")
            response = pending.value
            if not response.ok:
                error = f"pagination returned HTTP {response.status}"
                break
            pages_loaded += 1
            page.wait_for_function("""before =>
                document.querySelectorAll("a[href*='/lejebolig/']").length > before
                || document.querySelector('#search-items > div.last') !== null
                """, arg=before, timeout=15000)
            new_cards = _lejebolig_cards(page, seen)
            results.extend(new_cards)
            if not new_cards and more.count() and more.first.is_visible():
                error = "pagination made no progress while more results remain"
                break
    except Exception as exc:
        # Already discovered cards are useful; failed pagination is never
        # evidence that unseen listings have disappeared.
        results.extend(_lejebolig_cards(page, seen))
        error = f"pagination failed: {exc}"

    if error:
        status = "failed"
    elif results:
        status = "ok"
    elif has_no_results_marker(_page_text(page), "lejebolig"):
        status = "empty-confirmed"
    else:
        status = "empty-unconfirmed"
    return SearchResult(results, [area_report(area, status, cards_seen=len(results),
                                              expected_cards=expected,
                                              pages_loaded=pages_loaded, error=error,
                                              started_at=started)])


def lejebolig_detail(page, rec):
    """Stage 2: Open detail page, extract full financials, address, rooms, and description."""
    try:
        _goto(page, rec["url"], wait_ms=1800)
        body = page.inner_text("body").replace("\u00a0", " ")
    except Exception:
        rec["eligibility_text"] = None
        rec["eligibility_source_url"] = rec.get("url")
        rec["eligibility_evidence_state"] = "failed"
        rec["eligibility_checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        return rec
    body = re.sub(r"\s+", " ", body)
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    rec["eligibility_source_url"] = rec.get("url")
    rec["eligibility_checked_at"] = checked_at
    rec["eligibility_text"] = body if len(body.strip()) >= 40 else None
    rec["eligibility_evidence_state"] = "verified" if rec["eligibility_text"] else "unavailable"

    # Spec 07 section 6.2: the detail page's 'Boligtype' label is authoritative.
    # It overwrites the weaker card guess (Lejebolig hardcoded every card as
    # 'lejlighed' before, which is how room rentals sailed through as flats).
    btm = re.search(r"Boligtype\s*:?\s*([A-Za-zÆØÅæøå]+)", body)
    if btm:
        boligtype = btm.group(1).strip().lower()
        excerpt = f"Boligtype {btm.group(1).strip()}"
        rec["unit_type_evidence_source"] = "detail"
        rec["unit_type_evidence_excerpt"] = excerpt[:200]
        if boligtype in ("værelse", "vaerelse"):
            rec["unit_type_state"] = "room_rental"
            rec["card_class"] = "room"
            rec["listing_type"] = "værelse"
        elif boligtype in ("lejlighed", "lejligheder", "lejemål"):
            rec["unit_type_state"] = "self_contained_apartment"
            rec["card_class"] = "apartment"
            rec["listing_type"] = "lejlighed"
        elif boligtype in ("hus", "villa", "rækkehus", "raekkehus",
                           "landejendom", "fritidshus", "erhverv"):
            rec["unit_type_state"] = "house_or_other"
            rec["card_class"] = "other"
            rec["listing_type"] = boligtype

    def grab_int(label):
        m = re.search(re.escape(label) + r"\s*:?\s*([\d.,]+)\s*(?:kr)?", body, re.IGNORECASE)
        if m:
            clean = m.group(1).replace(".", "").replace(",", "")
            try:
                return int(clean)
            except ValueError:
                return None
        return None

    rec["rent_base"] = grab_int("Husleje")
    rec["utilities_amount"] = grab_int("A/C-forbrug") or grab_int("A/C") or grab_int("Aconto")
    if rec.get("rent_base") is not None and rec.get("utilities_amount"):
        rec["rent_total"] = rec["rent_base"] + rec["utilities_amount"]
    elif rec.get("rent_base") is not None:
        rec["rent_total"] = rec["rent_base"]
    else:
        rec["rent_total"] = None

    rec["prepaid_rent"] = grab_int("Forudbetalt leje")
    rec["deposit"] = grab_int("Depositum")
    rec["move_in_total_explicit"] = grab_int("Indflytningspris")

    # Extract room count
    rm = re.search(r"(?:Antal\s+v[æa]relser|V[æa]relser)\s*:?\s*(\d+)", body, re.IGNORECASE)
    if rm:
        try:
            rec["rooms"] = int(rm.group(1))
        except ValueError:
            rec["rooms"] = None
    else:
        # Check title/body for 1-værelses
        if re.search(r"\b1-v[æa]relses\b|\b1\s+v[æa]relse\b|\bét-v[æa]relses\b|\bstudio\b", body, re.IGNORECASE):
            rec["rooms"] = 1
        elif re.search(r"\b2-v[æa]relses\b|\b2\s+v[æa]relser\b|\bto-v[æa]relses\b", body, re.IGNORECASE):
            rec["rooms"] = 2
        elif re.search(r"\b3-v[æa]relses\b|\b3\s+v[æa]relser\b", body, re.IGNORECASE):
            rec["rooms"] = 3
        else:
            rec["rooms"] = None

    # Extract size in m2
    sm = re.search(r"(?:Areal|St[øo]rrelse)\s*:?\s*(\d+)\s*m[²2]", body, re.IGNORECASE)
    if sm:
        try:
            rec["size_m2"] = float(sm.group(1))
        except ValueError:
            rec["size_m2"] = None

    title = (page.title() or "").strip()
    street = None
    area = None
    m = re.search(r"([A-ZÆØÅa-zæøå0-9 .'-]+), (Sydhavnen|København [A-ZÅÆØ]|Valby|Nørrebro|Østerbro|Vesterbro|Frederiksberg|Amager|Vanløse|Hellerup|Nordhavn|Taastrup|Hvidovre|Herlev)", body)
    if m:
        area = m.group(2).strip()
    if title and " - " in title:
        street = title.split(" - ")[0].strip()
    if not street and m:
        street = m.group(1).strip()
    if street:
        rec["address"] = street
        postm = re.search(
            r"\bi (\d{4})\s+([A-ZÆØÅ][A-Za-zæøåÆØÅ]+(?:\s+[A-ZÅÆØ])?)\b",
            " Boliger til leje i " + body)
        if postm:
            rec["_geo_address"] = f"{street}, {postm.group(1)} {postm.group(2).strip()}"
        else:
            pc = AREA_POSTAL.get(area) or AREA_POSTAL.get(street)
            rec["_geo_address"] = f"{street}, {pc} {area}" if pc else f"{street}, {area}"
    else:
        rec["_geo_address"] = rec.get("address")

    lm = re.search(r"Ledig\s*[:.]?\s*([A-Za-zÆØÅæøå0-9 .\-\/]+?)(?:\s+Indrykket|\s*$)", body)
    if lm:
        rec["available_from"] = lm.group(1).strip()
    else:
        am = re.search(r"Ledig\s*[:.]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", body)
        if am:
            rec["available_from"] = am.group(1)
        else:
            rec["available_from"] = None

    rpm = re.search(r"Lejeperiode\s*[:.]?\s*([A-Za-zÆØÅæøå0-9 \-\/\+]+?)(?:\s+Boligtype|\s+Område|\s+Areal|\s+Overtagelse|\s+Indrykket|\s+Husleje|\s*$)", body)
    if rpm:
        rec["rental_period"] = rpm.group(1).strip()
    elif "ubegrænset" in body.lower() or "tidsubegrænset" in body.lower():
        rec["rental_period"] = "ubegrænset"
    else:
        rec["rental_period"] = None  # Do not invent defaults!

    rec["description"] = body
    try:
        img = page.query_selector("img[src*='/bolig/'], img.property-image, .gallery img")
        if img:
            rec["photo_url"] = img.get_attribute("src")
    except Exception:
        pass

    return rec


# ==============================================================================
# 2. BoligPortal.dk Fetcher (JSON Store SRP Hydration)
# ==============================================================================
def _bp_type(category):
    c = (category or "").lower()
    if c == "rental_apartment" or "apartment" in c:
        return "lejlighed"
    if c == "rental_room" or "room" in c:
        return "værelse"
    if c == "rental_house" or "house" in c or c == "rental_villa":
        return "hus"
    return c or "ukendt"


def _bp_card_class(category):
    """apartment | room | student | senior | unknown, from the store category."""
    c = (category or "").lower()
    if "room" in c:
        return "room"
    if "dorm" in c or "kollegie" in c or "student" in c:
        return "student"
    if "senior" in c or "elderly" in c:
        return "senior"
    if "apartment" in c or "villa" in c or "house" in c or "townhouse" in c:
        return "apartment" if "apartment" in c else "unknown"
    return "unknown"


def _bp_listing_id(r):
    """The stable id for a store-JSON result. Explicit None checks: 0 is an id."""
    for key in ("rentable_id", "id"):
        val = r.get(key)
        if val is not None and str(val) != "":
            return str(val)
    return None


def _bp_canonical_url(value):
    value = str(value or "")
    return value if value.startswith("http://") or value.startswith("https://") \
        else "https://www.boligportal.dk" + value


def _bp_parse_result(r, now):
    """One BoligPortal store-JSON result into a BoligWatch record, or None."""
    lid = _bp_listing_id(r)
    if not lid:
        return None

    street = (r.get("street_name") or "").strip()
    num = (r.get("street_number") or "").strip()
    addr = f"{street} {num}".strip()
    pcode = r.get("postal_code")
    # The search API's city is often the broad København search taxonomy; its
    # city_area is the locality (Vanløse, København S, etc.). Never invent one.
    locality = (r.get("city_area") or "").strip()
    addr_full = f"{addr}, {pcode} {locality}".strip() if (addr and pcode) else addr

    rp = r.get("rental_period")
    if rp == 0:
        rp_display = "ubegrænset"
    elif isinstance(rp, int) and rp > 0:
        rp_display = f"{rp} mdr (tidsbegrænset)"
    elif rp is not None:
        rp_display = str(rp)
    else:
        rp_display = None

    rent_base = r.get("monthly_rent")
    utils = r.get("monthly_rent_extra_costs")
    rent_total = ((rent_base or 0) + (utils or 0)
                  if (rent_base is not None or utils is not None) else None)

    return {
        "site": "boligportal",
        "listing_id": lid,
        "title": r.get("title") or f"Lejlighed på {addr}",
        "address": addr_full,
        "_geo_address": f"{addr}, {pcode}" if (addr and pcode) else addr,
        "postcode": str(pcode) if pcode else None,
        "listing_type": _bp_type(r.get("category")),
        "card_class": _bp_card_class(r.get("category")),
        "rooms": r.get("rooms"),
        "size_m2": r.get("size_m2"),
        "rent_base": rent_base,
        "utilities_amount": utils,
        "mandatory_monthly_fees": None,
        "rent_total": rent_total,
        "prepaid_rent": r.get("prepaid_rent"),
        "deposit": r.get("deposit"),
        "move_in_total_explicit": None,
        "available_from": r.get("available_from"),
        "rental_period": rp_display,
        "rental_period_raw": rp,
        "url": _bp_canonical_url(r.get("url")),
        "photo_url": (r.get("images") or [{}])[0].get("url") if r.get("images") else None,
        "social_housing": r.get("social_housing"),
        "description": r.get("description") or (r.get("title") or ""),
        # Search-card values are discovery data only. run_boligportal replaces
        # these with authoritative detail-page evidence before evaluation.
        "source_record": dict(r),
        "eligibility_text": None,
        "eligibility_source_url": _bp_canonical_url(r.get("url")),
        "eligibility_evidence_state": "unavailable",
        "eligibility_checked_at": None,
        "first_seen_at": now,
    }


def _bp_detail_hydration(page):
    """Return the raw detail hydration object when BoligPortal exposes one."""
    try:
        value = page.evaluate("""() => {
            const nodes = [document.querySelector('script#store'),
                           document.querySelector('script#__NEXT_DATA__')];
            for (const node of nodes) {
                if (!node || !node.textContent) continue;
                try { return JSON.parse(node.textContent); } catch (_) {}
            }
            return null;
        }""")
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def boligportal_detail(page, rec):
    """Enrich a search-card record with canonical detail-page evidence.

    A navigation/parsing error is a retryable ``failed`` state. A readable page
    without tenancy-eligibility text is ``unavailable`` and is never ranked.
    """
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    rec["eligibility_source_url"] = rec.get("url")
    rec["eligibility_checked_at"] = checked_at
    try:
        _goto(page, rec["url"], wait_ms=1800)
        body = page.inner_text("body").replace("\u00a0", " ")
        body = re.sub(r"\s+", " ", body).strip()
        hydration = _bp_detail_hydration(page)
        if hydration is not None:
            rec["detail_hydration"] = hydration
        # BoligPortal detail pages generally include a readable description even
        # when the search card does not. Keep it separate from display copy.
        structured_text = ""
        if hydration is not None:
            structured_text = re.sub(r"\s+", " ",
                                     json.dumps(hydration, ensure_ascii=False))
        structured_has_eligibility = bool(re.search(
            r"eligib|tenan|lease|målgruppe|studie|student|lejeperiode|"
            r"ubegrænset|tidsbegrænset|boligtype|værelse|køkken|badeværelse",
            structured_text, re.IGNORECASE))
        if len(body) < 40 and len(structured_text) >= 40 and structured_has_eligibility:
            rec["eligibility_text"] = structured_text
            rec["eligibility_evidence_state"] = "verified"
            return rec
        if len(body) < 40:
            rec["eligibility_text"] = None
            rec["eligibility_evidence_state"] = "unavailable"
            return rec
        rec["eligibility_text"] = body
        rec["eligibility_evidence_state"] = "verified"
        return rec
    except Exception:
        rec["eligibility_text"] = None
        rec["eligibility_evidence_state"] = "failed"
        return rec


def _bp_page(page, base_url, offset):
    """One page of the BoligPortal store JSON.

    Raises on a parse failure. The pager used to swallow this and return None,
    which is indistinguishable from "this query has no more results"; spec 06
    requires a failed page to be recorded as failed coverage.
    """
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}offset={offset}"
    _goto(page, url, wait_ms=2000)
    _dismiss_cookies(page)
    data = page.evaluate(
        "() => JSON.parse(document.querySelector('script#store').textContent)")
    return (data.get("props", {}) or {}).get("page_props", {}) or {}


def boligportal_search(page, max_pages=None, search_urls=None):
    """Every BoligPortal card in Greater Copenhagen up to the discovery ceiling.

    Pages each configured search URL until a page comes back empty or the
    result_count is exhausted, reading the page size from the response rather
    than assuming 18. Deduplicated across URLs by rentable_id.

    v3.5: no card is dropped here. Three-room flats used to be discarded before
    storage because there is no rooms query parameter; they are now stored and
    the detail envelope records ``skipped:rooms`` instead, so allowing a second
    room later is a local decision rather than a re-scan. Each search URL
    produces a coverage report on the returned ``SearchResult``.
    """
    max_pages = config.BP_MAX_PAGES if max_pages is None else max_pages
    urls = search_urls if search_urls is not None else config.BOLIGPORTAL_SEARCH_URLS
    out = []
    areas = []
    seen_ids = set()
    now = datetime.datetime.now().isoformat(timespec="seconds")

    for base_url in urls:
        started = _now()
        offset, pages, total = 0, 0, None
        # Cards this query returned, including ones an earlier query already
        # produced. Coverage is about what the page showed us, not about how
        # much of it was new: the bbox URL overlaps the city URL almost
        # completely, and counting only new ids made a fully-loaded 374-result
        # query report itself as empty.
        seen_here = 0
        query_ids = set()
        kept_here = 0
        error = None
        while pages < max_pages:
            try:
                pp = _bp_page(page, base_url, offset)
            except Exception as e:
                error = f"store parse failed at offset {offset}: {e}"
                if config.VERBOSE:
                    print(f"  [boligportal] {error}")
                break
            results = pp.get("results") or []
            if total is None:
                total = pp.get("result_count")
            if not results:
                if total is not None and offset < total:
                    error = f"empty page at offset {offset} before result_count {total}"
                break
            page_size = pp.get("limit") or len(results) or config.BP_PAGE_SIZE_FALLBACK

            new_here = 0
            for r in results:
                lid = _bp_listing_id(r)
                if not lid:
                    continue
                if lid not in query_ids:
                    query_ids.add(lid)
                    seen_here += 1
                    new_here += 1
                if lid in seen_ids:
                    continue
                seen_ids.add(lid)
                rec = _bp_parse_result(r, now)
                if rec:
                    out.append(rec)
                    kept_here += 1

            pages += 1
            if not new_here:
                error = f"pagination repeated listings at offset {offset}"
                break
            offset += page_size
            if total is not None and offset >= total:
                break
            if total is None and len(results) < page_size:
                break

        if not error and pages >= max_pages and (total is None or offset < total):
            error = f"pagination capped at {max_pages} pages before exhausting results"

        if error:
            status = "failed"
        elif seen_here:
            status = "ok"
        elif has_no_results_marker(_page_text(page), "boligportal") or total == 0:
            status = "empty-confirmed"
        else:
            status = "empty-unconfirmed"
        areas.append(area_report(base_url, status, cards_seen=seen_here,
                                 expected_cards=total, pages_loaded=pages,
                                 error=error, started_at=started))

        if config.VERBOSE:
            print(f"  [boligportal] {base_url.split('?')[0]}: {pages} pages, "
                  f"result_count {total}, status {status}, {seen_here} cards returned "
                  f"({kept_here} new), {len(out)} cards so far")

    return SearchResult(out, areas)
