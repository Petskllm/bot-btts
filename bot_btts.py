import csv
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — SPORTAPI — VERSION 6.0
#
# Marchés :
# - équipe +0,5 but (72-85 %)
# - BTTS Oui (72-85 %)
# - Plus de 1,5 but (72-85 %)
# - Buteur (60 % minimum)
#
# Ajouts 6.0 :
# - H2H dans BTTS et Over 1,5
# - Meilleurs buteurs de la ligue
# - Buteur retenu si probabilite >= 60 %
# - Tirs du buteur retenu recuperes pour calibration
# - Journal data/predictions.csv pour mesurer la calibration
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PROB = 0.72
MAX_PROB = 0.85
MIN_SCORER_PROB = 0.60

MAX_MATCHES = 15
MAX_MARKETS = 2
MAX_SCORERS_PER_MATCH = 1

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

H2H_N = 5
H2H_WEIGHT = 0.15
MIN_H2H = 3

TOP_SCORERS_N = 30
SCORER_MIN_GOALS = 2

MAX_API_CALLS = 350
MIN_INTERVAL = 0.5
MAX_RUNTIME_SECONDS = 600
TIMEOUT = 20
MAX_RETRIES = 2
RETRY_PAUSE = 60
MAX_CONSECUTIVE_FAILS = 8

PREDICTIONS_FILE = "data/predictions.csv"

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-history-bot/6.0",
})

CACHE = {}
TEAM_CACHE = {}
STANDINGS_CACHE = {}
TOP_SCORERS_CACHE = {}
H2H_CACHE = {}

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
    counts = Counter(str(f.get("league_id")) for f in fixtures)
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


# ---------------- H2H ----------------

def h2h_stats(home_id, away_id):
    key = f"{home_id}-{away_id}"
    if key in H2H_CACHE:
        return H2H_CACHE[key]

    payload = api_get(f"fixtures/h2h/{home_id}/{away_id}")
    matches = get_list(payload)

    btts = []
    over15 = []

    for match in matches:
        if str(match.get("status") or "").upper() != "FT":
            continue

        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))
        if home_score is None or away_score is None:
            continue

        btts.append(float(home_score > 0 and away_score > 0))
        over15.append(float(home_score + away_score >= 2))

        if len(btts) >= H2H_N:
            break

    result = None
    if len(btts) >= MIN_H2H:
        result = {
            "btts": sum(btts) / len(btts),
            "over15": sum(over15) / len(over15),
            "n": len(btts),
        }

    H2H_CACHE[key] = result
    return result


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


def estimate(home, away, h2h):
    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)
    total = lambda_home + lambda_away

    btts = (
        0.75 * p_home * p_away
        + 0.25 * (home["btts"] + away["btts"]) / 2
    )

    over15 = (
        0.75 * (1 - math.exp(-total) * (1 + total))
        + 0.25 * (home["over15"] + away["over15"]) / 2
    )

    if h2h:
        btts = (1 - H2H_WEIGHT) * btts + H2H_WEIGHT * h2h["btts"]
        over15 = (1 - H2H_WEIGHT) * over15 + H2H_WEIGHT * h2h["over15"]

    return lambda_home, lambda_away, {
        "home": p_home,
        "away": p_away,
        "btts": btts,
        "over15": over15,
    }


# ---------------- BUTEURS ----------------

def top_scorers(league_id):
    key = str(league_id)
    if key in TOP_SCORERS_CACHE:
        return TOP_SCORERS_CACHE[key]

    payload = api_get(f"leagues/{league_id}/topscorers?limit={TOP_SCORERS_N}")
    rows = get_list(payload)

    by_team = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue

        goals = number(row.get("goals"))
        team_id = row.get("team_id")
        player_name = row.get("player_name")

        if goals is None or goals < SCORER_MIN_GOALS:
            continue
        if not team_id or not player_name:
            continue

        by_team[str(team_id)].append({
            "name": str(player_name),
            "team_id": str(team_id),
            "goals": goals,
        })

    for team_id in by_team:
        by_team[team_id].sort(key=lambda p: -p["goals"])

    TOP_SCORERS_CACHE[key] = by_team
    return by_team


def scorer_candidates(fixture, lambda_home, lambda_away):
    scorers = top_scorers(fixture.get("league_id"))
    candidates = []

    team_goals = {
        str(fixture["home_id"]): max(lambda_home, 0.05),
        str(fixture["away_id"]): max(lambda_away, 0.05),
    }

    for side, team_id in (
        ("home", str(fixture["home_id"])),
        ("away", str(fixture["away_id"])),
    ):
        players = scorers.get(team_id, [])[:3]
        total_goals = sum(p["goals"] for p in players) or 1

        for player in players:
            share = player["goals"] / total_goals
            share = min(max(share, 0.15), 0.55)

            probability = 1 - math.exp(-team_goals[team_id] * share)

            candidates.append({
                "side": side,
                "player": player["name"],
                "goals": player["goals"],
                "probability": probability,
            })

    candidates.sort(key=lambda p: -p["probability"])
    return candidates[:MAX_SCORERS_PER_MATCH]


def player_shots(player_name, team_id):
    endpoint = (
        "players/stats?name="
        + quote(str(player_name), safe="")
        + f"&team_id={team_id}"
    )

    payload = api_get(endpoint)
    if not isinstance(payload, dict):
        return None

    totals = payload.get("totals")
    if not isinstance(totals, dict):
        return None

    return {
        "goals": number(totals.get("goals")),
        "shots": number(totals.get("shots_total")),
        "appearances": number(totals.get("appearances")),
    }


# ---------------- CALIBRATION ----------------

def ensure_predictions_file():
    os.makedirs(os.path.dirname(PREDICTIONS_FILE), exist_ok=True)

    if not os.path.exists(PREDICTIONS_FILE):
        with open(PREDICTIONS_FILE, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "date", "fixture_id", "match", "league",
                    "kickoff", "market", "player",
                    "probability", "model_version", "result",
                ],
            )
            writer.writeheader()


def append_prediction(row):
    ensure_predictions_file()

    with open(PREDICTIONS_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "date", "fixture_id", "match", "league",
            "kickoff", "market", "player",
            "probability", "model_version", "result",
        ])
        writer.writerow(row)


def settle_previous_predictions(today):
    ensure_predictions_file()

    if not os.path.exists(PREDICTIONS_FILE):
        return

    with open(PREDICTIONS_FILE, newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    changed = False

    for row in rows:
        if row.get("result") not in (None, "", "pending"):
            continue

        fixture_id = row.get("fixture_id")
        if not fixture_id:
            continue

        try:
            match_date = datetime.fromisoformat(row["date"]).date()
        except ValueError:
            continue

        if match_date >= today:
            continue

        payload = api_get(f"fixtures/{fixture_id}")
        fixture = payload.get("fixture") if isinstance(payload, dict) else None

        if not isinstance(fixture, dict):
            continue

        if str(fixture.get("status") or "").upper() not in ("FT", "AET", "PENS"):
            continue

        home_score = number(fixture.get("home_score"))
        away_score = number(fixture.get("away_score"))

        if home_score is None or away_score is None:
            continue

        market = row.get("market")
        player = row.get("player") or ""

        if market == "home":
            result = "gagne" if home_score > 0 else "perdu"
        elif market == "away":
            result = "gagne" if away_score > 0 else "perdu"
        elif market == "btts":
            result = (
                "gagne"
                if home_score > 0 and away_score > 0
                else "perdu"
            )
        elif market == "over15":
            result = (
                "gagne"
                if home_score + away_score >= 2
                else "perdu"
            )
        elif market == "buteur":
            events = get_list(api_get(f"fixtures/{fixture_id}/events"))
            scored = any(
                isinstance(event, dict)
                and str(event.get("event_type") or "").lower() == "goal"
                and str(event.get("player_name") or "").strip().lower()
                == player.strip().lower()
                for event in events
            )
            result = "gagne" if scored else "perdu"
        else:
            continue

        row["result"] = result
        changed = True

    if changed:
        with open(PREDICTIONS_FILE, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "date", "fixture_id", "match", "league",
                "kickoff", "market", "player",
                "probability", "model_version", "result",
            ])
            writer.writeheader()
            writer.writerows(rows)


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

    h2h = h2h_stats(fixture["home_id"], fixture["away_id"])
    lambda_home, lambda_away, probabilities = estimate(home, away, h2h)

    picks = sorted(
        (
            (market, probability)
            for market, probability in probabilities.items()
            if MIN_PROB <= probability <= MAX_PROB
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:MAX_MARKETS]

    scorer = None

    if lambda_home + lambda_away >= 1.20:
        candidates = scorer_candidates(
            fixture, lambda_home, lambda_away
        )

        if candidates and candidates[0]["probability"] >= MIN_SCORER_PROB:
            scorer = candidates[0]

    print(
        "MODELE :",
        fixture.get("home_team"), "-", fixture.get("away_team"),
        "probabilites :", probabilities,
        "buteur :", scorer,
        flush=True,
    )

    return {
        "fixture": fixture,
        "picks": picks,
        "scorer": scorer,
        "best": picks[0][1] if picks else 0,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
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
    }


def build_message(selected, partial, analysed, total):
    lines = [
        "⚽ PRONOSTICS DU JOUR",
        datetime.now(PARIS).strftime("📅 %d/%m/%Y"),
        "",
    ]

    for index, result in enumerate(selected, 1):
        fixture = result["fixture"]
        home = short_name(fixture.get("home_team"))
        away = short_name(fixture.get("away_team"))
        kickoff = kickoff_of(fixture).strftime("%H:%M")

        lines.append(f"{index}. {home} — {away} · {kickoff}")

        league = league_label(fixture)
        if league:
            lines.append(f"🏆 {league}")

        labels = market_labels(fixture)

        for market, probability in result["picks"]:
            lines.append(f"• {labels[market]} : {probability:.0%}")

        if result["scorer"]:
            scorer = result["scorer"]
            lines.append(
                f"• ⚽ Buteur : {scorer['player']} : "
                f"{scorer['probability']:.0%}"
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
        "ℹ️ Estimations non calibrées, sans garantie. "
        "Heures de Paris."
    )

    return "\n".join(lines)


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

    settle_previous_predictions(today)

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

        if result["picks"] or result["scorer"]:
            results.append(result)

    if BLOCKED:
        PARTIAL = True

    results.sort(
        key=lambda r: (-r["best"], kickoff_of(r["fixture"]))
    )

    publication_time = datetime.now(PARIS)
    selected = [
        r for r in results
        if kickoff_of(r["fixture"]) > publication_time
    ][:MAX_MATCHES]

    for result in selected:
        fixture = result["fixture"]
        match_name = (
            f"{fixture.get('home_team')} - {fixture.get('away_team')}"
        )

        for market, probability in result["picks"]:
            append_prediction({
                "date": today.isoformat(),
                "fixture_id": fixture.get("id"),
                "match": match_name,
                "league": fixture.get("league_name"),
                "kickoff": kickoff_of(fixture).strftime("%H:%M"),
                "market": market,
                "player": "",
                "probability": f"{probability:.4f}",
                "model_version": "6.0",
                "result": "pending",
            })

        if result["scorer"]:
            scorer = result["scorer"]
            team_id = (
                fixture["home_id"]
                if scorer["side"] == "home"
                else fixture["away_id"]
            )

            shots = player_shots(scorer["player"], team_id)

            append_prediction({
                "date": today.isoformat(),
                "fixture_id": fixture.get("id"),
                "match": match_name,
                "league": fixture.get("league_name"),
                "kickoff": kickoff_of(fixture).strftime("%H:%M"),
                "market": "buteur",
                "player": scorer["player"],
                "probability": f"{scorer['probability']:.4f}",
                "model_version": "6.0",
                "result": "pending",
            })

            print(
                f"BUTEUR RETENU : {scorer['player']} | "
                f"tirs carriere : {shots}",
                flush=True,
            )

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
