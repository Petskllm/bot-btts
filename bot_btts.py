import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — BTTS / ÉQUIPE +0,5 / OVER 1,5
# API : SportAPI (sportapi.ai)
# Plan gratuit : 2 000 requêtes/jour, 14 000/mois
# Publication : Telegram
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PERCENT = 70
MAX_MATCHES = 10
MAX_MARKETS_PER_MATCH = 2
CANDIDATE_MATCHES = 25

MAX_API_CALLS = 100
MIN_INTERVAL = 0.5
MAX_RETRIES = 2
REQUEST_TIMEOUT = 25

# Ligues suivies : on filtre par nom car les IDs peuvent varier
LEAGUE_NAMES = {
    "premier league": "Premier League",
    "la liga": "La Liga",
    "laliga": "La Liga",
    "serie a": "Serie A",
    "bundesliga": "Bundesliga",
    "ligue 1": "Ligue 1",
    "uefa champions league": "Ligue des Champions",
    "champions league": "Ligue des Champions",
    "uefa europa league": "Ligue Europa",
    "europa league": "Ligue Europa",
    "uefa conference league": "Conference League",
    "conference league": "Conference League",
    "primeira liga": "Primeira Liga",
    "eredivisie": "Eredivisie",
    "jupiler pro league": "Jupiler Pro League",
    "süper lig": "Süper Lig",
    "super lig": "Süper Lig",
    "swiss super league": "Super League Suisse",
    "austrian football bundesliga": "Bundesliga Autriche",
    "ekstraklasa": "Ekstraklasa",
    "allsvenskan": "Allsvenskan",
    "eliteserien": "Eliteserien",
    "brasileirão série a": "Brasileirão",
    "brasileirao serie a": "Brasileirão",
}

LEAGUE_PRIORITY = {
    "Premier League": 1,
    "La Liga": 1,
    "Serie A": 1,
    "Bundesliga": 1,
    "Ligue 1": 1,
    "Ligue des Champions": 1,
    "Ligue Europa": 2,
    "Conference League": 2,
    "Primeira Liga": 2,
    "Eredivisie": 2,
    "Jupiler Pro League": 3,
    "Süper Lig": 3,
    "Super League Suisse": 3,
    "Bundesliga Autriche": 3,
    "Ekstraklasa": 4,
    "Allsvenskan": 4,
    "Eliteserien": 4,
    "Brasileirão": 4,
}

W_POISSON = 0.55
W_FORM = 0.45

W_OVER_POISSON = 0.60
W_OVER_FORM = 0.40

PROB_MIN = 0.01
PROB_MAX = 0.99
DEFAULT_GOALS = 1.35

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY or "",
    "User-Agent": "football-stat-bot-sportapi/1.0",
})

CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False
LAST_CALL = None

NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
           "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

MARKET_LABELS = {
    "home_over_05": "Domicile +0,5 but",
    "away_over_05": "Extérieur +0,5 but",
    "btts": "BTTS — Oui",
    "over_15": "Plus de 1,5 but",
}


# ---------------- TELEGRAM ----------------

def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret TELEGRAM_TOKEN ou CHAT_ID absent.")

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        while len(line) > 3500:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:3500])
            line = line[3500:]

        if len(current) + len(line) > 3500:
            chunks.append(current)
            current = ""

        current += line

    if current:
        chunks.append(current)

    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk},
            timeout=REQUEST_TIMEOUT,
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or not payload.get("ok"):
            raise RuntimeError(
                f"Erreur Telegram : HTTP {response.status_code}"
            )


# ---------------- DIAGNOSTICS ----------------

def add_error(message):
    message = str(message)

    for secret in (API_KEY, TOKEN):
        if secret:
            message = message.replace(secret, "[MASQUÉ]")

    message = message[:200]

    if message not in ERRORS:
        ERRORS.append(message)

    print(f"DIAGNOSTIC : {message}", flush=True)


def diagnostics_text():
    return (
        "\n\n🛠️ Diagnostic : " + " ; ".join(ERRORS[:2])
    ) if ERRORS else ""


# ---------------- API ----------------

def api_get(endpoint, params=None):
    global API_CALLS, API_BLOCKED, LAST_CALL

    params = params or {}
    cache_key = (endpoint, tuple(sorted(params.items())))

    if cache_key in CACHE:
        return CACHE[cache_key]

    if API_BLOCKED:
        return None

    if not API_KEY:
        add_error("Secret API_KEY absent.")
        return None

    for attempt in range(MAX_RETRIES + 1):
        if API_CALLS >= MAX_API_CALLS:
            add_error("Budget maximal d'appels API atteint.")
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
            if wait > 0:
                time.sleep(wait)

        API_CALLS += 1
        LAST_CALL = time.monotonic()
        print(f"API {API_CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

        try:
            response = SESSION.get(
                f"{BASE_URL}/{endpoint}",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            add_error(f"{endpoint} : erreur réseau ({type(exc).__name__}).")

            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
                continue

            return None

        if response.status_code == 429:
            add_error(f"{endpoint} : quota API atteint.")
            API_BLOCKED = True
            return None

        if response.status_code in (401, 403):
            add_error("Clé API refusée ou invalide.")
            API_BLOCKED = True
            return None

        if not response.ok:
            add_error(f"{endpoint} : HTTP {response.status_code}.")
            return None

        try:
            payload = response.json()
        except ValueError:
            add_error(f"{endpoint} : JSON invalide.")
            return None

        CACHE[cache_key] = payload
        return payload

    return None


# ---------------- OUTILS ----------------

def clamp_probability(p):
    return max(PROB_MIN, min(PROB_MAX, p))


def normalize(text):
    return str(text or "").strip().lower()


def league_name(fixture):
    league = fixture.get("league") or {}
    competition = fixture.get("competition") or {}

    raw_name = (
        league.get("name")
        or competition.get("name")
        or ""
    )

    return LEAGUE_NAMES.get(
        normalize(raw_name),
        raw_name,
    )


def kickoff_of(fixture):
    raw = (
        fixture.get("startTime")
        or fixture.get("start_time")
        or fixture.get("date")
        or fixture.get("kickoff_time")
    )

    if not raw:
        raise ValueError("Date de match absente.")

    if raw.endswith("Z"):
        raw = raw.replace("Z", "+00:00")

    return datetime.fromisoformat(raw).astimezone(PARIS)


def fixture_id(fixture):
    return (
        fixture.get("id")
        or fixture.get("fixtureId")
        or fixture.get("matchId")
    )


def home_team(fixture):
    return fixture.get("homeTeam") or fixture.get("home") or {}


def away_team(fixture):
    return fixture.get("awayTeam") or fixture.get("away") or {}


def team_name(team):
    return (
        team.get("name")
        or team.get("shortName")
        or "Équipe"
    )


def team_id(team):
    return team.get("id") or team.get("teamId")


# ---------------- MATCHS DU JOUR ----------------

def get_today_fixtures():
    now = datetime.now(PARIS)
    today = now.date()

    data = api_get(f"fixtures/date/{today.isoformat()}")

    if not isinstance(data, list):
        if isinstance(data, dict):
            data = (
                data.get("fixtures")
                or data.get("matches")
                or data.get("response")
                or []
            )
        else:
            data = []

    selected = []

    for fixture in data:
        try:
            name = league_name(fixture)

            if name not in LEAGUE_PRIORITY:
                continue

            kickoff = kickoff_of(fixture)

            if kickoff.date() != today:
                continue

            if kickoff <= now:
                continue

            if not fixture_id(fixture):
                continue

            if not team_id(home_team(fixture)):
                continue

            if not team_id(away_team(fixture)):
                continue

            selected.append(fixture)

        except (KeyError, TypeError, ValueError):
            continue

    def priority(fixture):
        return LEAGUE_PRIORITY.get(
            league_name(fixture),
            5,
        )

    selected.sort(key=lambda x: (
        priority(x),
        kickoff_of(x),
    ))

    return selected[:CANDIDATE_MATCHES], len(data)


# ---------------- CLASSEMENT ET FORME ----------------

def find_team_row(standings, team_id_value, team_name_value):
    rows = []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("team", "home", "away") and isinstance(item, dict):
                    row_id = item.get("id") or item.get("teamId")
                    row_name = item.get("name") or item.get("shortName")

                    if row_id == team_id_value or (
                        row_name
                        and normalize(row_name) == normalize(team_name_value)
                    ):
                        rows.append(value)

                collect(item)

        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(standings)
    return rows[0] if rows else None


def get_number(row, *keys):
    value = row

    for key in keys:
        if not isinstance(value, dict):
            return None

        value = value.get(key)

    try:
        if value is None:
            return None

        return float(value)
    except (TypeError, ValueError):
        return None


def team_form(row):
    form = (
        row.get("form")
        or row.get("recentForm")
        or row.get("lastMatches")
        or []
    )

    if isinstance(form, str):
        form = list(form)

    return form[:6]


def form_goals(form, team_name_value):
    scored = 0
    conceded = 0
    btts = 0
    over15 = 0
    played = 0

    for match in form:
        if not isinstance(match, dict):
            continue

        home_name = team_name(match.get("homeTeam") or match.get("home") or {})
        away_name = team_name(match.get("awayTeam") or match.get("away") or {})

        home_goals = get_number(match, "homeScore") or get_number(
            match, "score", "home"
        )
        away_goals = get_number(match, "awayScore") or get_number(
            match, "score", "away"
        )

        if home_goals is None or away_goals is None:
            continue

        played += 1

        if normalize(home_name) == normalize(team_name_value):
            gf, ga = home_goals, away_goals
        else:
            gf, ga = away_goals, home_goals

        scored += gf
        conceded += ga

        if gf > 0 and ga > 0:
            btts += 1

        if gf + ga >= 2:
            over15 += 1

    if played == 0:
        return None

    return {
        "played": played,
        "gf": scored / played,
        "ga": conceded / played,
        "btts": btts / played,
        "over15": over15 / played,
    }


def get_team_profile(league_id, team):
    team_id_value = team_id(team)
    name = team_name(team)

    data = api_get(f"standings/{league_id}")

    if not data:
        return None

    row = find_team_row(data, team_id_value, name)

    if not row:
        return None

    form = form_goals(team_form(row), name)

    if not form or form["played"] < 4:
        return None

    gf = get_number(row, "goalsFor") or get_number(
        row, "all", "goals", "for"
    )
    ga = get_number(row, "goalsAgainst") or get_number(
        row, "all", "goals", "against"
    )
    played = get_number(row, "played") or get_number(
        row, "all", "played"
    )

    if gf is not None and ga is not None and played and played >= 6:
        season_gf = gf / played
        season_ga = ga / played

        profile_gf = (form["gf"] * form["played"] + season_gf * 6) / (
            form["played"] + 6
        )
        profile_ga = (form["ga"] * form["played"] + season_ga * 6) / (
            form["played"] + 6
        )
    else:
        profile_gf = form["gf"]
        profile_ga = form["ga"]

    return {
        "name": name,
        "gf": profile_gf,
        "ga": profile_ga,
        "btts": form["btts"],
        "over15": form["over15"],
        "played": form["played"],
        "form": form,
    }


# ---------------- MODÈLE ----------------

def poisson_markets(lambda_home, lambda_away):
    p_home_scores = 1.0 - math.exp(-lambda_home)
    p_away_scores = 1.0 - math.exp(-lambda_away)
    total = lambda_home + lambda_away

    p_over_15 = 1.0 - math.exp(-total) * (1.0 + total)

    return {
        "home_over_05": clamp_probability(p_home_scores),
        "away_over_05": clamp_probability(p_away_scores),
        "btts_poisson": clamp_probability(
            p_home_scores * p_away_scores
        ),
        "over_15": clamp_probability(p_over_15),
    }


def analyse_fixture(fixture):
    league = fixture.get("league") or {}
    competition = fixture.get("competition") or {}

    league_id = (
        league.get("id")
        or competition.get("id")
        or fixture.get("leagueId")
    )

    if not league_id:
        return None

    home = home_team(fixture)
    away = away_team(fixture)

    home_profile = get_team_profile(league_id, home)
    away_profile = get_team_profile(league_id, away)

    if not home_profile or not away_profile:
        return None

    lambda_home = max(
        0.2,
        min(3.5, (home_profile["gf"] + away_profile["ga"]) / 2),
    )

    lambda_away = max(
        0.2,
        min(3.5, (away_profile["gf"] + home_profile["ga"]) / 2),
    )

    poisson = poisson_markets(lambda_home, lambda_away)

    p_btts_form = (
        home_profile["btts"] + away_profile["btts"]
    ) / 2

    p_btts = (
        W_POISSON * poisson["btts_poisson"]
        + W_FORM * p_btts_form
    )
    p_btts = clamp_probability(p_btts)

    p_over_form = (
        home_profile["over15"] + away_profile["over15"]
    ) / 2

    p_over_15 = (
        W_OVER_POISSON * poisson["over_15"]
        + W_OVER_FORM * p_over_form
    )
    p_over_15 = clamp_probability(p_over_15)

    home_name = team_name(home)
    away_name = team_name(away)
    league_display = league_name(fixture)

    markets = {
        "home_over_05": poisson["home_over_05"],
        "away_over_05": poisson["away_over_05"],
        "btts": p_btts,
        "over_15": p_over_15,
    }

    details = {
        "btts": {
            "home": f"{round(home_profile['btts'] * 100)} %",
            "away": f"{round(away_profile['btts'] * 100)} %",
        },
        "home_over_05": {
            "home": f"{home_profile['gf']:.2f} but/match",
            "away": f"{away_profile['ga']:.2f} but/match",
        },
        "away_over_05": {
            "home": f"{away_profile['gf']:.2f} but/match",
            "away": f"{home_profile['ga']:.2f} but/match",
        },
        "over_15": {
            "home": f"{round(home_profile['over15'] * 100)} %",
            "away": f"{round(away_profile['over15'] * 100)} %",
        },
    }

    return {
        "id": fixture_id(fixture),
        "match": f"{home_name} - {away_name}",
        "home": home_name,
        "away": away_name,
        "league": league_display,
        "kickoff": kickoff_of(fixture),
        "markets": markets,
        "details": details,
        "warnings": [],
        "sample": f"{home_profile['played']} matchs de forme",
        "lambdas": (lambda_home, lambda_away),
    }


# ---------------- SÉLECTION ----------------

def build_candidates(fixtures):
    candidates = {key: [] for key in MARKET_LABELS}
    analysed = 0
    partial = False

    for fixture in fixtures:
        if API_BLOCKED or API_CALLS + 2 > MAX_API_CALLS:
            partial = True
            break

        try:
            result = analyse_fixture(fixture)
        except (
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
            OverflowError,
        ) as exc:
            add_error(f"Analyse impossible ({type(exc).__name__}).")
            result = None

        if not result:
            continue

        analysed += 1

        for market, probability in result["markets"].items():
            percent = int(round(probability * 100))

            if percent >= MIN_PERCENT:
                candidates[market].append({
                    **result,
                    "market": market,
                    "percent": percent,
                })

    return candidates, analysed, partial


# ---------------- MESSAGE TELEGRAM ----------------

def build_message(today, candidates, analysed, total_fixtures, partial):
    market_order = (
        "home_over_05",
        "away_over_05",
        "btts",
        "over_15",
    )

    market_display = {
        "home_over_05": lambda item, percent: (
            f"⚽️ {item['home']} marque : {percent} %"
        ),
        "away_over_05": lambda item, percent: (
            f"⚽️ {item['away']} marque : {percent} %"
        ),
        "btts": lambda item, percent: f"🔥 BTTS : {percent} %",
        "over_15": lambda item, percent: f"🥅 Over 1,5 : {percent} %",
    }

    matches = {}

    for market in market_order:
        for candidate in candidates.get(market, []):
            match_id = candidate.get("id", candidate["match"])

            if match_id not in matches:
                matches[match_id] = {
                    "item": candidate,
                    "markets": [],
                    "best_percent": 0,
                }

            percent = candidate["percent"]
            matches[match_id]["markets"].append((market, percent))
            matches[match_id]["best_percent"] = max(
                matches[match_id]["best_percent"],
                percent,
            )

    for match_id in matches:
        markets = matches[match_id]["markets"]
        markets.sort(key=lambda x: x[1], reverse=True)
        matches[match_id]["markets"] = markets[:MAX_MARKETS_PER_MATCH]

    ordered_all = sorted(
        matches.values(),
        key=lambda entry: (
            entry["best_percent"],
            entry["item"]["kickoff"],
        ),
        reverse=True,
    )

    selected_matches = ordered_all[:MAX_MATCHES]
    selected_matches.sort(
        key=lambda entry: entry["item"]["kickoff"]
    )

    lines = [
        "⚽ PRONOSTICS DU JOUR",
        f"📅 {today}",
        "",
    ]

    if not selected_matches:
        lines.append(
            f"Aucun pronostic n’atteint {MIN_PERCENT} % aujourd’hui."
        )
    else:
        index = 0

        for entry in selected_matches:
            item = entry["item"]
            index += 1

            number = NUMBERS[(index - 1) % len(NUMBERS)]
            kickoff = item["kickoff"].strftime("%H:%M")

            lines.append(f"{number} {item['match']}")
            lines.append(f"🏆 {item['league']} · 🕘 {kickoff}")
            lines.append("")

            for market, percent in entry["markets"]:
                lines.append(market_display[market](item, percent))

                details = item.get("details", {}).get(market, {})
                home_stat = details.get("home", "N/A")
                away_stat = details.get("away", "N/A")

                lines.append(
                    f"📊 Forme : {home_stat} et {away_stat}"
                )

                lambda_home, lambda_away = item.get(
                    "lambdas", (0, 0)
                )

                lines.append(
                    f"⚽️ But attendu : "
                    f"{lambda_home:.2f} - {lambda_away:.2f}"
                )
                lines.append("")

        if index > len(NUMBERS):
            lines.append("⚠️ Plus de 10 matchs : numérotation répétée.")

    displayed_picks = sum(
        len(entry["markets"]) for entry in selected_matches
    )

    lines.append(
        f"📊 {displayed_picks} pronostics · "
        f"{len(selected_matches)} matchs"
    )

    if partial:
        lines.append(
            "⚠️ Analyse partielle : limite d’appels API atteinte."
        )

    lines.append(
        "ℹ️ Pourcentages estimés par le modèle, sans garantie."
    )

    return "\n".join(lines) + diagnostics_text()


# ---------------- EXÉCUTION ----------------

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Ajoute les secrets TELEGRAM_TOKEN et CHAT_ID."
        )

    if not API_KEY:
        send_message(
            "Bot football : secret GitHub API_KEY absent."
        )
        raise RuntimeError("API_KEY absent.")

    today = datetime.now(PARIS).strftime("%d/%m/%Y")

    fixtures, total = get_today_fixtures()

    if fixtures is None:
        send_message(
            f"⚽ Analyse football — {today}\n\n"
            "❌ Impossible de récupérer les matchs du jour."
            + diagnostics_text()
        )
        return

    if not fixtures:
        send_message(
            f"⚽ Analyse football — {today}\n\n"
            "😴 Aucun match à venir trouvé dans les championnats suivis."
            + diagnostics_text()
        )
        return

    candidates, analysed, partial = build_candidates(fixtures)

    send_message(
        build_message(
            today,
            candidates,
            analysed,
            total,
            partial,
        )
    )

    print(
        f"Terminé : {API_CALLS} appels API, "
        f"{analysed} matchs analysés.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"ERREUR FATALE : {type(exc).__name__}",
            flush=True,
        )
        raise
