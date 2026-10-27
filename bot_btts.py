import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — BTTS / ÉQUIPE +0,5 / OVER 1,5
# API : football-data.org v4
# Publication : Telegram
#
# IMPORTANT :
# - Les probabilités sont des estimations de modèle, pas des garanties.
# - Le seuil de 70 % n'est pas une preuve de calibration.
# - Tester le modèle sur des matchs historiques avant toute utilisation
#   pour des décisions financières.
# ============================================================

# ---------------- CONFIGURATION ----------------

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://api.football-data.org/v4"
PARIS = ZoneInfo("Europe/Paris")

MIN_PERCENT = 70
MAX_MATCHES = 10
MAX_MARKETS_PER_MATCH = 2

MAX_API_CALLS = 60
MIN_INTERVAL = 6.5
MAX_RETRIES = 2
REQUEST_TIMEOUT = 25
MAX_CONSECUTIVE_FAILS = 4

LEAGUES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Ligue des champions",
    "PPL": "Primeira Liga",
    "DED": "Eredivisie",
    "BSA": "Brasileirão",
}

# Modèle de forme / historique
HISTORY_DAYS = 240
OVERALL_N = 10
VENUE_N = 6
MIN_OVERALL = 6
MIN_VENUE = 3
GOOD_SAMPLE = 8
DECAY = 0.90
VENUE_SHRINK = 3.0
PRIOR_SHRINK = 3.0
DEFAULT_GOALS = 1.35
MAX_STALE_DAYS = 60
SHORT_REST_HOURS = 72

# Mélange du modèle BTTS
W_POISSON = 0.40
W_RATES = 0.35
W_HISTORY = 0.25

# Mélange du modèle Over 1,5
W_OVER_POISSON = 0.60
W_OVER_HISTORY = 0.40

# Probabilités plafonnées pour éviter une confiance artificielle
PROB_MIN = 0.01
PROB_MAX = 0.99

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "football-stat-bot/7.0"})

CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False
LAST_CALL = None
CONSECUTIVE_FAILS = 0

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


def note_failure():
    global CONSECUTIVE_FAILS, API_BLOCKED
    CONSECUTIVE_FAILS += 1

    if CONSECUTIVE_FAILS >= MAX_CONSECUTIVE_FAILS:
        API_BLOCKED = True
        add_error("Trop d'échecs consécutifs : analyse arrêtée.")


def diagnostics_text():
    return (
        "\n\n🛠️ Diagnostic : " + " ; ".join(ERRORS[:2])
    ) if ERRORS else ""


# ---------------- API ----------------

def api_get(endpoint, params=None):
    global API_CALLS, API_BLOCKED, LAST_CALL, CONSECUTIVE_FAILS

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
                headers={"X-Auth-Token": API_KEY},
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            add_error(f"{endpoint} : erreur réseau ({type(exc).__name__}).")

            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
                continue

            note_failure()
            return None

        if response.status_code == 429:
            if attempt < MAX_RETRIES:
                retry_after = response.headers.get("Retry-After", "")

                try:
                    pause = max(60, int(retry_after))
                except (TypeError, ValueError):
                    pause = 60

                add_error(f"{endpoint} : quota atteint, pause {pause} s.")
                time.sleep(pause)
                continue

            add_error(f"{endpoint} : quota API dépassé.")
            API_BLOCKED = True
            return None

        if response.status_code == 401:
            add_error("Clé API refusée (401).")
            API_BLOCKED = True
            return None

        if not response.ok:
            add_error(f"{endpoint} : HTTP {response.status_code}.")
            note_failure()
            return None

        try:
            payload = response.json()
        except ValueError:
            add_error(f"{endpoint} : JSON invalide.")
            note_failure()
            return None

        if not isinstance(payload, dict):
            add_error(f"{endpoint} : format de réponse invalide.")
            note_failure()
            return None

        CONSECUTIVE_FAILS = 0
        CACHE[cache_key] = payload
        return payload

    return None


# ---------------- FIXTURES ----------------

def kickoff_of(match):
    raw = match["utcDate"]
    return datetime.fromisoformat(
        raw.replace("Z", "+00:00")
    ).astimezone(PARIS)


def get_today_fixtures():
    now = datetime.now(PARIS)
    today = now.date()

    data = api_get("matches", {
        "dateFrom": today.isoformat(),
        "dateTo": (today + timedelta(days=1)).isoformat(),
    })

    matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(matches, list):
        return None, 0

    selected = []
    seen = set()
    observed_codes = set()

    for fixture in matches:
        try:
            code = fixture["competition"]["code"]
            observed_codes.add(code)

            if code not in LEAGUES:
                continue

            kickoff = kickoff_of(fixture)

            if kickoff.date() != today:
                continue

            if fixture.get("status") not in {"TIMED", "SCHEDULED"}:
                continue

            if kickoff <= now:
                continue

            fixture_id = fixture["id"]

            if fixture_id in seen:
                continue

            seen.add(fixture_id)
            selected.append(fixture)

        except (KeyError, TypeError, ValueError):
            continue

    selected.sort(key=kickoff_of)

    if not selected:
        if not matches:
            add_error(
                "L'API n'a retourné aucun match pour la période interrogée "
                f"({today.isoformat()} à {(today + timedelta(days=1)).isoformat()})."
            )
        elif not observed_codes.intersection(LEAGUES):
            add_error(
                "Des matchs existent dans la réponse, mais aucun ne correspond "
                "aux codes de compétitions configurés. Codes reçus : "
                + ", ".join(sorted(observed_codes)[:12])
            )
        else:
            add_error(
                "Matchs reçus, mais aucun match à venir aujourd'hui à Paris "
                "dans les compétitions configurées."
            )

    return selected, len(matches)


# ---------------- HISTORIQUE ET STATS ----------------

def get_league_prior(code):
    key = ("prior", code)

    if key in CACHE:
        return CACHE[key]

    prior = DEFAULT_GOALS
    data = api_get(f"competitions/{code}/standings")

    if isinstance(data, dict):
        goals = 0
        played = 0

        for block in data.get("standings") or []:
            if block.get("type") != "TOTAL":
                continue

            for row in block.get("table") or []:
                try:
                    goals += int(row.get("goalsFor") or 0)
                    played += int(row.get("playedGames") or 0)
                except (TypeError, ValueError):
                    continue

        if played >= 20:
            prior = goals / played

    prior = max(0.9, min(2.2, prior))
    CACHE[key] = prior
    return prior


def regular_goals(match):
    score = match.get("score") or {}
    regular = score.get("regularTime") or {}

    if regular.get("home") is not None and regular.get("away") is not None:
        return int(regular["home"]), int(regular["away"])

    if score.get("duration") in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
        return None

    full = score.get("fullTime") or {}

    if full.get("home") is not None and full.get("away") is not None:
        return int(full["home"]), int(full["away"])

    return None


def get_team_history(team_id):
    now = datetime.now(PARIS)

    data = api_get(f"teams/{team_id}/matches", {
        "status": "FINISHED",
        "dateFrom": (now.date() - timedelta(days=HISTORY_DAYS)).isoformat(),
        "dateTo": now.date().isoformat(),
        "limit": 100,
    })

    raw_matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(raw_matches, list):
        return None

    unique = {}

    for match in raw_matches:
        try:
            if match.get("status") != "FINISHED":
                continue

            goals = regular_goals(match)

            if goals is None:
                continue

            date = kickoff_of(match)

            if date > now:
                continue

            home_id = match["homeTeam"]["id"]
            away_id = match["awayTeam"]["id"]

            if team_id == home_id:
                venue, gf, ga = "home", goals[0], goals[1]
            elif team_id == away_id:
                venue, gf, ga = "away", goals[1], goals[0]
            else:
                continue

            unique[match["id"]] = {
                "gf": gf,
                "ga": ga,
                "date": date,
                "venue": venue,
            }

        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    return sorted(unique.values(), key=lambda x: x["date"], reverse=True)


METRICS = ("gf", "ga", "scored", "conceded", "btts")


def shrink(value, n, prior, k):
    return (value * n + prior * k) / (n + k)


def weighted_stats(matches):
    weights = [DECAY ** index for index in range(len(matches))]
    total = sum(weights)

    def average(function):
        return sum(
            w * function(m)
            for w, m in zip(weights, matches)
        ) / total

    return {
        "n": len(matches),
        "gf": average(lambda m: m["gf"]),
        "ga": average(lambda m: m["ga"]),
        "scored": average(lambda m: 1.0 if m["gf"] > 0 else 0.0),
        "conceded": average(lambda m: 1.0 if m["ga"] > 0 else 0.0),
        "btts": average(
            lambda m: 1.0 if m["gf"] > 0 and m["ga"] > 0 else 0.0
        ),
    }


def build_profile(history, venue, prior):
    recent = history[:OVERALL_N]

    if len(recent) < MIN_OVERALL:
        return None

    overall = weighted_stats(recent)
    same_venue = [
        m for m in history if m["venue"] == venue
    ][:VENUE_N]

    if same_venue:
        local = weighted_stats(same_venue)
        blended = {
            key: shrink(
                local[key],
                local["n"],
                overall[key],
                VENUE_SHRINK,
            )
            for key in METRICS
        }
    else:
        blended = {key: overall[key] for key in METRICS}

    prior_scored = 1.0 - math.exp(-prior)

    references = {
        "gf": prior,
        "ga": prior,
        "scored": prior_scored,
        "conceded": prior_scored,
        "btts": prior_scored ** 2,
    }

    profile = {
        key: shrink(
            blended[key],
            overall["n"],
            references[key],
            PRIOR_SHRINK,
        )
        for key in METRICS
    }

    profile.update({
        "n": overall["n"],
        "n_venue": len(same_venue),
        "btts_count": sum(
            1 for m in recent
            if m["gf"] > 0 and m["ga"] > 0
        ),
        "scored_count": sum(
            1 for m in recent if m["gf"] > 0
        ),
        "conceded_count": sum(
            1 for m in recent if m["ga"] > 0
        ),
        "over15_count": sum(
            1 for m in recent
            if m["gf"] + m["ga"] >= 2
        ),
        "last_date": history[0]["date"],
    })

    return profile


def clamp_probability(p):
    return max(PROB_MIN, min(PROB_MAX, p))


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
    home_team = fixture.get("homeTeam") or {}
    away_team = fixture.get("awayTeam") or {}
    competition = fixture.get("competition") or {}
    code = competition.get("code")

    if not home_team.get("id") or not away_team.get("id") or not code:
        return None

    prior = get_league_prior(code)
    home_history = get_team_history(home_team["id"])
    away_history = get_team_history(away_team["id"])

    if not home_history or not away_history:
        return None

    home = build_profile(home_history, "home", prior)
    away = build_profile(away_history, "away", prior)

    if not home or not away:
        return None

    kickoff = kickoff_of(fixture)

    for profile in (home, away):
        if (kickoff - profile["last_date"]).days > MAX_STALE_DAYS:
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

    # BTTS : Poisson + fréquences observées + historique BTTS
    p_rates = (
        (home["scored"] + away["conceded"]) / 2
    ) * (
        (away["scored"] + home["conceded"]) / 2
    )

    p_history = (home["btts"] + away["btts"]) / 2

    p_btts = (
        W_POISSON * poisson["btts_poisson"]
        + W_RATES * p_rates
        + W_HISTORY * p_history
    )
    p_btts = clamp_probability(p_btts)

    # Over 1,5 : Poisson + fréquence réelle de matchs à 2+ buts
    p_over_history = (
        home["over15_count"] + away["over15_count"]
    ) / (home["n"] + away["n"])

    p_over_15 = (
        W_OVER_POISSON * poisson["over_15"]
        + W_OVER_HISTORY * p_over_history
    )
    p_over_15 = clamp_probability(p_over_15)

    home_name = (
        home_team.get("shortName")
        or home_team.get("name")
        or "Domicile"
    )
    away_name = (
        away_team.get("shortName")
        or away_team.get("name")
        or "Extérieur"
    )
    league = LEAGUES.get(code, competition.get("name") or code)

    warnings = []

    for name, profile in ((home_name, home), (away_name, away)):
        rest_hours = (
            kickoff - profile["last_date"]
        ).total_seconds() / 3600

        if 0 < rest_hours < SHORT_REST_HOURS:
            warnings.append(f"repos court : {name}")

    if min(home["n"], away["n"]) < GOOD_SAMPLE:
        warnings.append("échantillon limité")

    if min(home["n_venue"], away["n_venue"]) < MIN_VENUE:
        warnings.append("peu de matchs domicile/extérieur")

    markets = {
        "home_over_05": poisson["home_over_05"],
        "away_over_05": poisson["away_over_05"],
        "btts": p_btts,
        "over_15": p_over_15,
    }

    details = {
        "btts": {
            "home": f"{home['btts_count']}/{home['n']}",
            "away": f"{away['btts_count']}/{away['n']}",
        },
        "home_over_05": {
            "home": f"{home['scored_count']}/{home['n']}",
            "away": f"{away['conceded_count']}/{away['n']}",
        },
        "away_over_05": {
            "home": f"{away['scored_count']}/{away['n']}",
            "away": f"{home['conceded_count']}/{home['n']}",
        },
        "over_15": {
            "home": f"{home['over15_count']}/{home['n']}",
            "away": f"{away['over15_count']}/{away['n']}",
        },
    }

    return {
        "id": fixture["id"],
        "match": f"{home_name} - {away_name}",
        "home": home_name,
        "away": away_name,
        "league": league,
        "kickoff": kickoff,
        "markets": markets,
        "details": details,
        "warnings": warnings,
        "sample": f"{home['n']} matchs récents par équipe",
        "lambdas": (lambda_home, lambda_away),
    }


# ---------------- SÉLECTION ----------------

def build_candidates(fixtures):
    candidates = {key: [] for key in MARKET_LABELS}
    analysed = 0
    partial = False

    for fixture in fixtures:
        if API_BLOCKED or API_CALLS + 3 > MAX_API_CALLS:
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

    # Maximum 2 meilleurs marchés par match
    for match_id in matches:
        markets = matches[match_id]["markets"]
        markets.sort(key=lambda x: x[1], reverse=True)
        matches[match_id]["markets"] = markets[:MAX_MARKETS_PER_MATCH]

    # Maximum 10 matchs, classés par meilleure probabilité
    ordered_all = sorted(
        matches.values(),
        key=lambda entry: (
            entry["best_percent"],
            entry["item"]["kickoff"],
        ),
        reverse=True,
    )

    selected_matches = ordered_all[:MAX_MATCHES]

    # Remettre les matchs dans l’ordre chronologique
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
                    f"📊 Derniers matchs : {home_stat} et {away_stat}"
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
    fixtures, _ = get_today_fixtures()

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
            len(fixtures),
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
