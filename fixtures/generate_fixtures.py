"""generate_fixtures.py — build the synthetic OM corpus and its ground truth.

Fifteen small Offering Memoranda, each deliberately encoding one trap that real
brokerage OMs contain. All data is invented: the properties, tenants, addresses,
parcel numbers and contacts do not exist. Nothing here is derived from a real
document, and nothing in this repository should ever be.

Run it:
    python fixtures/generate_fixtures.py

Writes fixtures/pdfs/*.pdf and fixtures/ground_truth.json.

TWO THINGS THAT MUST NOT CHANGE
-------------------------------
1. The build loop stays behind `if __name__ == "__main__"`. Anything at module
   scope that mutates state turns a bare `import generate_fixtures` — the obvious
   way to reuse draw_row() — into a silent rewrite of every tracked PDF.
2. The scanned fixture's table keeps its ruling lines and stays inside the page.
   Space-padded columns make OCR insert characters AND stop table detection
   entirely; a table running past the page edge gets its last column transcribed
   as clipped glyphs. Both look exactly like OCR bugs and are not.
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

HERE = Path(__file__).parent
PDF_DIR = HERE / "pdfs"
W, H = letter

MONO, MONO_B = "Courier", "Courier-Bold"
SANS, SANS_B = "Helvetica", "Helvetica-Bold"
TSIZE = 8

# Default rent roll column origins, in points from the left edge.
# The tenant column is 205pt wide — about 42 monospace characters at 8pt. Several
# fixture tenants are deliberately longer than that.
XS = [45, 250, 320, 375, 440, 505, 565]
HEADERS = ["TENANT", "RSF", "%", "LED", "BASE RENT", "RENT", "% MKT"]
GROUPS = [(440, "IN-PLACE"), (505, "MARKET")]


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------
def draw_row(c, y, cells, xs, font=MONO, size=TSIZE):
    """Draw one table row, letting long cells push their neighbours right.

    This is the single most important behaviour in the generator. A cell is drawn
    at its column origin UNLESS the previous cell already ran past it, in which
    case it is drawn one space after whatever came before.

    That is exactly what a real designed rent roll does: rows with short tenant
    names come out neatly column-aligned, and the one row whose tenant is
    "Northstar Bioscience Partners, LLLP" collapses its gaps and lands its figures
    wherever they fit. Both forms appear in the same table, which is why an
    extractor cannot rely on horizontal position and has to read by column order
    relative to a stable anchor.
    """
    c.setFont(font, size)
    space = stringWidth(" ", font, size)
    cursor = 0.0
    for i, cell in enumerate(cells):
        text = "" if cell is None else str(cell)
        if not text:
            cursor = max(cursor, xs[i])
            continue
        x = max(xs[i], cursor + space) if cursor else xs[i]
        c.drawString(x, y, text)
        cursor = x + stringWidth(text, font, size)
    return y


def running_header(c, section, prop_name):
    c.setFont(SANS, 7)
    c.drawString(45, H - 34, section.upper())
    c.drawRightString(W - 45, H - 34, prop_name.upper())


def cover_page(c, f):
    c.setFont(SANS_B, 26)
    c.drawString(60, H - 200, f["property_name"])
    c.setFont(SANS, 12)
    c.drawString(60, H - 226, f["city_state"])
    c.setFont(SANS, 10)
    c.drawString(60, H - 258, "CONFIDENTIAL OFFERING MEMORANDUM")
    if f.get("contacts"):
        c.setFont(SANS, 9)
        c.drawString(60, H - 300, f"Presented by: {f['contacts'][0]}")
        if len(f["contacts"]) > 1:
            c.drawString(60, H - 314,
                         f"For more information, please contact: {f['contacts'][1]}")
    c.showPage()


def details_page(c, f, parcel_value=None):
    """Property Details — the labelled key-value block the regex pass reads.

    Kept single-column on purpose. Real OMs set this as two columns, which the
    collapsed text extractor interleaves onto one line; handling that is a
    documented limitation rather than something these fixtures pretend away.
    """
    running_header(c, "Property Overview", f["property_name"])
    c.setFont(SANS_B, 16)
    c.drawString(45, H - 80, "PROPERTY DETAILS")

    rows = [
        ("Property Name", f["property_name"]),
        ("Address", f["address"]),
        ("County", f["county"]),
        ("Year Built/Renovated", f["year_built"]),
        ("Rentable Square Footage", f["total_rsf"]),
        ("Floors", f["floors"]),
    ]
    if parcel_value:
        rows.insert(3, ("Property Tax Parcel ID Number", parcel_value))

    y = H - 120
    for label, value in rows:
        c.setFont(MONO, 9)
        c.drawString(45, y, f"{label} {value}")
        y -= 18
    c.showPage()


def rentroll_page(c, f, rows, headers=None, groups=None, xs=None,
                  heading="CORE RENT ROLL", subheading="TENANT SUMMARY",
                  chrome_after=None, trailing=None, parcel_value=None):
    """One rent roll page.

    `chrome_after` maps a row index to a line of graphic-design chrome printed
    between table rows — pull quotes and stat callouts that a designed OM
    interleaves with the table. The region slicer has to survive them.
    """
    headers = headers or HEADERS
    groups = groups if groups is not None else GROUPS
    xs = xs or XS

    running_header(c, "Tenant Profile", f["property_name"])
    c.setFont(SANS_B, 18)
    # Letter-spaced, the way a designed OM sets a section heading. The start
    # pattern has to tolerate wide gaps between the words.
    c.drawString(45, H - 80, "  ".join(heading.split()))
    c.setFont(SANS, 11)
    c.drawString(45, H - 100, subheading)

    y = H - 140
    if groups:
        c.setFont(MONO_B, TSIZE)
        for gx, glabel in groups:
            c.drawString(gx, y, glabel)
        y -= 11
    draw_row(c, y, headers, xs, font=MONO_B)
    y -= 14

    for i, cells in enumerate(rows):
        draw_row(c, y, cells, xs)
        y -= 12
        if chrome_after and i in chrome_after:
            c.setFont(SANS_B, 9)
            c.drawString(45, y, chrome_after[i])
            y -= 12

    if trailing:
        y -= 10
        c.setFont(SANS, 8)
        for line in trailing:
            c.drawString(45, y, line)
            y -= 11

    if parcel_value:
        c.setFont(SANS, 6)
        c.drawString(45, 40, f"Tax Parcel ID Number {parcel_value}")
    c.showPage()


def prose_page(c, f, title, paragraphs):
    running_header(c, "Market Overview", f["property_name"])
    c.setFont(SANS_B, 16)
    c.drawString(45, H - 80, title)
    c.setFont(SANS, 9)
    y = H - 110
    for p in paragraphs:
        c.drawString(45, y, p)
        y -= 13
    c.showPage()


# --------------------------------------------------------------------------
# scanned page
# --------------------------------------------------------------------------
def _mono_font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
              "/System/Library/Fonts/Menlo.ttc",
              "/Library/Fonts/Courier New.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def scanned_cover_page(c, f):
    """An image-only cover, so the document's text fraction is 0 and triage
    classifies it `scanned` rather than `hybrid`."""
    SW, SH = 1700, 2200
    img = Image.new("RGB", (SW, SH), "white")
    d = ImageDraw.Draw(img)
    d.text((150, 700), f["property_name"], font=_mono_font(80), fill="black")
    d.text((150, 820), f["city_state"], font=_mono_font(44), fill="black")
    d.text((150, 900), "CONFIDENTIAL OFFERING MEMORANDUM",
           font=_mono_font(34), fill="black")
    c.drawImage(ImageReader(img), 0, 0, width=W, height=H)
    c.showPage()


def scanned_rentroll_page(c, f, rows, headers=None):
    """Rasterize a real, RULED rent roll table and embed it as an image-only page.

    Ruling lines are not decoration here. Without them the layout model has no
    table to detect, the OCR output degrades to one cell per line, and the region
    slicer gets nothing it can parse into rows. The table also has to finish
    inside the page: a column running off the right edge is transcribed from
    clipped glyphs and produces values like "4,7!" that look like an OCR failure
    and are actually a fixture bug.
    """
    headers = headers or ["TENANT", "RSF", "%", "LED", "IN-PLACE", "MARKET"]
    SW, SH, DPI = 1700, 2200, 200
    img = Image.new("RGB", (SW, SH), "white")
    d = ImageDraw.Draw(img)

    title_f, head_f, cell_f = _mono_font(46), _mono_font(30), _mono_font(28)
    d.text((90, 90), "CORE RENT ROLL", font=title_f, fill="black")
    d.text((90, 160), "TENANT SUMMARY", font=head_f, fill="black")

    col_w = [620, 200, 150, 190, 200, 200]
    x0, y0, rh = 90, 260, 56
    assert x0 + sum(col_w) < SW, "table must stay inside the page"

    ncols, nrows = len(col_w), len(rows) + 1
    xs, acc = [], x0
    for w in col_w:
        xs.append(acc)
        acc += w
    x1, y1 = acc, y0 + nrows * rh

    for i in range(nrows + 1):                       # horizontal rules
        d.line([(x0, y0 + i * rh), (x1, y0 + i * rh)], fill="black", width=3)
    for i in range(ncols + 1):                       # vertical rules
        x = xs[i] if i < ncols else x1
        d.line([(x, y0), (x, y1)], fill="black", width=3)

    for i, h in enumerate(headers):
        d.text((xs[i] + 12, y0 + 12), h, font=head_f, fill="black")
    for r, cells in enumerate(rows, start=1):
        for i, cell in enumerate(cells):
            d.text((xs[i] + 12, y0 + r * rh + 12), str(cell or ""),
                   font=cell_f, fill="black")

    d.text((90, y1 + 70), f"Property Tax Parcel ID Number {f['parcel']}",
           font=cell_f, fill="black")

    c.drawImage(ImageReader(img), 0, 0, width=W, height=H)
    c.showPage()


# --------------------------------------------------------------------------
# tenant helpers
# --------------------------------------------------------------------------
def T(name, sf, pct, led, inplace, market, suite=None):
    return {"tenant_name": name, "suite": suite, "rentable_sf": sf,
            "pct_of_building": pct, "lease_expiration": led,
            "in_place_base_rent": inplace, "market_rent": market}


def variance(t):
    """The '% MKT' column: in-place rent against market, as a signed percent.

    A distractor by design. It is a percent sitting to the RIGHT of the rents,
    while the real percent-of-building column sits to the LEFT of the term — so
    an extractor that grabs "the percent on this row" gets the wrong one on every
    single line, and the mistake looks entirely plausible in the output.
    """
    ip, mk = t.get("in_place_base_rent"), t.get("market_rent")
    if not ip or not mk:
        return ""
    return f"{round((float(ip) - float(mk)) / float(mk) * 100):+d}%".replace("+", "")


def cells(t, dollar=False):
    def m(v):
        return ("$" + v) if (v and dollar) else (v or "")
    return [t["tenant_name"], t["rentable_sf"], t["pct_of_building"] or "",
            t["lease_expiration"] or "", m(t["in_place_base_rent"]),
            m(t["market_rent"]), variance(t)]


# --------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------
BASE = {
    "city_state": "Riverton, OH",
    "county": "Calder County",
    "year_built": "1998/2014",
    "floors": "12",
    "contacts": ["Marguerite Okonkwo", "Dev Ramaswamy"],
}


def fixtures():
    """Return the fixture definitions. Pure data — safe to import."""
    F = []

    # -- F1: parcel number repeated on every page -> must dedupe --------------
    F.append(dict(BASE, key="F1", filename="Offering Memorandum - 2026-02-17 - Final.pdf",
                  property_name="Calder Point", address="1400 Beaumont Avenue",
                  total_rsf="318,400", parcel="0341207700", parcel_everywhere=True,
                  version="Final", trap="parcel repeated on every page -> dedupe",
                  tenants=[
                      T("Halcyon Freight Systems", "48,200", "15.1%", "Dec-28", "24.10", "29.00"),
                      T("Bright Meridian Analytics", "31,455", "9.9%", "Sep-27", "26.75", "29.00"),
                      T("Kestrel Dental Group", "12,880", "4.0%", "Mar-26", "22.40", "29.00"),
                  ]))

    # -- F2: no parcel number at all; listing contacts only -------------------
    F.append(dict(BASE, key="F2", filename="Offering Memorandum - 2025-11-11.pdf",
                  property_name="Ironwood Exchange", address="90 Fenmore Street",
                  total_rsf="204,900", parcel=None, version=None,
                  trap="no parcel number anywhere; contact block is the only evidence",
                  tenants=[
                      T("Sable Ridge Insurance Services", "27,300", "13.3%", "Jun-29", "21.00", "25.50"),
                      T("Overland Title Company", "15,120", "7.4%", "Jan-27", "19.85", "25.50"),
                  ]))

    # -- F3: one tenant, two suites, two rows -> never merge ------------------
    F.append(dict(BASE, key="F3", filename="Offering Memorandum - 2026-05-04 - Revised.pdf",
                  property_name="Halstead Tower", address="2 Corvin Plaza",
                  total_rsf="455,000", parcel="118-224-0093", version="Revised",
                  trap="one tenant across two suites -> two rows, never merged",
                  tenants=[
                      T("Pemberton Wexler & Crane LLP", "34,220", "7.5%", "Nov-30", "31.05", "34.00"),
                      T("Pemberton Wexler & Crane LLP", "18,640", "4.1%", "Nov-27", "29.50", "34.00"),
                      T("Ardent Peak Capital", "22,010", "4.8%", "Aug-28", "32.75", "34.00"),
                  ]))

    # -- F4: scanned, image-only, real rasterized text -> OCR branch ----------
    F.append(dict(BASE, key="F4", filename="Offering Memorandum - 2024-09-30 - SCAN.pdf",
                  property_name="Weatherfield Commons", address="775 Aldergate Road",
                  total_rsf="188,300", parcel="55-1120-0447", version=None,
                  scanned=True, trap="image-only pages, real rasterized ruled table",
                  tenants=[
                      T("Corvid Logistics LLC", "26,400", "14.0%", "Jul-27", "18.25", "22.00"),
                      T("Fairholme Pediatrics", "11,900", "6.3%", "Feb-26", "20.10", "22.00"),
                      T("Talon Print Works", "9,750", "5.2%", "Oct-25", "16.80", "22.00"),
                  ]))

    # -- F5: two distinct parcel numbers -> flag, never pick ------------------
    F.append(dict(BASE, key="F5", filename="Offering Memorandum - 2026-06-15 - Draft.pdf",
                  property_name="Braddock Yards", address="410 Wicklow Lane",
                  total_rsf="271,600", parcel="0662104400", second_parcel="0662104500",
                  version="Draft",
                  trap="two distinct parcel numbers -> flag for review, never pick one",
                  tenants=[
                      T("Grantham Medical Supply", "33,050", "12.2%", "Apr-29", "23.60", "27.00"),
                      T("Lyra Systems Group", "20,415", "7.5%", "Dec-26", "25.30", "27.00"),
                  ]))

    # -- F6: m.d.yyyy filename date ------------------------------------------
    F.append(dict(BASE, key="F6", filename="Offering Memorandum 3.9.2026 Final.pdf",
                  property_name="Sutter Grove", address="1801 Ellery Court",
                  total_rsf="149,750", parcel="7734009100", version="Final",
                  trap="m.d.yyyy filename date format",
                  tenants=[
                      T("Westbrook Culinary Holdings", "19,600", "13.1%", "May-28", "20.75", "24.00"),
                      T("Ninth Street Physical Therapy", "8,240", "5.5%", "Sep-26", "18.90", "24.00"),
                  ]))

    # -- F7: month-year filename + full label drift --------------------------
    F.append(dict(BASE, key="F7", filename="Offering Memorandum Sept 2019.pdf",
                  property_name="Verity Center", address="66 Halloway Street",
                  total_rsf="392,800", parcel="0219883000", version=None,
                  headers=["OCCUPANT", "RENTABLE SF", "% OF BLDG", "EXPIRES",
                           "ANNUAL BASE RENT", "MARKET", "VAR"],
                  groups=[],
                  trap="'Sept 2019' filename (month precision only) + label drift",
                  tenants=[
                      T("Ashgrove Reinsurance Company", "41,300", "10.5%", "Dec-24", "27.15", "31.00"),
                      T("Blackwater Software, Inc.", "36,905", "9.4%", "Aug-23", "25.80", "31.00"),
                      T("Ivory Lane Staffing", "14,220", "3.6%", "Mar-22", "24.05", "31.00"),
                  ]))

    # -- F8: $-prefixed money, "Sq. Ft." labels ------------------------------
    F.append(dict(BASE, key="F8", filename="Offering Memorandum - 2025-03-18 - Updated.pdf",
                  property_name="Redmond Arch", address="333 Pallister Way",
                  total_rsf="226,450", parcel="41-0937-2210", version="Updated",
                  dollar=True,
                  headers=["TENANT", "SQ. FT.", "SHARE", "LEASE END",
                           "IN-PLACE", "ASKING", "DELTA"],
                  groups=[],
                  trap="$-prefixed money and Sq. Ft. / Lease End labels",
                  tenants=[
                      T("Marchetti Fine Foods", "29,800", "13.2%", "Jul-28", "22.95", "26.50"),
                      T("Quill & Harrow Advisors", "17,340", "7.7%", "Nov-26", "24.60", "26.50"),
                  ]))

    # -- F9: distractor columns after the real ones --------------------------
    F.append(dict(BASE, key="F9", filename="Offering Memorandum - 2026-01-27 - Final.pdf",
                  property_name="Northgate Union", address="5 Trellis Boulevard",
                  total_rsf="303,100", parcel="0885532100", version="Final",
                  headers=["TENANT", "RSF", "%", "LED", "BASE RENT", "MKT RENT",
                           "ESCAL", "SEC DEP"],
                  groups=[], xs=[45, 232, 292, 340, 400, 458, 516, 562],
                  extra=["3.0%", "45,000"],
                  trap="Escalation and Security Deposit distractor columns",
                  tenants=[
                      T("Caldwell Orthopedic Partners", "38,700", "12.8%", "Feb-30", "28.40", "31.50"),
                      T("Tessellate Design Studio", "13,905", "4.6%", "Jun-27", "26.15", "31.50"),
                  ]))

    # -- F10: distractor columns bracketing the real ones --------------------
    F.append(dict(BASE, key="F10", filename="Offering Memorandum - 2025-09-09 - Final.pdf",
                  property_name="Pinecrest Landing", address="1220 Aveline Drive",
                  total_rsf="167,900", parcel="63-2214-0088", version="Final",
                  headers=["TENANT", "RSF", "%", "TI ALLOW", "LED", "BASE RENT",
                           "MKT RENT", "RENT/MO"],
                  groups=[], xs=[45, 215, 272, 318, 380, 432, 492, 548],
                  bracket=True,
                  trap="TI Allowance before the term and Rent/Month after the rents",
                  tenants=[
                      T("Solstice Bakery Group", "21,450", "12.8%", "Oct-28", "19.30", "23.00"),
                      T("Harrowgate Veterinary", "9,880", "5.9%", "Apr-26", "21.05", "23.00"),
                  ]))

    # -- F11: six subtotal / summary rows that must NOT be extracted ---------
    F.append(dict(BASE, key="F11", filename="Offering Memorandum - 2026-04-06 - Final.pdf",
                  property_name="Ellsworth Center", address="900 Kingsbridge Road",
                  total_rsf="486,300", parcel="0410889200", version="Final",
                  trap="eight subtotal/summary rows, one carrying a full set of figures",
                  tenants=[
                      T("Fairmont Ridge Bank", "52,180", "10.7%", "Dec-26", "25.40", "31.00"),
                      T("Delacroix Media Publishing LLC", "48,915", "10.1%", "Sep-24", "21.85", "31.00"),
                      T("Sandoval Hospitality Services, LLC", "41,260", "8.5%", "Apr-21", "23.75", "31.00"),
                      T("Whitlock and Pryce, LLP", "35,404", "7.3%", "Dec-21", "27.30", "34.00"),
                  ],
                  # Printed in the table, and none of them is a tenant. The
                  # TOTAL OFFICE LEASED line is the dangerous one: it carries a
                  # complete set of figures, so nothing structural separates it
                  # from the four rows above.
                  decoys=[
                      ["Tenants <15K RSF", "96,470", "19.8%", "", "", "", ""],
                      ["Leased Storage", "5,120", "1.1%", "", "", "", ""],
                      ["Amenities", "4,845", "1.0%", "", "", "", ""],
                      ["Antenna", "1,205", "0.2%", "", "", "", ""],
                      ["TOTAL OFFICE LEASED", "431,700", "88.8%", "Jan-25", "26.15", "32.40", "-19%"],
                      ["BOMA Adjustment", "4,260", "0.9%", "", "", "", ""],
                      ["TOTAL LEASED", "435,960", "89.6%", "", "", "", ""],
                      ["Available", "50,340", "10.4%", "", "", "", ""],
                  ]))

    # -- F12: blank cells -> null --------------------------------------------
    F.append(dict(BASE, key="F12", filename="Offering Memorandum - 2025-05-13 - Preliminary.pdf",
                  property_name="Marrow Hill Plaza", address="47 Tenby Avenue",
                  total_rsf="132,600", parcel="0117740500", version="Preliminary",
                  trap="blank expiration and blank market rent -> null, not guessed",
                  tenants=[
                      T("Ambergris Legal Services", "18,900", "14.3%", None, "23.75", "27.00"),
                      T("Foxglove Interiors", "11,240", "8.5%", "Aug-27", "21.60", None),
                      T("Hollis Brothers Hardware", "7,505", "5.7%", "MTM", None, "27.00"),
                  ]))

    # -- F13: tenant names wrapping to a second line -------------------------
    F.append(dict(BASE, key="F13", filename="Offering Memorandum - 2026-07-01 - Final.pdf",
                  property_name="Thornbury Works", address="18 Carrickmore Street",
                  total_rsf="358,200", parcel="0993317700", version="Final",
                  wraps={0: "Building C", 2: "and Trust"},
                  trap="tenant names wrapping onto a second line with no figures",
                  tenants=[
                      T("Aldermere Industrial Commerce Center Building C", "44,180", "12.3%", "Jan-31", "17.95", "21.00"),
                      T("Verano Coffee Roasters", "10,660", "3.0%", "Jun-27", "19.40", "21.00"),
                      T("Kettleridge Savings and Trust", "26,300", "7.3%", "Mar-29", "18.80", "21.00"),
                  ]))

    # -- F14: ten tenants, table split across two pages ----------------------
    F.append(dict(BASE, key="F14", filename="Offering Memorandum - 2026-08-19 - Final.pdf",
                  property_name="Ravensworth Campus", address="7000 Lindale Parkway",
                  total_rsf="612,300", parcel="0774411900", version="Final",
                  split_after=5,
                  trap="table split across two pages under a repeated heading",
                  tenants=[
                      T("Beacon Hollow Laboratories", "58,400", "9.5%", "Dec-31", "29.90", "33.00"),
                      T("Marisol Pharmaceutical Distribution Group", "47,220", "7.7%", "Sep-30", "28.15", "33.00"),
                      T("Ridgeline Actuarial", "31,050", "5.1%", "Jul-28", "30.40", "33.00"),
                      T("Copperfield Trust Company", "28,775", "4.7%", "Feb-29", "31.20", "33.00"),
                      T("Alcott & Fenn Architects", "22,600", "3.7%", "May-27", "27.85", "33.00"),
                      T("Duskwood Robotics", "19,340", "3.2%", "Nov-30", "32.05", "35.00"),
                      T("Pilcher Environmental Testing", "16,880", "2.8%", "Aug-26", "26.70", "35.00"),
                      T("Saltmarsh Publishing House", "14,215", "2.3%", "Apr-28", "25.95", "35.00"),
                      T("Orwell Lane Fitness", "12,400", "2.0%", "Jan-27", "24.30", "35.00"),
                      T("Tamsin Kwok Dermatology", "10,055", "1.6%", "Jun-29", "28.60", "35.00"),
                  ]))

    # -- F15: a look-alike table under a different heading -------------------
    F.append(dict(BASE, key="F15", filename="Offering Memorandum - 2025-12-08 - Final.pdf",
                  property_name="Cranmoor Depot", address="240 Selby Vale",
                  total_rsf="215,400", parcel="0558822300", version="Final",
                  trap="a Prior Year Lease Schedule that must never be read",
                  tenants=[
                      T("Vantage Row Distribution", "35,600", "16.5%", "Oct-29", "15.75", "18.50"),
                      T("Bexley Tool & Die", "18,220", "8.5%", "Mar-28", "14.90", "18.50"),
                  ],
                  # Same shape, different heading, stale figures. The slicer must
                  # never open a region here, so the model never sees these rows.
                  prior_year=[
                      ["Vantage Row Distribution", "35,600", "16.5%", "Oct-24", "13.10", "16.00", ""],
                      ["Corrigan Bearing Supply", "22,900", "10.6%", "Dec-23", "12.85", "16.00", ""],
                      ["Ellery Fastener Corp.", "9,140", "4.2%", "Jun-23", "11.95", "16.00", ""],
                  ]))

    return F


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def build_one(f) -> dict:
    path = PDF_DIR / f["filename"]
    c = Canvas(str(path), pagesize=letter)
    parcel = f.get("parcel")

    if f.get("scanned"):
        # Image-only throughout, so the text fraction is 0 and triage says
        # `scanned` rather than `hybrid`. Exactly ONE rent roll page: two
        # identical scanned tables would slice as two regions and duplicate
        # every row, which tests the wrong thing.
        scanned_cover_page(c, f)
        scanned_rentroll_page(c, f, [
            [t["tenant_name"], t["rentable_sf"], t["pct_of_building"],
             t["lease_expiration"], t["in_place_base_rent"], t["market_rent"]]
            for t in f["tenants"]])
        c.save()
        return {"classification": "scanned"}

    cover_page(c, f)
    details_parcel = parcel

    details_page(c, f, details_parcel)

    xs = f.get("xs", XS)
    headers = f.get("headers", HEADERS)
    groups = f.get("groups", GROUPS)
    dollar = f.get("dollar", False)

    def row_cells(t, i):
        base = cells(t, dollar=dollar)
        if f.get("extra"):                       # trailing distractor columns
            return base[:6] + f["extra"]
        if f.get("bracket"):                     # distractors before and after
            return [t["tenant_name"], t["rentable_sf"], t["pct_of_building"],
                    "35.00", t["lease_expiration"], t["in_place_base_rent"],
                    t["market_rent"], "1,788"]
        return base

    tenants = f["tenants"]

    if f.get("split_after"):
        n = f["split_after"]
        rentroll_page(c, f, [row_cells(t, i) for i, t in enumerate(tenants[:n])],
                      headers, groups, xs,
                      trailing=["The rent roll continues on the following page.",
                                "Figures are as of the date of this memorandum."],
                      parcel_value=parcel if f.get("parcel_everywhere") else None)
        rentroll_page(c, f, [row_cells(t, i) for i, t in enumerate(tenants[n:])],
                      headers, groups, xs, heading="CORE RENT ROLL (CONTINUED)",
                      parcel_value=parcel if f.get("parcel_everywhere") else None)
    else:
        rows = [row_cells(t, i) for i, t in enumerate(tenants)]
        if f.get("decoys"):
            rows = rows + f["decoys"]
        wraps = f.get("wraps")
        if wraps:
            # A wrapped name prints its tail on the next line, with no figures.
            # Drawn by trimming the printed name and adding a continuation line.
            out = []
            for i, r in enumerate(rows):
                if i in wraps:
                    head = r[0][: -(len(wraps[i]) + 1)]
                    out.append([head] + r[1:])
                    out.append([wraps[i]] + [""] * (len(r) - 1))
                else:
                    out.append(r)
            rows = out
        rentroll_page(c, f, rows, headers, groups, xs,
                      chrome_after=f.get("chrome_after"),
                      parcel_value=parcel if f.get("parcel_everywhere") else None)

    if f.get("second_parcel"):
        prose_page(c, f, "TITLE & PARCEL NOTE", [
            "The site was assembled from two adjoining lots and remains split for",
            "assessment purposes as of the date of this memorandum.",
            f"Tax Parcel ID Number {f['second_parcel']}",
        ])

    if f.get("prior_year"):
        # Same table shape, a heading the region pattern does not match, stale
        # figures. If this ever gets extracted the corpus will say so loudly.
        running_header(c, "Market Overview", f["property_name"])
        c.setFont(SANS_B, 16)
        c.drawString(45, H - 80, "PRIOR YEAR LEASE SCHEDULE (2019)")
        c.setFont(SANS, 9)
        c.drawString(45, H - 100, "Shown for comparison only. Superseded.")
        y = H - 140
        draw_row(c, y, HEADERS, XS, font=MONO_B)
        y -= 14
        for r in f["prior_year"]:
            draw_row(c, y, r, XS)
            y -= 12
        c.showPage()

    prose_page(c, f, "MARKET OVERVIEW", [
        "The submarket has absorbed steady net new demand over the trailing eight",
        "quarters, with asking rents trending above the metropolitan average.",
        "Ownership has completed capital improvements to the lobby and elevators.",
    ])

    c.save()
    return {"classification": "text_layer"}


# Expected document dates, stated explicitly rather than parsed. Ground truth
# produced by the code under test proves only that the code is self-consistent.
# F7 is the interesting one: "Sept 2019" carries no day, so the expected id is
# month-precision and must NOT be padded to a day.
EXPECTED_DATE = {
    "F1": "2026-02-17", "F2": "2025-11-11", "F3": "2026-05-04",
    "F4": "2024-09-30", "F5": "2026-06-15", "F6": "2026-03-09",
    "F7": "2019-09",    "F8": "2025-03-18", "F9": "2026-01-27",
    "F10": "2025-09-09", "F11": "2026-04-06", "F12": "2025-05-13",
    "F13": "2026-07-01", "F14": "2026-08-19", "F15": "2025-12-08",
}


def build():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    gt = {}
    for f in fixtures():
        meta = build_one(f)
        iso = EXPECTED_DATE[f["key"]]
        gt[f["filename"]] = {
            "trap": f["trap"],
            "document_date": iso,
            "document_id": f"OM-{iso}",
            "classification": meta["classification"],
            "ocr_required": bool(f.get("scanned")),
            "document_version": f.get("version"),
            "parcel_id": "FLAG_MULTIPLE" if f.get("second_parcel") else f.get("parcel"),
            "property_address": f["address"],
            "total_rentable_sf": f["total_rsf"],
            "contacts_present": bool(f.get("contacts")),
            "tenants": f["tenants"],
        }
        print(f"{f['key']:>3}  {f['filename']:<58} {f['trap']}")

    (HERE / "ground_truth.json").write_text(json.dumps(gt, indent=2))
    print(f"\n{len(gt)} fixtures -> {PDF_DIR}")
    print(f"ground truth -> {HERE / 'ground_truth.json'}")


if __name__ == "__main__":
    build()
