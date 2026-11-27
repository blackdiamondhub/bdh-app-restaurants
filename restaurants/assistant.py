"""Assistant tools: Sun Peaks restaurants for the hub's voice/chat assistant.

One read-only lookup for every mode: places to eat or drink, filtered by a
kind of food or meal ("pizza", "dinner", "takeout") and by when the guest
wants to go ("now", "tonight", "7 pm", "saturday"), or one place named
loosely ("masas") with its hours, phone and what it offers. Answers are small
dicts of speech-ready strings in lodge time ("open until 10 pm").
"""
from __future__ import annotations

import datetime as dt
import difflib
import logging
import re
import unicodedata

from bdh_sdk import ALL_MODES
from bdh_sdk.tools import Tool, ToolError

from . import catalog
from .models import Restaurant

log = logging.getLogger(__name__)

FIND_RESTAURANTS = "find_restaurants"

NO_DATA = "I don't have the restaurants' hours right now."
EMPTY = "The restaurant list is empty right now."
# Hours older than this still answer, with a note.
STALE_AFTER = dt.timedelta(days=4)
MAX_LIST = 6
MAX_MATCHES = 3
MAX_ABOUT = 160
WEEK = catalog.WEEK
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Meal and part-of-day words: (usual time, window start hour, window end hour; past 24 is after midnight).
TIMES = {
    "breakfast": ((8, 0), 6, 11),
    "brunch": ((10, 30), 9, 13),
    "morning": ((9, 0), 6, 12),
    "lunch": ((12, 30), 11, 15),
    "afternoon": ((14, 0), 12, 17),
    "apres": ((16, 0), 15, 18),
    "dinner": ((18, 30), 17, 21),
    "supper": ((18, 30), 17, 21),
    "evening": ((18, 30), 17, 22),
    "tonight": ((18, 30), 17, 28),
    "night": ((20, 0), 18, 28),
    "late": ((22, 0), 21, 28),
}
WHEN_FILLER = frozenset({"at", "on", "this", "for", "around", "about", "the", "by", "in", "o", "clock", "oclock",
                         "time", "is", "open", "next", "coming", "eat", "go", "get", "some", "a", "-", "ish"})
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(a|p)?$")

# Food words → what they mean: "tag:x" (catalog.tags), "cat:x" (category) or "attr:x" (Google field).
FOOD = {
    "coffee": ("tag:coffee",), "espresso": ("tag:coffee",), "latte": ("tag:coffee",), "cafe": ("cat:cafe",),
    "bakery": ("cat:cafe",), "pastry": ("cat:cafe",), "pastries": ("cat:cafe",),
    "breakfast": ("tag:breakfast",), "brunch": ("tag:breakfast",), "lunch": ("tag:lunch",),
    "dinner": ("tag:dinner",), "supper": ("tag:dinner",),
    "apres": ("tag:apres", "cat:pub"), "drink": ("tag:apres", "cat:pub"), "beer": ("tag:apres", "cat:pub"),
    "cocktail": ("tag:apres", "cat:pub"), "bar": ("cat:pub", "tag:apres"), "pub": ("cat:pub",),
    "happy hour": ("tag:apres", "cat:pub"), "grill": ("cat:pub",), "wing": ("cat:pub",),
    "dessert": ("cat:treats",), "ice cream": ("cat:treats",), "sweet": ("cat:treats",), "treat": ("cat:treats",),
    "donut": ("cat:treats",), "doughnut": ("cat:treats",), "mini donut": ("cat:treats",),
    "pizza": ("cat:pizza",), "italian": ("cat:italian",), "pasta": ("cat:italian",),
    "sushi": ("cat:japanese",), "japanese": ("cat:japanese",), "ramen": ("cat:japanese",),
    "asian": ("cat:japanese",),
    "burger": ("cat:burger",), "diner": ("cat:burger",), "poutine": ("cat:burger",), "fries": ("cat:burger",),
    "fondue": ("cat:fondue",), "swiss": ("cat:fondue",), "cheese": ("cat:fondue",),
    "fine dining": ("cat:fine",), "fancy": ("cat:fine",), "upscale": ("cat:fine",), "romantic": ("cat:fine",),
    "date night": ("cat:fine",), "steak": ("cat:fine",), "steakhouse": ("cat:fine",),
    "german": ("cat:german",), "schnitzel": ("cat:german",), "bratwurst": ("cat:german",),
    "sausage": ("cat:german",), "casual": ("cat:family",),
    "kid": ("tag:kids",), "family": ("tag:kids",), "family friendly": ("tag:kids",), "children": ("tag:kids",),
    "takeout": ("attr:takeout",), "take out": ("attr:takeout",), "to go": ("attr:takeout",),
    "delivery": ("attr:delivery",), "deliver": ("attr:delivery",), "delivers": ("attr:delivery",),
    "reservation": ("attr:reservable",), "reserve": ("attr:reservable",), "book": ("attr:reservable",),
    "patio": ("attr:outdoorSeating",), "outdoor": ("attr:outdoorSeating",), "group": ("tag:groups",),
    "dog": ("attr:allowsDogs",), "dog friendly": ("attr:allowsDogs",), "live music": ("attr:liveMusic",),
    "sport": ("attr:goodForWatchingSports",), "hockey": ("attr:goodForWatchingSports",),
}
FOOD_FILLER = frozenset({"food", "place", "places", "restaurant", "restaurants", "something", "some", "get", "eat",
                         "to", "for", "a", "an", "good", "the", "and", "with", "spot", "spots", "joint", "great",
                         "best", "nice", "option", "options", "where", "can", "we", "i", "any", "that", "has",
                         "serves", "serving", "open", "or", "meal"})
MEALS = frozenset({"breakfast", "brunch", "lunch", "apres", "dinner", "supper"})
KINDS_HINT = ("Try pizza, sushi, Italian, burgers, pub food, fondue, German, fine dining, coffee, treats, "
              "breakfast, lunch, après or dinner.")
OFFERS = (("takeout", "takeout"), ("delivery", "delivery"), ("reservable", "reservations"),
          ("outdoorSeating", "a patio"), ("menuForChildren", "a kids menu"), ("liveMusic", "live music"))

NAME_FILLER = frozenset({"the", "a", "an", "at", "is", "are", "open", "restaurant", "sun", "peaks", "and", "in",
                         "place", "still", "today", "tonight", "now"})
FUZZY_WORD = 0.75
FUZZY_NAME = 0.75
NUMBERS = dict(zip(("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
                   map(str, range(1, 11))))


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def tools(mode=None):
    return [
        Tool(FIND_RESTAURANTS, "Find restaurants",
             "Sun Peaks restaurants, cafés and bars: what's open now or at a time, where to get a food or meal "
             "(pizza, sushi, coffee, breakfast, dinner, après), or one place's hours, phone and takeout. Use for "
             "'what's open for dinner?', 'is Masa's open?'. Not for events.",
             params={"type": "object", "properties": {
                 "name": {"type": "string", "maxLength": 60,
                          "description": "One place in the guest's words, e.g. 'Masas', 'the pizza place'. "
                                         "Leave out to list places."},
                 "food": {"type": "string", "maxLength": 60,
                          "description": "A cuisine, dish, meal or need: pizza, sushi, italian, burgers, "
                                         "poutine, fondue, steak, coffee, breakfast, lunch, apres, dinner, "
                                         "dessert, drinks, kids, takeout, delivery, reservations, patio."},
                 "when": {"type": "string", "maxLength": 40,
                          "description": "When they'd go, in their words: now, tonight, 7 pm, tomorrow "
                                         "morning, saturday dinner. With it only places open then are listed."},
             }},
             modes=ALL_MODES, readonly=True),
    ]


# ── words ──

def _fold(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().replace("'", "").replace("’", "")


def _words(text, filler):
    words = (NUMBERS.get(w, w) for w in re.split(r"[^0-9a-z]+", _fold(text)) if w)
    return tuple(w for w in words if w not in filler)


def _fix_words(words, vocab, cutoff):
    out = []
    for w in words:
        if w in vocab or w.isdigit():
            out.append(w)
            continue
        close = difflib.get_close_matches(w, vocab, n=1, cutoff=cutoff)
        out.append(close[0] if close else w)
    return tuple(out)


def match(text, names, filler=NAME_FILLER, cutoff=FUZZY_WORD):
    """The names (in order) that ``text`` means, tolerant of misheard words.

    An exact name wins; otherwise every name holding all the request's words,
    then the closest whole names. Empty when nothing is close.
    """
    keys = {name: frozenset(_words(name, filler)) for name in names}
    vocab = sorted({w for k in keys.values() for w in k})
    raw = _words(text, filler)
    fixed = _fix_words(raw, vocab, cutoff)
    for attempt in (raw, fixed):
        key = frozenset(attempt)
        if not key:
            return []
        exact = [n for n in names if keys[n] == key]
        if exact:
            return exact
        holding = [n for n in names if key <= keys[n]]
        if holding:
            return holding
    tries = {"".join(raw), "".join(fixed)}
    scored = sorted(((max(difflib.SequenceMatcher(None, t, "".join(_words(n, filler))).ratio() for t in tries), n)
                     for n in names), reverse=True)
    best = [n for score, n in scored if score >= FUZZY_NAME and score >= scored[0][0] - 0.05]
    return [n for n in names if n in best]


def _join(parts):
    parts = list(parts)
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


# ── hours ──

def _speak(hour, minute=0):
    if hour % 24 == 0 and minute == 0:
        return "midnight"
    if hour == 12 and minute == 0:
        return "noon"
    return f"{hour % 12 or 12}{f':{minute:02d}' if minute else ''} {'am' if hour % 24 < 12 else 'pm'}"


def _point(p):
    return p.get("hour", 0), p.get("minute", 0)


def _mow(t):
    return ((t.weekday() + 1) % 7) * 1440 + t.hour * 60 + t.minute


def _periods(data):
    return [p for p in ((data or {}).get("regularOpeningHours") or {}).get("periods") or [] if p.get("open")]


def _season(data):
    business = (data or {}).get("businessStatus")
    if business in ("CLOSED_TEMPORARILY", "FUTURE_OPENING"):
        return "closed for the season"
    if business == "CLOSED_PERMANENTLY":
        return "closed for good"
    return None


def _open_at(periods, t):
    """The period ``t`` (lodge time, naive) falls in, else None."""
    current = _mow(t)
    for p in periods:
        if not p.get("close"):
            return p
        start, end = catalog._minute_of_week(p["open"]), catalog._minute_of_week(p["close"])
        if end <= start:
            end += WEEK
        if start <= current < end or start <= current + WEEK < end:
            return p
    return None


def _day_word(d, today):
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    if 0 < (d - today).days < 7:
        return f"{d:%A}"
    return f"{d:%A}, {d:%B} {d.day}"


def state(data, t):
    """``(kind, text)`` at ``t``: kind open, closed, season or unknown."""
    if not data:
        return "unknown", "hours unknown"
    season = _season(data)
    if season:
        return "season", season
    periods = _periods(data)
    if not periods:
        return "unknown", "hours unknown"
    p = _open_at(periods, t)
    if p:
        if not p.get("close"):
            return "open", "open 24 hours"
        return "open", f"open until {_speak(*_point(p['close']))}"
    current = _mow(t)
    wait, nxt = min(((catalog._minute_of_week(p["open"]) - current) % WEEK, p["open"]) for p in periods)
    opens = (t + dt.timedelta(minutes=wait)).date()
    day = "" if opens == t.date() else f" {_day_word(opens, t.date())}"
    return "closed", f"closed, opens{day} at {_speak(*_point(nxt))}"


def hours_on(data, d):
    """The hours a place opens on date ``d``: "9 am to 10 pm", "closed" or None when unknown."""
    season = _season(data)
    if season:
        return season
    periods = _periods(data)
    if not periods:
        return None
    google_day = (d.weekday() + 1) % 7
    spans = []
    for p in sorted((p for p in periods if p["open"].get("day") == google_day), key=lambda p: _point(p["open"])):
        if not p.get("close"):
            return "open 24 hours"
        spans.append(f"{_speak(*_point(p['open']))} to {_speak(*_point(p['close']))}")
    return " and ".join(spans) or "closed"


def _open_on(data, d):
    hours = hours_on(data, d)
    return bool(hours) and hours not in ("closed", "closed for the season", "closed for good")


# ── when ──

def _weekday(word):
    if len(word) < 3:
        return None
    return next((i for i, w in enumerate(WEEKDAYS) if w == word.rstrip("s") or w.startswith(word)), None)


def parse_when(text, local):
    """``text`` → ``(datetime or None, date, label)`` in lodge time, or None for right now.

    A time with no day that's well past rolls over to tomorrow; a meal or part of
    the day already under way means now. With only a day, the time is None.
    """
    s = _fold(text).strip(" ?.!,")
    s = re.sub(r"(\d)\s*([ap])\.?\s*m\b\.?", r"\1\2", s)
    s = s.replace("after tomorrow", "aftertomorrow")
    words = [w for w in re.split(r"[^0-9a-z:]+", s) if w and w not in WHEN_FILLER]
    today = local.date()
    if not words or words in (["now"], ["right", "now"], ["currently"]):
        return None
    if words == ["today"]:
        return None, today, "today"
    day, explicit_day, clock, part = today, False, None, None
    pending = None
    for w in words:
        if w in ("today", "now", "right"):
            explicit_day = explicit_day or w == "today"
        elif w == "tomorrow":
            day, explicit_day = today + dt.timedelta(days=1), True
        elif w in ("aftertomorrow", "day"):
            if w == "aftertomorrow":
                day, explicit_day = today + dt.timedelta(days=2), True
        elif w in ("noon", "midday"):
            clock = (12, 0)
        elif w == "midnight":
            clock = (0, 0)
        elif w in TIMES or w.rstrip("s") in TIMES:
            part = w if w in TIMES else w.rstrip("s")
        elif w in ("am", "pm", "a", "p") and pending is not None:
            clock = (pending[0] % 12 + (12 if w[0] == "p" else 0), pending[1])
            pending = None
        elif _CLOCK.match(w):
            hour, minute, ap = _CLOCK.match(w).groups()
            hour, minute = int(hour), int(minute or 0)
            if hour > 23 or minute > 59:
                raise ToolError(f"I can't tell what time {text!r} is.")
            if ap:
                clock = (hour % 12 + (12 if ap == "p" else 0), minute)
            else:
                clock, pending = (hour, minute), (hour, minute)
        elif _weekday(w) is not None:
            day, explicit_day = today + dt.timedelta(days=(_weekday(w) - today.weekday()) % 7), True
        else:
            raise ToolError(f"I can't tell when {text!r} is; say now, tonight, a time like 7 pm, tomorrow "
                            "or a weekday.")
    if pending is not None and clock == pending and pending[0] <= 12:
        # "at 7" for a meal out is the evening, unless it's a morning.
        hour, minute = pending
        morning = part in ("breakfast", "brunch", "morning")
        if hour == 12:
            clock = (12, minute)
        elif morning or hour == 11:
            clock = (hour, minute)
        else:
            clock = (hour + 12, minute)
    elif clock and part in ("tonight", "evening", "night", "late", "dinner", "supper", "afternoon", "apres") \
            and clock[0] < 12 and clock != (0, 0):
        clock = (clock[0] + 12, clock[1])
    if clock is None and part is None:
        return None, day, _day_word(day, today)
    if clock is None:
        (hour, minute), start, end = TIMES[part]
        hour_now = local.hour + (24 if local.hour < 4 else 0)
        if not explicit_day and start <= hour_now < end:
            return local, today, "now"
        clock = (hour, minute)
    t = dt.datetime.combine(day, dt.time(*clock))
    if not explicit_day and t < local - dt.timedelta(hours=1):
        t += dt.timedelta(days=1)
    label = _day_word(t.date(), today)
    if label == "today" and t.hour >= 17:
        label = "tonight"
    return t, t.date(), f"{label} at {_speak(t.hour, t.minute)}"


# ── food ──

def _singular(word):
    if word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 else word


def food_terms(text):
    """``text`` → ``[(word, targets)]``; targets empty means "search names and summaries"."""
    s = " " + " ".join(_words(text, ())) + " "
    terms = []
    for phrase in sorted((k for k in FOOD if " " in k), key=len, reverse=True):
        if f" {phrase} " in s or f" {phrase}s " in s:
            terms.append((phrase, FOOD[phrase]))
            s = s.replace(f" {phrase}s ", " ").replace(f" {phrase} ", " ")
    for word in s.split():
        if word in FOOD_FILLER:
            continue
        key = word if word in FOOD else _singular(word)
        terms.append((key, FOOD.get(key, ())))
    return terms


def _has(restaurant, tags, target):
    kind, _, value = target.partition(":")
    if kind == "tag":
        return value in tags
    if kind == "cat":
        return restaurant.category == value
    return bool((restaurant.data or {}).get(value))


def _mentions(restaurant, word):
    text = _fold(f"{restaurant.name} {catalog.summary(restaurant.data)}")
    return re.search(rf"\b{re.escape(word)}", text) is not None


def _serves(restaurant, terms):
    tags = catalog.tags(restaurant)
    return all(any(_has(restaurant, tags, t) for t in targets) or _mentions(restaurant, word)
               for word, targets in terms)


# ── running ──

def _places():
    try:
        from .places import maybe_refresh_async
        maybe_refresh_async()
    except Exception:
        log.exception("restaurants refresh didn't start")
    places = list(Restaurant.objects.filter(hidden=False))
    if not places:
        raise ToolError(EMPTY)
    if not any(r.data for r in places):
        raise ToolError(NO_DATA)
    return places


def _note(places, now):
    fetched = [r.fetched_at for r in places if r.fetched_at]
    if fetched and now - max(fetched) > STALE_AFTER:
        return f"Hours were last checked {(now - max(fetched)).days} days ago and may have changed."
    return None


def _kind(restaurant):
    return catalog.CATEGORIES.get(restaurant.category, catalog.CATEGORIES["family"]).label


def _compact(entry):
    return {k: v for k, v in entry.items() if v not in (None, "", [], {})}


def _about(restaurant):
    text = catalog.summary(restaurant.data)
    if len(text) > MAX_ABOUT:
        text = text[:MAX_ABOUT].rsplit(" ", 1)[0].rstrip(",;") + "…"
    return text


def _one(r, local, when):
    data = r.data or {}
    entry = {"name": r.name, "kind": _kind(r), "status": state(data, local)[1],
             "hours_today": hours_on(data, local.date())}
    if when and when[1] != local.date():
        entry[f"hours_{_day_word(when[1], local.date()).split(',')[0].lower()}"] = hours_on(data, when[1])
    if when and when[0] is not None and when[2] != "now":
        entry["then"] = f"{'open' if state(data, when[0])[0] == 'open' else 'closed'} {when[2]}"
    offers = [label for attr, label in OFFERS if data.get(attr)]
    km = catalog.distance_km(data)
    entry.update({
        "phone": data.get("nationalPhoneNumber"),
        "price": catalog.PRICE.get(data.get("priceLevel")),
        "rating": f"{data['rating']:g} stars" if data.get("rating") else None,
        "offers": _join(offers),
        "distance": f"{km:.1f} km from the lodge" if km else None,
        "about": _about(r),
        "owner_note": r.note,
    })
    return _compact(entry)


def _named(places, args, local, when):
    by_name = {r.name: r for r in places}
    names = match(args["name"], list(by_name))
    if not names:
        raise ToolError(f"There's no {args['name'].strip()!r} on the Sun Peaks restaurant list.")
    if len(names) > MAX_MATCHES:
        raise ToolError(f"{args['name'].strip()!r} could be {_join(names)}. Which one?")
    entries = [_one(by_name[n], local, when) for n in names]
    return entries[0] if len(entries) == 1 else {"matches": entries}


def _list(places, args, local, when):
    terms = food_terms(args.get("food") or "")
    if terms:
        unknown = [w for w, targets in terms if not targets and not any(_mentions(r, w) for r in places)]
        if unknown:
            raise ToolError(f"None of the Sun Peaks places list {_join(unknown)}. {KINDS_HINT}")
        places = [r for r in places if _serves(r, terms)]
    filtered = bool(args.get("when")) or not terms
    if when is None:
        meal = next((w for w, _ in terms if w in MEALS), None)
        if meal and not args.get("when"):
            when = parse_when(meal, local)
    at, day, label = when if when else (local, local.date(), "now")
    season = [r.name for r in places if _season(r.data)]
    places = [r for r in places if not _season(r.data)]
    hits = []
    for r in places:
        data = r.data or {}
        is_open = state(data, at)[0] == "open" if at is not None else _open_on(data, day)
        hits.append((not is_open, -(data.get("rating") or 0), r.name, r, is_open))
    hits.sort(key=lambda h: h[:3])
    open_count = sum(1 for h in hits if h[4])
    if filtered:
        hits = [h for h in hits if h[4]]
    day_word = _day_word(day, local.date())
    entries = []
    for *_, r, is_open in hits[:MAX_LIST]:
        data = r.data or {}
        hours = hours_on(data, day)
        if hours:
            hours = f"closed {day_word}" if hours == "closed" else f"{day_word} {hours}"
        entries.append(_compact({"name": r.name, "kind": _kind(r), "status": state(data, local)[1],
                                 "hours": hours,
                                 "price": catalog.PRICE.get(data.get("priceLevel"))}))
    out = {"when": label, "open_then": f"{open_count} of {len(places)}" + (f" {args['food'].strip()} places"
                                                                            if terms else " places"),
           "places": entries}
    if len(hits) > MAX_LIST:
        out["also"] = _join(h[3].name for h in hits[MAX_LIST:])
    if not entries:
        if places:
            out["note"] = f"Nothing{' that fits' if terms else ''} is open {label}."
        elif season:
            out["note"] = "The only places that fit are closed for the season."
        else:
            out["note"] = f"None of the Sun Peaks places list {args['food'].strip()}."
        soon = []
        for r in places:
            kind, text = state(r.data or {}, at or dt.datetime.combine(day, dt.time()))
            if kind == "closed":
                soon.append((_mow_wait(r.data, at or dt.datetime.combine(day, dt.time())), f"{r.name} {text[8:]}"))
        if soon:
            out["opens_next"] = [text for _, text in sorted(soon)[:3]]
    if season:
        out["closed_for_season"] = _join(season)
    return {**_compact(out), "places": entries}


def _mow_wait(data, t):
    current = _mow(t)
    return min((catalog._minute_of_week(p["open"]) - current) % WEEK for p in _periods(data))


def _find(args, now):
    places = _places()
    local = catalog.local_now(None, now).replace(tzinfo=None)
    when = parse_when(args["when"], local) if args.get("when") else None
    out = _named(places, args, local, when) if args.get("name") else _list(places, args, local, when)
    note = _note(places, now)
    if note:
        out["note"] = f"{out['note']} {note}" if out.get("note") else note
    return out


_RUNNERS = {FIND_RESTAURANTS: _find}


def run_tool(tool_id, args, mode=None):
    runner = _RUNNERS.get(tool_id)
    if runner is None:
        raise ToolError(f"Unknown tool {tool_id!r}")
    return runner(args or {}, utc_now())
