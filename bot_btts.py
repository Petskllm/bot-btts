import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — BTTS / ÉQUIPE +0,5 / OVER 1,5
# API : SportAPI (sportapi.ai)
# Plan gratuit : 2 000 requêtes/jour
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

LEAGUES = {
    "Premier League": 1,
    "Spanish La Liga": 1,
    "Serie A": 1,
    "Bundesliga": 1,
    "Ligue 1": 1,
    "UEFA Champions League": 1,
    "UEFA Europa League": 2,
    "UEFA Conference League": 2,
    "Primeira Liga": 2,
    "Eredivisie": 2,
    "Jupiler Pro League": 3,
    "Süper Lig": 3,
    "Swiss Super League": 3,
    "Austrian Football Bundesliga": 3,
    "Ekstraklasa": 4,
    "Allsvenskan": 4,
    "Eliteserien": 4,
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
    "User-Agent": "football-bot-sportapi/1.0",
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
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
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

        if not response.ok:
            raise RuntimeError(
                f"Erreur Telegram : HTTP {response.status_code}"
            )


# ---------------- DIAGNOSTICS ----------------

def add_error(message):
    message = str(message)

    if API_KEY:
        message = message.replace(API_KEY, "[MASQUÉ]")
    if TOKEN:
        message = message.replace(TOKEN, "[MASQUÉ]")

    message = message[:200]

    if message not in ERRORS:
        ERRORS.append(message)

    print(f"DIAGNOSTIC : {message}", flush=True)


def diagnostics_text():
    return (
        "\n\n🛠️ Diagnostic : " + " ; ".join(ERRORS[:2])
    ) if ERRORS else ""


# ---------------- API ----------------

def api_get(endpoint):
    global API_CALLS, API_BLOCKED, LAST_CALL

    if endpoint in CACHE:
        return CACHE[endpoint]

    if API_BLOCKED:
        return None

    if not API_KEY:
        add_error("API_KEY absente.")
        return None

    for attempt in range(MAX_RETRIES + 1):
        if API_CALLS >= MAX_API_CALLS:
            add_error("Limite d'appels API atteinte.")
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
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            add_error(f"{endpoint} : erreur réseau ({type(exc).__name__}).")

            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
                continue

            return None

        if response.status_code == 429:
            add_error(f"{endpoint} : quota SportAPI atteint.")
            API_BLOCKED = True
            return None

        if response.status_code in (401, 403):
            add_error("Clé SportAPI invalide ou refusée.")
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

        if not isinstance(payload, dict) or not payload.get("success"):
            add_error(f"{endpoint} : réponse API invalide.")
            return None

        CACHE[endpoint] = payload
        return payload

    return None


# ---------------- OUTILS ----------------

def clamp_probability(value):
    return max(PROB_MIN, min(PROB_MAX, value))


def parse_kickoff(fixture):
    raw = fixture.get("kickoff_utc")

    if not raw:
        return None

    return datetime.fromisoformat(
        raw.replace("Z", "+00:00")
    ).astimezone(PARIS)


# ---------------- MATCHS DU JOUR ----------------

def get_today_fixtures():
    now = datetime.now(PARIS)
    today = now.date().isoformat()

    payload = api_get(f"fixtures/date/{today}")

    if not payload:
        return [], 0, []

    fixtures = payload.get("data") or []

    league_names = sorted({
        str(fixture.get("league_name") or "INCONNU")
        for fixture in fixtures
    })

    print("LIGUES REÇUES :", league_names, flush=True)

    selected = []

    for fixture in fixtures:
        try:
            league_name = fixture.get("league_name") or ""
            kickoff = parse_kickoff(fixture)
            status = fixture.get("status")

            if league_name not in LEAGUES:
                continue

            if not kickoff:
                continue

            if kickoff.date() != now.date():
                continue

            if status != "Scheduled":
                continue

            if kickoff <= now:
                continue

            if not fixture.get("home_id") or not fixture.get("away_id"):
                continue

            selected.append(fixture)

        except (TypeError, ValueError):
            continue

    selected.sort(key=lambda x: parse_kickoff(x))
    selected = selected[:CANDIDATE_MATCHES]

    print(
        f"Matchs SportAPI reçus : {len(fixtures)} ; "
        f"matchs retenus : {len(selected)}",
        flush=True,
    )

    return selected, len(fixtures), league_names


# ---------------- CLASSEMENT ET FORME ----------------

def get_standings(league_id):
    payload = api_get(f"standings/{league_id}")

    if not payload:
        return []

    return payload.get("data") or []


def find_team_row(rows, team_id):
    for row in rows:
        team = row.get("team") or {}

        if team.get("id") == team_id:
            return row

    return None


def build_profile(row):
    if not row:
        return None

    played = row.get("played") or 0
    goals_for = row.get("goals_for") or 0
    goals_against = row.get("goals_against") or 0

    if played < 4:
        return None

    season_gf = goals_for / played
    season_ga = goals_against / played

    form = row.get("form") or []
    form = form[-6:]

    form_gf = []
    form_ga = []
    btts = []
    over15 = []

    for match in form:
        home_score = match.get("home_score")
        away_score = match.get("away_score")

        if home_score is None or away_score is None:
            continue

        home_score = float(home_score)
        away_score = float(away_score)

        if match.get("is_home"):
            gf, ga = home_score, away_score
        else:
            gf, ga = away_score, home_score

        form_gf.append(gf)
        form_ga.append(ga)

        btts.append(1.0 if gf > 0 and ga > 0 else 0.0)
        over15.append(1.0 if gf + ga >= 2 else 0.0)

    if len(form_gf) < 3:
        profile_gf = season_gf
        profile_ga = season_ga
        profile_btts = 0.5
        profile_over15 = 0.5
        form_played = 0
    else:
        form_played = len(form_gf)

        form_gf_avg = sum(form_gf) / form_played
        form_ga_avg = sum(form_ga) / form_played

        profile_gf = (
            form_gf_avg * form_played + season_gf * 6
        ) / (form_played + 6)

        profile_ga = (
            form_ga_avg * form_played + season_ga * 6
        ) / (form_played + 6)

        profile_btts = sum(btts) / form_played
        profile_over15 = sum(over15) / form_played

    return {
        "gf": profile_gf,
        "ga": profile_ga,
        "btts": profile_btts,
        "over15": profile_over15,
        "form_played": form_played,
        "season_played": played,
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
    league_id = fixture.get("league_id")
    home_id = fixture.get("home_id")
    away_id = fixture.get("away_id")

    if not league_id or not home_id or not away_id:
        return None

    rows = get_standings(league_id)

    home_row = find_team_row(rows, home_id)
    away_row = find_team_row(rows, away_id)

    home = build_profile(home_row)
    away = build_profile(away_row)

    if not home or not away:
        return None

    lambda_home = max(
        0.2,
        min(3.5, (home["gf"] + away["ga"]) / 2),
    )

    lambda_away = max(
        0.2,
        min(3.5, (away["gf"] + home["ga"]) / 2),
    )

    poisson = poisson_markets(lambda_home, lambda_away)

    p_btts_form = (home["btts"] + away["btts"]) / 2

    p_btts = (
        W_POISSON * poisson["btts_poisson"]
        + W_FORM * p_btts_form
    )
    p_btts = clamp_probability(p_btts)

    p_over_form = (home["over15"] + away["over15"]) / 2

    p_over_15 = (
        W_OVER_POISSON * poisson["over_15"]
        + W_OVER_FORM * p_over_form
    )
    p_over_15 = clamp_probability(p_over_15)

    markets = {
        "home_over_05": poisson["home_over_05"],
        "away_over_05": poisson["away_over_05"],
        "btts": p_btts,
        "over_15": p_over_15,
    }

    details = {
        "btts": {
            "home": f"{round(home['btts'] * 100)} %",
            "away": f"{round(away['btts'] * 100)} %",
        },
        "home_over_05": {
            "home": f"{home['gf']:.2f} but/match",
            "away": f"{away['ga']:.2f} but/match",
        },
        "away_over_05": {
            "home": f"{away['gf']:.2f} but/match",
            "away": f"{home['ga']:.2f} but/match",
        },
        "over_15": {
            "home": f"{round(home['over15'] * 100)} %",
            "away": f"{round(away['over15'] * 100)} %",
        },
    }

    warnings = []

    if home["form_played"] < 4 or away["form_played"] < 4:
        warnings.append("forme limitée")

    return {
        "id": fixture.get("id"),
        "match": (
            f"{fixture.get('home_team')} - "
            f"{fixture.get('away_team')}"
        ),
        "home": fixture.get("home_team"),
        "away": fixture.get("away_team"),
        "league": fixture.get("league_name"),
        "kickoff": parse_kickoff(fixture),
        "markets": markets,
        "details": details,
        "warnings": warnings,
        "sample": (
            f"{home['form_played']} matchs de forme / "
            f"{home['season_played']} en saison"
        ),
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
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")

    if not API_KEY:
        send_message("Bot football : API_KEY absente.")
        raise RuntimeError("API_KEY absente.")

    today = datetime.now(PARIS).strftime("%d/%m/%Y")

    fixtures, total, league_names = get_today_fixtures()

    if not fixtures:
        send_message(
            f"⚽ Analyse football — {today}\n\n"
            "😴 Aucun match à venir trouvé dans les championnats suivis.\n\n"
            f"Matchs reçus de SportAPI : {total}\n\n"
            "🏆 Ligues reçues :\n"
            + "\n".join(league_names[:40])
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
