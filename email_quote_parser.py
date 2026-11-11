"""Pure, conservative parsing of monetary proposals in a single reply body.

Callers must remove quoted mail history before calling ``extract_quotes``. This
module never reads configuration, contacts, files, or network resources. Parsed
amounts are in the stated currency; no conversion is performed.
"""

from decimal import Decimal, InvalidOperation
import re


_HSPACE = r"[ \t\u00a0\u202f]*"
_CURRENCY = (
    r"(?<![A-Za-z])(?:"
    r"Canadian[ \t]+dollars?|Australian[ \t]+dollars?|"
    r"U\.?S\.?[ \t]+dollars?|US[ \t]*\$|CA[ \t]*\$|"
    r"AU[ \t]*\$|C[ \t]*\$|A[ \t]*\$|"
    r"USD(?![A-Za-z])|EUR(?![A-Za-z])|GBP(?![A-Za-z])|"
    r"CAD(?![A-Za-z])|AUD(?![A-Za-z])|"
    r"euros?\b|pounds?(?:[ \t]+sterling)?\b|dollars?\b|[\u20ac\u00a3$])"
)
_NUMBER = r"\d+(?:(?:[.,]\d+)|(?:[ \u00a0\u202f]\d{3}(?!\d)))*"
_TOKENS = rf"{_CURRENCY}(?:{_HSPACE}{_CURRENCY})?"
_MONEY_RE = re.compile(
    rf"(?<![\d.,])(?P<prefix>{_TOKENS})?{_HSPACE}"
    rf"(?P<first>{_NUMBER})(?P<first_scale>[kKmM])?"
    rf"(?:{_HSPACE}(?:"
    rf"(?P<range_sep>[-\u2013\u2014~\u223c]|\bto\b|\bthrough\b|至|到)"
    rf"{_HSPACE}(?P<range_currency>{_TOKENS})?{_HSPACE}|"
    rf"(?P<alternative_sep>/|\bor\b|\band\b){_HSPACE}(?!{_CURRENCY}))"
    rf"(?P<second>{_NUMBER})(?P<second_scale>[kKmM])?)?"
    rf"{_HSPACE}(?P<suffix>{_TOKENS}(?![ \t]*\d))?"
    rf"(?!\d|[.,]\d)",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(_CURRENCY, re.IGNORECASE)
_COOP_PATTERNS = (
    ("独立视频", r"独立视频|专属视频|(?<![A-Za-z])dedicated\b[^\n;.!?]{0,55}?\b(?:videos?|tutorials?)\b|(?:stand[ -]?alone|full[ -]?length)[ -]+(?:youtube[ -]+)?videos?\b|dedicated(?=\s*[:=])"),
    ("加链接", r"加链接|添加链接|描述区链接|简介链接|(?:description|bio)(?:[ -]+box)?[ -]+(?:link|placement)|(?:product[ -]+)?link[ -]+(?:in|to)[ -]+(?:the[ -]+)?description|link[ -]+(?:insertion|placement)|(?:add(?:ing)?|insert(?:ing)?)[ -]+(?:a[ -]+)?(?:product[ -]+)?link"),
    ("shorts", r"短视频|(?<![A-Za-z])(?:youtube[ -]+)?shorts?\b|short[ -]+(?:form[ -]+)?video"),
    ("插播", r"插播|植入|口播|(?<![A-Za-z])integrations?\b|integrated[ -]+(?:videos?|segments?|placements?)\b|mid[ -]?roll|in[ -]video[ -]+(?:ad|placement)|sponsored[ -]+segment"),
)
_COOP_REGEXES = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _COOP_PATTERNS]
_BOUNDARY_RE = re.compile(r"[;\n\r!?。！？；]|(?<!\d)\.(?!\d)|(?<=\d)\.(?!\d)")
_PRICE_CUE_RE = re.compile(
    r"(?:rates?|prices?|costs?|fees?|quotes?|budget|charg(?:e|es|ing)|报价|费用|价格|收费|预算)"
    r"[^\d\n;!?]{0,45}$",
    re.IGNORECASE,
)
_NON_MONEY_UNIT_RE = re.compile(
    r"^[ \t]*(?:[-\u2013\u2014][ \t]*)?(?:%|(?:views?|subscribers?|followers?|days?|weeks?|months?|years?|"
    r"minutes?|mins?|seconds?|secs?|hours?|videos?|s)\b|天|分钟|秒|粉丝|次播放)",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(
    r"(?<![\w.])(?P<quantity>\d+|single|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"[ \t]+(?:(?:dedicated|stand[ -]?alone|full[ -]?length|sponsored|youtube)[ \t]+)?videos?\b",
    re.IGNORECASE,
)
_QUANTITY_WORDS = {"single": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                   "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _number(raw):
    """Read US/EU grouping and decimal conventions without locale state."""
    value = re.sub(r"[ \u00a0\u202f]", "", raw)
    if "," in value and "." in value:
        decimal_mark = "," if value.rfind(",") > value.rfind(".") else "."
        grouping_mark = "." if decimal_mark == "," else ","
        whole, fractional = value.rsplit(decimal_mark, 1)
        if len(fractional) not in (1, 2) or not fractional.isdigit():
            return None
        if decimal_mark in whole:
            return None
        if grouping_mark in whole and not re.fullmatch(r"\d{1,3}(?:" + re.escape(grouping_mark) + r"\d{3})+", whole):
            return None
        value = whole.replace(grouping_mark, "") + "." + fractional
    elif "," in value or "." in value:
        mark = "," if "," in value else "."
        parts = value.split(mark)
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            value = parts[0] + "." + parts[1]
        elif all(len(part) == 3 for part in parts[1:]) and 1 <= len(parts[0]) <= 3:
            value = "".join(parts)
        else:
            return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def _currency(parts):
    explicit = []
    assumed_usd = False
    ambiguous_dollars = False
    for part in parts:
        for match in _CURRENCY_RE.finditer(part or ""):
            token = re.sub(r"[\s.]", "", match.group()).lower()
            if token in ("usd", "us$", "usdollar", "usdollars"):
                explicit.append(("USD", "explicit_code" if token == "usd" else "explicit_symbol" if "$" in token else "explicit_name"))
            elif token in ("cad", "ca$", "c$", "canadiandollar", "canadiandollars"):
                explicit.append(("CAD", "explicit_code" if token == "cad" else "explicit_symbol" if "$" in token else "explicit_name"))
            elif token in ("aud", "au$", "a$", "australiandollar", "australiandollars"):
                explicit.append(("AUD", "explicit_code" if token == "aud" else "explicit_symbol" if "$" in token else "explicit_name"))
            elif token in ("eur", "€", "euro", "euros"):
                explicit.append(("EUR", "explicit_code" if token == "eur" else "explicit_symbol" if token == "€" else "explicit_name"))
            elif token == "gbp" or token == "£" or token.startswith("pound"):
                explicit.append(("GBP", "explicit_code" if token == "gbp" else "explicit_symbol" if token == "£" else "explicit_name"))
            elif token == "$":
                assumed_usd = True
            elif token in ("dollar", "dollars"):
                ambiguous_dollars = True
    currencies = {currency for currency, _ in explicit}
    if len(currencies) > 1:
        return "", "conflicting", sorted(currencies)
    if explicit:
        return explicit[0][0], explicit[0][1], list(currencies)
    if assumed_usd:
        return "USD", "symbol_assumed_usd", ["USD"]
    return "", "ambiguous_dollars" if ambiguous_dollars else "unknown", []


def _clause_bounds(text, start, end):
    left = 0
    right = len(text)
    for boundary in _BOUNDARY_RE.finditer(text):
        if boundary.end() <= start:
            left = boundary.end()
        elif boundary.start() >= end:
            right = boundary.start()
            break
    return left, right


def _cooperation(text, start, end):
    """Attach the closest label within the price's sentence or list line."""
    left, right = _clause_bounds(text, start, end)
    labels = []
    clause_types = set()
    for name, pattern in _COOP_REGEXES:
        for match in pattern.finditer(text, left, right):
            clause_types.add(name)
            gap = max(start - match.end(), match.start() - end, 0)
            if gap <= 120:
                labels.append((gap, name, match.start(), match.end()))
    if not labels:
        # A second offer described only as "video" inherits the sole explicit
        # type in its sentence; mixed dedicated/shorts sentences stay distinct.
        return next(iter(clause_types)) if len(clause_types) == 1 else ""
    nearest_distance = min(label[0] for label in labels)
    nearest = {label[1] for label in labels if label[0] == nearest_distance}
    return next(iter(nearest)) if len(nearest) == 1 else ""


def _quantity(text, start, end):
    left, right = _clause_bounds(text, start, end)
    before = []
    after = []
    for match in _QUANTITY_RE.finditer(text, left, right):
        token = match.group("quantity").lower()
        quantity = int(token) if token.isdigit() else _QUANTITY_WORDS[token]
        if quantity <= 0:
            continue
        if match.end() <= start:
            between = text[match.end():start]
            if len(between) <= 120 and not _CURRENCY_RE.search(between):
                before.append((start - match.end(), quantity))
        elif match.start() >= end:
            between = text[end:match.start()]
            if re.fullmatch(r"[ \t]*(?:for|covers?|includes?)[ \t]+", between, re.IGNORECASE):
                after.append((match.start() - end, quantity))
    choices = after or before
    return min(choices)[1] if choices else None


def normalize_coop_type(value):
    """Normalize UI values and common English labels to the four UI types."""
    value = str(value or "").strip()
    for canonical, pattern in _COOP_REGEXES:
        if value == canonical or pattern.fullmatch(value):
            return canonical
    return value


def extract_quotes(text):
    """Return every monetary proposal, retaining currencies and price ranges.

    A scalar has ``amount``; a range has ``amount=None`` and ``amount_min`` /
    ``amount_max``. ``context`` is at most 300 characters from this input body.
    Bare dollar signs are explicitly marked as assumed USD. Contextual prices
    without a currency remain inspectable but cannot be auto-selected.
    """
    text = str(text or "")
    # Same-length normalization accepts $*2000* and *1500$* without changing
    # source offsets. Context and raw excerpts still use the original body.
    parse_text = text.replace("*", " ")
    quotes = []
    for match in _MONEY_RE.finditer(parse_text):
        start, end = match.span()
        # Optional leading whitespace belongs to layout, not the proposal.
        while start < end and parse_text[start].isspace():
            start += 1
        while end > start and parse_text[end - 1].isspace():
            end -= 1
        # Never turn an unsupported attached suffix into a smaller price.
        if end < len(parse_text) and parse_text[end].isalpha():
            continue
        prefix, suffix = match.group("prefix"), match.group("suffix")
        range_currency = match.group("range_currency")
        currency, source, candidates = _currency((prefix, range_currency, suffix))
        first = _number(match.group("first"))
        second = _number(match.group("second")) if match.group("second") else None
        if first is None or (match.group("second") and second is None):
            continue
        first *= {"k": 1000, "m": 1000000}.get((match.group("first_scale") or "").lower(), 1)
        if second is not None:
            second *= {"k": 1000, "m": 1000000}.get((match.group("second_scale") or "").lower(), 1)
        if start > 0 and parse_text[start - 1] == "-":
            first = -first
        coop_type = _cooperation(parse_text, start, end)
        if not (prefix or suffix or range_currency):
            before = parse_text[max(0, start - 80):start]
            label_price = bool(coop_type and re.search(r"[:=：]\s*$", before))
            if not (_PRICE_CUE_RE.search(before) or label_price):
                continue
            if _NON_MONEY_UNIT_RE.match(parse_text[end:]):
                continue
        quantity = _quantity(parse_text, start, end)
        while start > 0 and text[start - 1] == "*":
            start -= 1
        while end < len(text) and text[end] == "*":
            end += 1
        context_start = max(0, start - 100)
        context_end = min(len(text), max(end + 100, context_start + 200))
        if context_end - context_start > 300:
            context_start = max(0, start - 80)
            context_end = min(len(text), context_start + 300)
        quote = {
            "amount": first if second is None else None,
            "currency": currency,
            "currency_source": source,
            "coop_type": coop_type,
            "quantity": quantity,
            "price_basis": "package" if quantity and quantity > 1 else "single" if quantity == 1 else "unknown",
            "context": text[context_start:context_end].strip()[:300],
            "start": start,
            "end": end,
            "raw": text[start:end],
        }
        if len(candidates) > 1:
            quote["currency_candidates"] = candidates
        if second is not None:
            quote["amount_min"] = min(first, second)
            quote["amount_max"] = max(first, second)
            quote["range_kind"] = "alternatives" if match.group("alternative_sep") else "range"
        elif quantity and quantity > 1:
            unit_amount = Decimal(str(first)) / quantity
            quote["unit_amount"] = int(unit_amount) if unit_amount == unit_amount.to_integral_value() else float(unit_amount)
        quotes.append(quote)
    # A sentence containing a currency price may also contain numbered list
    # items, production durations, or quantities. Those are not extra prices.
    explicit_bounds = {
        _clause_bounds(parse_text, quote["start"], quote["end"])
        for quote in quotes if quote["currency_source"] != "unknown"
    }
    quotes = [quote for quote in quotes if quote["currency_source"] != "unknown"
              or _clause_bounds(parse_text, quote["start"], quote["end"]) not in explicit_bounds]
    distinct = []
    seen = {}
    for quote in quotes:
        key = tuple(quote.get(field) for field in
                    ("amount", "currency", "coop_type", "quantity", "price_basis", "amount_min", "amount_max", "range_kind"))
        if key in seen:
            previous = seen[key]
            if "occurrences" not in previous:
                previous["occurrences"] = [{field: previous[field] for field in ("start", "end", "raw", "context")}]
            previous["occurrences"].append({field: quote[field] for field in ("start", "end", "raw", "context")})
        else:
            seen[key] = quote
            distinct.append(quote)
    return distinct


def select_quote(quotes, coop_type=""):
    """Select a clear scalar without silently choosing among alternatives.

    Exactly one proposal for the requested type may use any supported currency.
    Otherwise there must be just one proposal globally and it must be USD.
    A stated different cooperation type, ranges, package totals, and unknown /
    conflicting currencies never populate the negotiation amount automatically.
    """
    quotes = list(quotes or [])
    requested_type = normalize_coop_type(coop_type)

    def scalar(quote):
        amount = quote.get("amount")
        return (
            isinstance(amount, (int, float))
            and not isinstance(amount, bool)
            and amount > 0
            and quote.get("price_basis") != "package"
            and "amount_min" not in quote
            and "amount_max" not in quote
            and quote.get("currency") in {"USD", "EUR", "GBP", "CAD", "AUD"}
            and quote.get("currency_source") not in {"unknown", "conflicting", "ambiguous_dollars"}
        )

    if requested_type:
        matching = [q for q in quotes if normalize_coop_type(q.get("coop_type")) == requested_type]
        if matching:
            return matching[0] if len(matching) == 1 and scalar(matching[0]) else None
    if len(quotes) != 1:
        return None
    quote = quotes[0]
    if requested_type and quote.get("coop_type") and normalize_coop_type(quote["coop_type"]) != requested_type:
        return None
    return quote if scalar(quote) and quote.get("currency") == "USD" else None
