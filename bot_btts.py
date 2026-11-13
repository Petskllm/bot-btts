import csv
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — SPORTAPI — VERSION 6.2
#
# Marchés :
# - Domicile +0,5 but (72-85 %)
# - Extérieur +0,5 but (72-85 %)
# - BTTS Oui (72-85 %)
# - Plus de 1,5 but (72-85 %)
# - Équipe : plus de 2,5 tirs cadrés (70 % minimum)
#
# Nouveautés 6.2 :
# - Buteurs supprimés
# - H2H supprimé
# - Ajustement domicile/extérieur basé sur le journal v6.1
# - Nouveau marché : plus de 2,5 tirs cadrés par équipe
# - Cache disque des statistiques de tirs cadrés
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PROB = 0.72
MAX_PROB = 0.85

# Tirs cadrés : ligne unique 2,5, filtre 70 %
SOT_LINE = 2.5
SOT_MIN_PROB = 0.70
SOT_MAX_PROB = 0.92
SOT_HISTORY_N = 5
SOT_CACHE_FILE = "data/sot_stats_cache.json"

ODDS_MARGIN = 1.03
LOG_MIN_PROB = 0.65

MAX_MATCHES = 15
MAX_MARKETS = 3

MAX_PER_LEAGUE = 12
PRIORITY_LEAGUE_IDS = set()

EXCLUDE_YOUTH = True
YOUTH_PATTERN = re.compile(
    r"\bU[- ]?(1[5-9]|2[0-3])\b|\byouth\b|\bjuniors?\b"
    r"|\bprimavera\b|\breserves?\b|\bamateurs?\b|\bII$",
    re.IGNORECASE,
)

PRIOR_K = 4.0
PRIORS = {
    "gf": 1.35,
    "ga": 1.35,
    "btts": 0.52,
    "over15": 0.75,
}

HISTORY_N = 10
MIN_HISTORY = 6
VENUE_N = 6
MAX_HISTORY_DAYS = 240
MAX_STALE_DAYS = 60
DECAY = 0.90

# Ajustements mesurés sur le journal v6.1
HOME_ADJUST = 0.02
AWAY_ADJUST = -0.02
HOME_PROB_CAP = 0.88
AWAY_PROB_FLOOR = 0.60

MAX_API_CALLS = 350
MAX_SETTLE_DATES = 5
PENDING_EXPIRY_DAYS = 7
MODEL_VERSION = "6.2"
MIN_INTERVAL = 0.5
MAX_RUNTIME_SECONDS = 600
TIMEOUT = 20
MAX_RETRIES = 2
RETRY_PAUSE = 60
MAX_CONSECUTIVE_FAILS = 8

PREDICTIONS_FILE = "data/predictions.csv"
FIELDS = [
    "date", "fixture_id", "match", "league", "kickoff",
    "market", "player", "probability", "prob_base",
    "published", "model_version", "result",
]

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-history-bot/6.2",
})

CACHE = {}
TEAM_CACHE = {}
STANDINGS_CACHE = {}
SOT_CACHE = {}

ERRORS = []
REASONS = Counter()
CALLS = 0
LAST_CALL = None
BLOCKED = False
PARTIAL = False
FAILS = 0
STARTED = time.monotonic()


# ---------------- SECURITE / LOGS ----------------

def safe(value):
    text = str(value)
    for secret in (TOKEN, API_KEY, CHAT_ID):
        if secret:
            text = text.replace(secret, "[MASQUE]")
    return text


def error(message):
    message = safe(message)[:350]
    print("DIAGNOSTIC :", message, flush=True)
    if message not in ERRORS:
        ERRORS.append(message)


def budget_available():
    return (
        not BLOCKED
        and CALLS < MAX_API_CALLS
        and time.monotonic() - STARTED < MAX_RUNTIME_SECONDS
    )


# ---------------- TELEGRAM ----------------

def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        while len(line) > 3000:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:3000])
            line = line[3000:]

        if len(current) + len(line) > 3000:
            chunks.append(current)
            current = ""

        current += line

    if current:
        chunks.append(current)

    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk},
            timeout=TIMEOUT,
        )
        payload = response.json() if response.content else {}

        if not response.ok or not payload.get("ok"):
            raise RuntimeError(
                f"Telegram HTTP {response.status_code} : "
                + safe(payload.get("description", "erreur"))
            )


# ---------------- API ----------------

def failed(endpoint):
    global FAILS, BLOCKED
    CACHE[endpoint] = None
    FAILS += 1
    if FAILS >= MAX_CONSECUTIVE_FAILS:
        BLOCKED = True
        error("Trop d'echecs d'affilee : analyse arretee.")
    return None


def retry_pause(response):
    try:
        return max(RETRY_PAUSE, int(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return RETRY_PAUSE


def api_get(endpoint):
    global CALLS, LAST_CALL, BLOCKED, FAILS

    if endpoint in CACHE:
        return CACHE[endpoint]

    for attempt in range(MAX_RETRIES + 1):
        if not budget_available():
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
            if wait > 0:
                time.sleep(wait)

        LAST_CALL = time.monotonic()
        CALLS += 1
        print(f"API {CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

        try:
            response = SESSION.get(
                f"{BASE_URL}/{endpoint}",
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            error(f"{endpoint} : erreur reseau.")
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            return failed(endpoint)

        if response.status_code == 429:
            pause = retry_pause(response)
            elapsed = time.monotonic() - STARTED
            if attempt < MAX_RETRIES and elapsed + pause < MAX_RUNTIME_SECONDS:
                error(f"{endpoint} : limite atteinte, pause {pause} s.")
                time.sleep(pause)
                continue
            error(f"{endpoint} : limite de requetes depassee.")
            BLOCKED = True
            return None

        if response.status_code == 401:
            error("Cle API refusee (401).")
            BLOCKED = True
            return None

        if response.status_code >= 500 and attempt < MAX_RETRIES:
            error(f"{endpoint} : HTTP {response.status_code}, nouvel essai.")
            time.sleep(2 * (attempt + 1))
            continue

        if not response.ok:
            error(f"{endpoint} : HTTP {response.status_code}.")
            return failed(endpoint)

        try:
            payload = response.json()
        except ValueError:
            error(f"{endpoint} : JSON invalide.")
            return failed(endpoint)

        if not isinstance(payload, dict) or payload.get("success") is False:
            error(f"{endpoint} : reponse invalide.")
            return failed(endpoint)

        FAILS = 0
        CACHE[endpoint] = payload
        return payload

    return None


def get_list(payload, key="data"):
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return value if isinstance(value, list) else []


# ---------------- CACHE TIRS CADRÉS ----------------

def load_sot_cache():
    if SOT_CACHE:
        return SOT_CACHE

    if os.path.exists(SOT_CACHE_FILE):
        try:
            with open(SOT_CACHE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                SOT_CACHE.update(data)
        except (OSError, ValueError):
            pass

    return SOT_CACHE


def save_sot_cache():
    os.makedirs(os.path.dirname(SOT_CACHE_FILE), exist_ok=True)

    temp = SOT_CACHE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(SOT_CACHE, file, ensure_ascii=False)

    os.replace(temp, SOT_CACHE_FILE)


# ---------------- OUTILS ----------------

def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def kickoff_of(match):
    raw = match.get("kickoff_utc")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PARIS)


def date_of(match):
    kickoff = kickoff_of(match)
    if kickoff is not None:
        return kickoff.date()
    try:
        return datetime.fromisoformat(str(match.get("date"))[:10]).date()
    except ValueError:
        return None


def short_name(value):
    name = " ".join(str(value or "Equipe").split())
    return name if len(name) <= 25 else name[:24] + "…"


def is_youth(fixture):
    return any(
        YOUTH_PATTERN.search(str(fixture.get(field) or ""))
        for field in ("home_team", "away_team")
    )


def league_label(fixture):
    for key in ("league_name", "league", "competition"):
        value = fixture.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            return short_name(value)
    return None


def prioritise(fixtures):
    ordered = sorted(
        fixtures,
        key=lambda f: (
            0 if str(f.get("league_id")) in PRIORITY_LEAGUE_IDS else 1,
            kickoff_of(f),
        ),
    )

    kept = []
    taken = Counter()

    for fixture in ordered:
        league = str(fixture.get("league_id"))
        if (
            league not in PRIORITY_LEAGUE_IDS
            and taken[league] >= MAX_PER_LEAGUE
        ):
            REASONS["Plafond par ligue atteint"] += 1
            continue
        taken[league] += 1
        kept.append(fixture)

    return kept


def boolean(value):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


# ---------------- CLASSEMENT ----------------

def standings_row(league_id, team_id):
    key = str(league_id)
    if key not in STANDINGS_CACHE:
        payload = api_get(f"standings/{league_id}")
        STANDINGS_CACHE[key] = get_list(payload)

    for row in STANDINGS_CACHE[key]:
        if not isinstance(row, dict):
            continue
        team = row.get("team")
        if isinstance(team, dict) and str(team.get("id")) == str(team_id):
            return row
    return None


# ---------------- HISTORIQUE EQUIPE ----------------

def parse_results(matches, team_id, today, slug=None):
    history = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        if str(match.get("status") or "").upper() != "FT":
            continue

        date = date_of(match)
        if date is None:
            continue

        age = (today - date).days
        if age <= 0 or age > MAX_HISTORY_DAYS:
            continue

        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))
        if home_score is None or away_score is None:
            continue
        if home_score < 0 or away_score < 0:
            continue

        is_home = None
        if str(match.get("home_id")) == str(team_id):
            is_home = True
        elif str(match.get("away_id")) == str(team_id):
            is_home = False
        elif slug:
            if match.get("home_slug") == slug:
                is_home = True
            elif match.get("away_slug") == slug:
                is_home = False

        if is_home is None:
            continue

        history.append({
            "id": match.get("id"),
            "date": date,
            "venue": "home" if is_home else "away",
            "gf": home_score if is_home else away_score,
            "ga": away_score if is_home else home_score,
        })

    return history


def merge_history(*groups):
    unique = {}
    for group in groups:
        for match in group:
            identifier = match.get("id")
            if identifier is None:
                identifier = (
                    match["date"], match["venue"],
                    match["gf"], match["ga"],
                )
            unique[identifier] = match

    return sorted(unique.values(), key=lambda m: m["date"], reverse=True)


def history_from_form(row, today):
    history = []
    form = row.get("form") if isinstance(row, dict) else None
    if not isinstance(form, list):
        return history

    for match in form:
        if not isinstance(match, dict):
            continue

        date = date_of(match)
        home = boolean(match.get("is_home"))
        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))

        if None in (date, home, home_score, away_score):
            continue
        if home_score < 0 or away_score < 0:
            continue
        if not 0 < (today - date).days <= MAX_HISTORY_DAYS:
            continue

        history.append({
            "id": None,
            "date": date,
            "venue": "home" if home else "away",
            "gf": home_score if home else away_score,
            "ga": away_score if home else home_score,
        })

    return merge_history(history)


def team_history(team_id, league_id, today):
    key = str(team_id)
    if key in TEAM_CACHE:
        return TEAM_CACHE[key]

    payload = api_get(f"teams/{team_id}")
    history = parse_results(get_list(payload, "matches"), team_id, today)
    history = merge_history(history)

    team = payload.get("team") if isinstance(payload, dict) else {}
    team = team if isinstance(team, dict) else {}
    slug = team.get("slug")
    row = None

    if len(history) < HISTORY_N and not slug:
        row = standings_row(league_id, team_id)
        row_team = row.get("team") if isinstance(row, dict) else {}
        if isinstance(row_team, dict):
            slug = row_team.get("slug")

    if len(history) < HISTORY_N and slug:
        slug_payload = api_get(f"teams/slug/{quote(str(slug), safe='')}")
        extra = parse_results(
            get_list(slug_payload, "results"),
            team_id,
            today,
            slug=str(slug),
        )
        history = merge_history(history, extra)

    if len(history) < MIN_HISTORY:
        if row is None:
            row = standings_row(league_id, team_id)

        fallback = history_from_form(row, today)
        if len(fallback) > len(history):
            history = fallback

    print(
        f"HISTORIQUE FINAL EQUIPE {team_id} : "
        f"{len(history)} resultats exploitables",
        flush=True,
    )

    TEAM_CACHE[key] = history
    return history


# ---------------- MODELE EQUIPE ----------------

def weighted_stats(matches):
    weights = [DECAY ** i for i in range(len(matches))]
    total = sum(weights)

    def average(function):
        return sum(
            weight * function(match)
            for weight, match in zip(weights, matches)
        ) / total

    return {
        "gf": average(lambda m: m["gf"]),
        "ga": average(lambda m: m["ga"]),
        "btts": average(lambda m: float(m["gf"] > 0 and m["ga"] > 0)),
        "over15": average(lambda m: float(m["gf"] + m["ga"] >= 2)),
    }


def profile(history, venue, today):
    recent = history[:HISTORY_N]
    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    overall = weighted_stats(recent)

    local_matches = [
        m for m in history if m["venue"] == venue
    ][:VENUE_N]

    if len(local_matches) >= 3:
        venue_weight = min(0.50, len(local_matches) / (len(local_matches) + 6))
        local = weighted_stats(local_matches)
    else:
        venue_weight = 0
        local = overall

    result = {
        metric: (
            (1 - venue_weight) * overall[metric]
            + venue_weight * local[metric]
        )
        for metric in overall
    }

    n = len(recent)
    for metric, prior in PRIORS.items():
        result[metric] = (
            result[metric] * n + prior * PRIOR_K
        ) / (n + PRIOR_K)

    result["n"] = n
    result["venue_n"] = len(local_matches)
    return result


def estimate(home, away):
    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)

    # Ajustements v6.2 :
    # domicile marque sous-estime, extérieur marque surestime.
    p_home = min(p_home + HOME_ADJUST, HOME_PROB_CAP)
    p_away = max(p_away + AWAY_ADJUST, AWAY_PROB_FLOOR)

    total = lambda_home + lambda_away

    btts = (
        0.75 * p_home * p_away
        + 0.25 * (home["btts"] + away["btts"]) / 2
    )

    over15 = (
        0.75 * (1 - math.exp(-total) * (1 + total))
        + 0.25 * (home["over15"] + away["over15"]) / 2
    )

    return lambda_home, lambda_away, {
        "home": p_home,
        "away": p_away,
        "btts": btts,
        "over15": over15,
    }


# ---------------- TIRS CADRÉS ----------------

def match_sot(fixture_id):
    """Renvoie (tirs cadrés domicile, tirs cadrés extérieur)."""
    cache = load_sot_cache()
    key = str(fixture_id)

    if key in cache:
        value = cache[key]
        if isinstance(value, list) and len(value) == 2:
            return tuple(value)

    payload = api_get(f"fixtures/{fixture_id}/stats")
    if not isinstance(payload, dict):
        return None

    stats = payload.get("data")
    if not isinstance(stats, dict):
        stats = payload

    home = None
    away = None

    for side in ("home", "away"):
        block = stats.get(side)
        if not isinstance(block, dict):
            continue

        value = number(block.get("shots_on_target"))
        if value is None:
            continue

        if side == "home":
            home = value
        else:
            away = value

    if home is None or away is None:
        return None

    cache[key] = [home, away]
    save_sot_cache()
    return home, away


def sot_history(history):
    sot_for = []
    sot_against = []

    for match in history[:SOT_HISTORY_N]:
        fixture_id = match.get("id")
        if not fixture_id:
            continue

        result = match_sot(fixture_id)
        if result is None:
            continue

        home_sot, away_sot = result

        if match["venue"] == "home":
            sot_for.append(home_sot)
            sot_against.append(away_sot)
        else:
            sot_for.append(away_sot)
            sot_against.append(home_sot)

    return sot_for, sot_against


def weighted_sot(values):
    if not values:
        return None

    weights = [DECAY ** i for i in range(len(values))]
    total = sum(weights)

    return sum(w * v for w, v in zip(weights, values)) / total


def sot_profile(history, venue, today):
    recent = history[:HISTORY_N]
    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    sot_for, sot_against = sot_history(recent)

    if len(sot_for) < 3:
        return None

    overall_for = weighted_sot(sot_for)
    overall_against = weighted_sot(sot_against)

    local_for = []
    local_against = []

    for match, for_value, against_value in zip(recent, sot_for, sot_against):
        if match["venue"] == venue:
            local_for.append(for_value)
            local_against.append(against_value)

    if len(local_for) >= 3:
        venue_weight = min(0.45, len(local_for) / (len(local_for) + 6))
        for_value = weighted_sot(local_for)
        against_value = weighted_sot(local_against)
    else:
        venue_weight = 0
        for_value = overall_for
        against_value = overall_against

    n = len(sot_for)

    result = {
        "sot_for": (
            (1 - venue_weight) * overall_for
            + venue_weight * for_value
        ),
        "sot_against": (
            (1 - venue_weight) * overall_against
            + venue_weight * against_value
        ),
    }

    for key, prior in (("sot_for", 3.2), ("sot_against", 3.2)):
        result[key] = (
            result[key] * n + prior * PRIOR_K
        ) / (n + PRIOR_K)

    result["sot_n"] = n
    return result


def sot_probability(team, opponent):
    """Probabilité que l'équipe dépasse 2,5 tirs cadrés."""
    lambda_sot = (team["sot_for"] + opponent["sot_against"]) / 2
    lambda_sot = max(lambda_sot, 0.30)

    return 1 - math.exp(-lambda_sot) * (1 + lambda_sot)


# ---------------- COTE MINIMALE ----------------

def min_odds(probability):
    if probability <= 0:
        return None
    return math.ceil(ODDS_MARGIN / probability * 100) / 100


def fmt_odds(value):
    return f"{value:.2f}".replace(".", ",")


# ---------------- JOURNAL / CALIBRATION ----------------

OLD_FIELDS = [
    "date", "fixture_id", "match", "league", "kickoff",
    "market", "player", "probability", "model_version", "result",
]


def read_predictions():
    old_backup = PREDICTIONS_FILE.replace(".csv", "_ancien.csv")
    if os.path.exists(old_backup):
        recovered = read_old_format(old_backup)
        if recovered:
            current = read_predictions_file()
            known = {
                (r["date"], r["fixture_id"], r["market"], r["player"])
                for r in current
            }
            current += [
                r for r in recovered
                if (r["date"], r["fixture_id"], r["market"], r["player"])
                not in known
            ]
            write_predictions(current)
            os.replace(old_backup, old_backup + ".repris")
    return read_predictions_file()


def read_old_format(path):
    with open(path, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != OLD_FIELDS:
            return []
        rows = list(reader)
    for row in rows:
        row["prob_base"] = row.get("probability", "")
        row["published"] = 1
    return [{k: r.get(k, "") for k in FIELDS} for r in rows]


def read_predictions_file():
    if not os.path.exists(PREDICTIONS_FILE):
        return []
    with open(PREDICTIONS_FILE, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        header = reader.fieldnames
        rows = list(reader)

    if header == FIELDS:
        return rows

    if header == OLD_FIELDS:
        for row in rows:
            row["prob_base"] = row.get("probability", "")
            row["published"] = 1
        return [{k: r.get(k, "") for k in FIELDS} for r in rows]

    backup = PREDICTIONS_FILE.replace(".csv", "_ancien.csv")
    os.replace(PREDICTIONS_FILE, backup)
    return []


def write_predictions(rows):
    os.makedirs(os.path.dirname(PREDICTIONS_FILE), exist_ok=True)
    with open(PREDICTIONS_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def outcome(market, home_score, away_score):
    if market == "home":
        return home_score > 0
    if market == "away":
        return away_score > 0
    if market == "btts":
        return home_score > 0 and away_score > 0
    if market == "over15":
        return home_score + away_score >= 2
    return None


def settle_previous_predictions(today):
    rows = read_predictions()
    pending = []

    for row in rows:
        if row.get("result") not in ("pending", "", None):
            continue
        try:
            row["_date"] = datetime.fromisoformat(row["date"]).date()
        except (ValueError, KeyError):
            continue
        if row["_date"] < today:
            pending.append(row)

    if not pending:
        for row in rows:
            row.pop("_date", None)
        return

    dates = sorted({r["_date"] for r in pending})[:MAX_SETTLE_DATES]
    finished = {}
    dead = set()

    for day in dates:
        payload = api_get(f"fixtures/date/{day.isoformat()}")
        for fixture in get_list(payload):
            if not isinstance(fixture, dict):
                continue
            status = str(fixture.get("status") or "").upper()
            identifier = str(fixture.get("id"))
            if status == "FT":
                home = number(fixture.get("home_score"))
                away = number(fixture.get("away_score"))
                if home is not None and away is not None:
                    finished[identifier] = (home, away)
            elif status in {"POSTPONED", "CANCELLED", "CANCELED",
                            "ABANDONED", "SUSPENDED"}:
                dead.add(identifier)

    for row in pending:
        identifier = str(row.get("fixture_id"))
        age = (today - row["_date"]).days

        if identifier in dead:
            row["result"] = "annule"
            continue

        if identifier not in finished:
            if age > PENDING_EXPIRY_DAYS:
                row["result"] = "expire"
            continue

        home_score, away_score = finished[identifier]
        market = row.get("market")

        if market in ("sot_home", "sot_away"):
            sot = match_sot(identifier)

            if sot is None:
                continue

            home_sot, away_sot = sot
            value = home_sot if market == "sot_home" else away_sot

            row["result"] = "gagne" if value > SOT_LINE else "perdu"
            continue

        won = outcome(market, home_score, away_score)
        if won is None:
            continue
        row["result"] = "gagne" if won else "perdu"

    for row in rows:
        row.pop("_date", None)
    write_predictions(rows)

    settled = [r for r in rows if r.get("result") in ("gagne", "perdu")]
    if settled:
        wins = sum(r["result"] == "gagne" for r in settled)
        print(f"JOURNAL : {wins}/{len(settled)} gagnes au total.", flush=True)


# ---------------- ANALYSE ----------------

def analyse(fixture, today):
    home_history = team_history(
        fixture["home_id"], fixture["league_id"], today
    )
    away_history = team_history(
        fixture["away_id"], fixture["league_id"], today
    )

    home = profile(home_history, "home", today)
    away = profile(away_history, "away", today)

    if home is None or away is None:
        return None

    lambda_home, lambda_away, probabilities = estimate(home, away)

    picks = sorted(
        (
            (market, probability)
            for market, probability in probabilities.items()
            if MIN_PROB <= probability <= MAX_PROB
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:MAX_MARKETS]

    home_sot = sot_profile(home_history, "home", today)
    away_sot = sot_profile(away_history, "away", today)

    sot_picks = []

    if home_sot and away_sot:
        home_sot_prob = sot_probability(home_sot, away_sot)
        away_sot_prob = sot_probability(away_sot, home_sot)

        if SOT_MIN_PROB <= home_sot_prob <= SOT_MAX_PROB:
            sot_picks.append(("sot_home", home_sot_prob))

        if SOT_MIN_PROB <= away_sot_prob <= SOT_MAX_PROB:
            sot_picks.append(("sot_away", away_sot_prob))

    sot_picks.sort(key=lambda item: item[1], reverse=True)
    picks.extend(sot_picks[:1])

    print(
        "MODELE :",
        fixture.get("home_team"), "-", fixture.get("away_team"),
        "probabilites :", probabilities,
        "tirs cadrés :",
        {
            "home": home_sot,
            "away": away_sot,
        },
        flush=True,
    )

    return {
        "fixture": fixture,
        "picks": picks,
        "probabilities": probabilities,
        "base": probabilities,
        "best": picks[0][1] if picks else 0,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "home_sot": home_sot,
        "away_sot": away_sot,
    }


# ---------------- MESSAGE ----------------

def market_labels(fixture):
    home = short_name(fixture.get("home_team"))
    away = short_name(fixture.get("away_team"))

    return {
        "home": f"{home} +0,5 but",
        "away": f"{away} +0,5 but",
        "btts": "BTTS Oui",
        "over15": "Plus de 1,5 but",
        "sot_home": f"{home} : plus de 2,5 tirs cadrés",
        "sot_away": f"{away} : plus de 2,5 tirs cadrés",
    }


def build_message(selected, partial, analysed, total):
    lines = [
        "⚽ PRONOSTICS DU JOUR",
        datetime.now(PARIS).strftime("📅 %d/%m/%Y"),
        "",
    ]

    selected = sorted(
        selected, key=lambda r: (kickoff_of(r["fixture"]), -r["best"])
    )
    current_hour = None

    for index, result in enumerate(selected, 1):
        fixture = result["fixture"]
        home = short_name(fixture.get("home_team"))
        away = short_name(fixture.get("away_team"))
        start = kickoff_of(fixture)
        kickoff = start.strftime("%H:%M")

        if start.hour != current_hour:
            current_hour = start.hour
            lines.append(f"⏰ {current_hour:02d}h")

        lines.append(f"{index}. {home} — {away} · {kickoff}")

        league = league_label(fixture)
        if league:
            lines.append(f"🏆 {league}")

        labels = market_labels(fixture)

        for market, probability in result["picks"]:
            lines.append(
                f"• {labels[market]} : {probability:.0%}"
                f" · 💰 min {fmt_odds(min_odds(probability))}"
            )

        projection = (
            f"{result['lambda_home']:.2f} — {result['lambda_away']:.2f}"
        ).replace(".", ",")

        lines.extend([
            f"⚽ Buts projetés : {projection}",
            "",
        ])

    if not selected:
        lines.extend([
            "Aucun marché estimé dans les filtres actuels.",
            "",
        ])

    lines.append(f"📈 {analysed}/{total} matchs analysés")

    if partial:
        lines.append("⚠️ Analyse partielle.")

    lines.append(
        "💰 Ne joue que si la cote du bookmaker est au moins égale "
        "à la cote min : en dessous, le pari perd sur la durée."
    )
    lines.append("ℹ️ Heures de Paris. Sans garantie.")

    return "\n".join(lines)


def write_journal(all_results, selected, today):
    rows = read_predictions()
    known = {
        (r["date"], r["fixture_id"], r["market"], r["player"])
        for r in rows
    }
    published = {
        (str(r["fixture"].get("id")), m)
        for r in selected for m, _ in r["picks"]
    }

    for result in all_results:
        fixture = result["fixture"]
        fid = str(fixture.get("id"))
        base_info = {
            "date": today.isoformat(),
            "fixture_id": fid,
            "match": f"{fixture.get('home_team')} - {fixture.get('away_team')}",
            "league": fixture.get("league_name") or "",
            "kickoff": kickoff_of(fixture).strftime("%H:%M"),
            "model_version": MODEL_VERSION,
            "result": "pending",
        }

        all_markets = dict(result["probabilities"])

        if result.get("home_sot") and result.get("away_sot"):
            all_markets["sot_home"] = sot_probability(
                result["home_sot"], result["away_sot"]
            )
            all_markets["sot_away"] = sot_probability(
                result["away_sot"], result["home_sot"]
            )

        for market, probability in all_markets.items():
            if probability < LOG_MIN_PROB:
                continue

            key = (base_info["date"], fid, market, "")
            if key in known:
                continue

            rows.append({
                **base_info,
                "market": market,
                "player": "",
                "probability": f"{probability:.4f}",
                "prob_base": f"{probability:.4f}",
                "published": int((fid, market) in published),
            })
            known.add(key)

    write_predictions(rows)
    print(f"JOURNAL : {len(rows)} lignes au total.", flush=True)


# ---------------- EXECUTION ----------------

def main():
    global PARTIAL

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")
    if not API_KEY:
        send_message("❌ Secret API_KEY absent.")
        return

    now = datetime.now(PARIS)
    today = now.date()

    try:
        settle_previous_predictions(today)
    except Exception as exc:
        error("Verification du journal impossible : " + type(exc).__name__)

    payload = api_get(f"fixtures/date/{today.isoformat()}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        send_message("⚠️ Analyse indisponible : matchs SportAPI illisibles.")
        return

    fixtures = payload["data"]
    eligible = []
    seen = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue

        kickoff = kickoff_of(fixture)
        if kickoff is None:
            continue

        if kickoff.date() != today or kickoff <= now:
            continue

        if str(fixture.get("status") or "").casefold() != "scheduled":
            continue

        if any(
            fixture.get(field) is None
            for field in ("home_id", "away_id", "league_id")
        ):
            continue

        if EXCLUDE_YOUTH and is_youth(fixture):
            continue

        identifier = fixture.get("id") or (
            fixture["home_id"],
            fixture["away_id"],
            str(fixture.get("kickoff_utc")),
        )

        if identifier in seen:
            continue

        seen.add(identifier)
        eligible.append(fixture)

    eligible = prioritise(eligible)

    results = []
    all_results = []
    analysed = 0

    for fixture in eligible:
        if not budget_available():
            PARTIAL = True
            break

        if kickoff_of(fixture) <= datetime.now(PARIS):
            continue

        try:
            result = analyse(fixture, today)
        except Exception as exc:
            error("Lecture historique impossible : " + type(exc).__name__)
            continue

        if result is None:
            REASONS["Historique insuffisant ou trop ancien"] += 1
            continue

        analysed += 1
        all_results.append(result)

        if result["picks"]:
            results.append(result)

    if BLOCKED:
        PARTIAL = True

    results.sort(
        key=lambda r: (-r["best"], kickoff_of(r["fixture"]))
    )

    publication_time = datetime.now(PARIS)
    selected = [
        r for r in results
        if r["picks"] and kickoff_of(r["fixture"]) > publication_time
    ][:MAX_MATCHES]

    try:
        write_journal(all_results, selected, today)
    except Exception as exc:
        error("Ecriture du journal impossible : " + type(exc).__name__)

    print("\n===== BILAN FINAL =====", flush=True)
    print("MATCHS RECUS :", len(fixtures), flush=True)
    print("MATCHS A VENIR :", len(eligible), flush=True)
    print("MATCHS ANALYSABLES :", analysed, flush=True)
    print("MATCHS PUBLIES :", len(selected), flush=True)
    print("REQUETES :", CALLS, flush=True)
    print("ANALYSE PARTIELLE :", PARTIAL, flush=True)
    print("DIAGNOSTIC :", dict(REASONS), flush=True)
    print("ERREURS :", ERRORS, flush=True)

    send_message(
        build_message(selected, PARTIAL, analysed, len(eligible))
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERREUR FATALE :", type(exc).__name__, safe(exc), flush=True)
        raise SystemExit(1)
