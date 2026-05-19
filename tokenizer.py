import re

# Allow underscores in block names: RECIPE, HEADNOTE, INGREDIENTS, STEPS, INDEX_TERMS, END
MARKER_LINE = re.compile(r'^===\s*([A-Z_ ]+)\s*===$', re.MULTILINE)

def _nl(x: str) -> str:
    return x.replace('\r\n', '\n').replace('\r', '\n')

def normalize_word(w: str) -> str:
    return w.lower()

def tokenize(text: str):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        # skip non-word chars
        while i < n and not (text[i].isalnum() or text[i] == '_'):
            i += 1
        if i >= n:
            break
        # read word
        start = i
        i += 1
        while i < n and (text[i].isalnum() or text[i] == '_'):
            i += 1
        word = normalize_word(text[start:i])
        # read joiner
        jstart = i
        while i < n and not (text[i].isalnum() or text[i] == '_'):
            i += 1
        joiner = text[jstart:i]
        tokens.append((word, joiner))
    return tokens

def parse_recipe_text(text: str):
    """
    Robust marker-based parser for the structured recipe format.
    Blocks: RECIPE, HEADNOTE, INGREDIENTS, STEPS, INDEX_TERMS, END
    """
    text = _nl(text).strip()
    if not text:
        raise ValueError("Empty input")

    # 1) Find all markers with their positions
    markers = []
    for m in MARKER_LINE.finditer(text):
        name = m.group(1).strip().upper()
        markers.append((name, m.start(), m.end()))
    if not markers:
        raise ValueError("No block markers found (e.g., '=== RECIPE ===').")

    # 2) Build blocks dict by slicing from this marker to the next
    blocks = {}
    for idx, (name, start, end) in enumerate(markers):
        next_start = markers[idx + 1][1] if idx + 1 < len(markers) else len(text)
        content = text[end:next_start].strip('\n')
        blocks[name] = content

    # 3) Parse header from RECIPE block only
    header = blocks.get('RECIPE', '')
    def get_field(fname: str):
        for ln in header.splitlines():
            if ln.upper().startswith(fname + ":"):
                return ln.split(":", 1)[1].strip()
        return ""

    try:
        order_in_section = int(get_field("ORDER_IN_SECTION") or "0")
    except ValueError:
        order_in_section = 0

    var_raw = get_field("VARIANT_NO")
    try:
        variant_no = int(var_raw) if var_raw else None
    except ValueError:
        variant_no = None

    serv_raw = get_field("SERVINGS")
    try:
        servings = int(serv_raw) if serv_raw else None
    except ValueError:
        servings = None

    kt = (get_field("KASHRUT_TYPE") or "UNKNOWN").upper()
    if kt not in ("MEAT","DAIRY","PARVE","UNKNOWN"):
        kt = "UNKNOWN"

    data = {
        "title": get_field("TITLE"),
        "section": get_field("SECTION") or "UNSPECIFIED",
        "order_in_section": order_in_section,
        "variant_no": variant_no,
        "kashrut_type": kt,
        "holiday_flags": get_field("HOLIDAY_FLAGS"),
        "primary_ingredient": get_field("PRIMARY_INGREDIENT"),
        "methods": get_field("METHODS"),
        "yield_text": get_field("YIELD_TEXT"),
        "servings": servings,
        # Pull INDEX_TERMS cleanly from its own block (now recognized thanks to underscore)
        "index_terms": (blocks.get("INDEX_TERMS", "") or "").replace("\n", " ").strip(),
        "segments": [],
    }

    if not data["title"]:
        raise ValueError("TITLE is required in RECIPE header")

    # 4) Build segments (TITLE/HEADNOTE/INGREDIENT/STEP) ONLY
    # Title as a segment for convenience (index 1)
    data["segments"].append({"type": "TITLE", "index": 1, "text": data["title"]})

    head = (blocks.get("HEADNOTE", "") or "").strip()
    if head:
        data["segments"].append({"type": "HEADNOTE", "index": 1, "text": head})

    ings = blocks.get("INGREDIENTS", "")
    if ings:
        lines = [ln.strip() for ln in _nl(ings).splitlines() if ln.strip()]
        for i, ln in enumerate(lines, start=1):
            ln = re.sub(r'^\d+\.\s*', '', ln)  # remove "1. " prefix if present
            data["segments"].append({"type": "INGREDIENT", "index": i, "text": ln})

    steps = blocks.get("STEPS", "")
    if steps:
        lines = [ln.strip() for ln in _nl(steps).splitlines() if ln.strip()]
        for i, ln in enumerate(lines, start=1):
            ln = re.sub(r'^\d+\.\s*', '', ln)  # remove "1. " prefix if present
            data["segments"].append({"type": "STEP", "index": i, "text": ln})

    return data
