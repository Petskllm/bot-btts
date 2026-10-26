import os
import time
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


# ================= CONFIGURATION =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://api.football-data.org/v4"
PARIS = ZoneInfo("Europe/Paris")

MIN_SCORE = 60
MAX_ENVOIS = 10
MAX_API_CALLS = 60

# Plan gratuit : 10 requêtes / minute
MIN_INTERVAL = 6.5

LEAGUES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "PPL": "Primeira Liga",
    "DED": "Eredivisie",
    "BSA": "Serie A Brésil",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "btts-telegram-bot/2.0"})

CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False
LAST_CALL = 0.0


# ================= TELEGRAM =================

def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret TELEGRAM_TOKEN ou CHAT_ID absent.")

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
            timeout=25,
        )

        if not response.ok or not response.json().get("ok"):
            raise RuntimeError(f"Erreur Telegram : HTTP {response.status_code}")


# ================= API =================

def add_error(message):
    message = str(message)

    for secret in (API_KEY, TOKEN):
        if secret:
            message = message.replace(secret, "[MASQUÉ]")

    message = message[:400]

    if message not in ERRORS:
        ERRORS.append(message)

    print(f"DIAGNOSTIC : {message}", flush=True)


def api_get(endpoint, params=None):
    global API_CALLS, API_BLOCKED, LAST_CALL

    params = params or {}
    key = (endpoint, tuple(sorted(params.items())))

    if key in CACHE:
        return CACHE[key]

    if API_BLOCKED:
        return None

    if API_CALLS >= MAX_API_CALLS:
        add_error("Budget API atteint.")
        return None

    response = None

    for attempt in (1, 2):
        wait = MIN_INTERVAL - (time.time() - LAST_CALL)

        if wait > 0:
            time.sleep(wait)

        API_CALLS += 1
        LAST_CALL = time.time()

        print(f"API {API_CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

        try:
            response = SESSION.get(
                f"{BASE_URL}/{endpoint}",
                headers={"X-Auth-Token": API_KEY},
                params=params,
                timeout=25,
            )
        except requests.RequestException:
            add_error(f"{endpoint} : erreur réseau.")
            return None

        if response.status_code == 429 and attempt == 1:
            print("Limite API : pause 60 s.", flush=True)
            time.sleep(60)
            continue

        break

    if not response.ok:
        add_error(
            f"{endpoint} : HTTP {response.status_code} — "
            f"{response.text[:200]}"
        )

        if response.status_code in {401, 403, 429}:
            API_BLOCKED = True

        return None

    try:
        payload = response.json()
    except ValueError:
        add_error(f"{endpoint} : JSON invalide.")
        return None

    CACHE[key] = payload
    return payload


def diagnostics_text():
    if not ERRORS:
        return ""

    return "\n\nDiagnostic API :\n" + "\n".join(
        f"• {error}" for error in ERRORS[:3]
    )


# ================= MATCHS =================

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
        "dateTo": today.isoformat(),
    })

    matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(matches, list):
        return None, 0, 0

    selected = []
    league_matches = 0
    seen = set()

    for fixture in matches:
        try:
            if fixture["competition"]["code"] not in LEAGUES:
                continue

            kickoff = kickoff_of(fixture)

            if kickoff.date() != today:
                continue

            league_matches += 1

            if fixture["status"] not in {"TIMED", "SCHEDULED"}:
                continue

            if kickoff <= now:
                continue

            if fixture["id"] in seen:
                continue

            seen.add(fixture["id"])
            selected.append(fixture)

        except (KeyError, TypeError, ValueError):
            continue

    selected.sort(key=kickoff_of)
    return selected, len(matches), league_matches


def get_standings(code):
    data = api_get(f"competitions/{code}/standings")

    if not isinstance(data, dict):
        return None

    result = {}

    for block in data.get("standings") or []:
        kind = block.get("type")

        for row in block.get("table") or []:
            try:
                team_id = row["team"]["id"]

                result.setdefault(kind, {})[team_id] = {
                    "played": int(row.get("playedGames") or 0),
                    "gf": int(row.get("goalsFor") or 0),
                    "ga": int(row.get("goalsAgainst") or 0),
                }
            except (KeyError, TypeError, ValueError):
                continue

    return result or None


def extract_rates(standings, team_id, side):
    """Essaie HOME/AWAY, puis retombe sur le TOTAL."""
    total = standings.get("TOTAL", {}).get(team_id)

    if not total or total["played"] < 3:
        return None

    part = standings.get(side.upper(), {}).get(team_id)

    if part and part["played"] >= 3:
        row = part
        scope = side
    else:
        row = total
        scope = "total"

    return {
        "gf": row["gf"] / row["played"],
        "ga": row["ga"] / row["played"],
        "scope": scope,
    }


def get_recent_form(team_id):
    now = datetime.now(PARIS)
    date_to = now.date()
    date_from = date_to - timedelta(days=180)

    data = api_get(f"teams/{team_id}/matches", {
        "status": "FINISHED",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "limit": 30,
    })

    raw_matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(raw_matches, list):
        return None

    matches = []

    for match in raw_matches:
        try:
            full_time = match["score"]["fullTime"]
            gh = full_time.get("home")
            ga = full_time.get("away")

            if gh is None or ga is None:
                continue

            date = kickoff_of(match)

            if date > now:
                continue

            home_id = match["homeTeam"]["id"]
            away_id = match["awayTeam"]["id"]

            if team_id == home_id:
                gf, conceded = gh, ga
            elif team_id == away_id:
                gf, conceded = ga, gh
            else:
                continue

            matches.append({
                "gf": gf,
                "ga": conceded,
                "date": date,
            })

        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    matches.sort(key=lambda item: item["date"], reverse=True)
    matches = matches[:6]

    if len(matches) < 3:
        return None

    n = len(matches)

    return {
        "n": n,
        "gf": sum(m["gf"] for m in matches) / n,
        "ga": sum(m["ga"] for m in matches) / n,
        "btts": sum(
            m["gf"] > 0 and m["ga"] > 0 for m in matches
        ) / n,
        "btts_count": sum(
            m["gf"] > 0 and m["ga"] > 0 for m in matches
        ),
        "last_date": matches[0]["date"],
    }


def get_h2h_rate(match_id):
    data = api_get(f"matches/{match_id}/head2head", {"limit": 6})

    raw_matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(raw_matches, list):
        return None

    outcomes = []
    now = datetime.now(PARIS)

    for match in raw_matches:
        try:
            if match.get("status") != "FINISHED":
                continue

            full_time = match["score"]["fullTime"]
            gh = full_time.get("home")
            ga = full_time.get("away")

            if gh is not None and ga is not None:
                outcomes.append(gh > 0 and ga > 0)

        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    if len(outcomes) < 3:
        return None

    return sum(outcomes) / len(outcomes)


# ================= INDICE BTTS =================

def calculate_score(fixture):
    home = fixture["homeTeam"]
    away = fixture["awayTeam"]
    code = fixture["competition"]["code"]

    standings = get_standings(code)

    if not standings:
        return None

    home_rates = extract_rates(standings, home["id"], "home")
    away_rates = extract_rates(standings, away["id"], "away")

    if not home_rates or not away_rates:
        return None

    home_form = get_recent_form(home["id"])
    away_form = get_recent_form(away["id"])

    if not home_form or not away_form:
        return None

    # Moyenne entre classement et forme récente
    home_attack = 0.6 * home_rates["gf"] + 0.4 * home_form["gf"]
    home_defence = 0.6 * home_rates["ga"] + 0.4 * home_form["ga"]

    away_attack = 0.6 * away_rates["gf"] + 0.4 * away_form["gf"]
    away_defence = 0.6 * away_rates["ga"] + 0.4 * away_form["ga"]

    # Buts attendus simplifiés
    expected_home = (home_attack + away_defence) / 2
    expected_away = (away_attack + home_defence) / 2

    # Probabilité Poisson simplifiée que chaque équipe marque
    p_home_scores = 1 - math.exp(-expected_home)
    p_away_scores = 1 - math.exp(-expected_away)

    base = p_home_scores * p_away_scores * 100

    # Forme BTTS récente
    recent_rate = (home_form["btts"] + away_form["btts"]) / 2
    score = base + 12 * (recent_rate - 0.5)

    # H2H seulement si le match est proche du seuil
    if score >= MIN_SCORE - 8:
        h2h_rate = get_h2h_rate(fixture["id"])

        if h2h_rate is not None:
            score += 5 * (h2h_rate - 0.5)

    score = round(max(0, min(100, score)), 1)

    warnings = []

    if home_rates["scope"] == "total" or away_rates["scope"] == "total":
        warnings.append("stats domicile/extérieur limitées")

    kickoff = kickoff_of(fixture)

    for form in (home_form, away_form):
        rest_hours = (kickoff - form["last_date"]).total_seconds() / 3600

        if 0 < rest_hours < 72:
            warnings.append("repos inférieur à 72 h")
            break

    home_name = home.get("shortName") or home.get("name")
    away_name = away.get("shortName") or away.get("name")

    return {
        "id": fixture["id"],
        "match": f"{home_name} - {away_name}",
        "league": fixture["competition"]["name"],
        "kickoff": kickoff,
        "score": score,
        "reason": (
            f"BTTS récents : {home_name} "
            f"{home_form['btts_count']}/{home_form['n']} | "
            f"{away_name} "
            f"{away_form['btts_count']}/{away_form['n']}"
        ),
        "warnings": warnings,
    }


# ================= ANALYSE =================

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Ajoute TELEGRAM_TOKEN et CHAT_ID.")

    if not API_KEY:
        send_message("Secret GitHub API_KEY absent.")
        raise RuntimeError("API_KEY absent.")

    today = datetime.now(PARIS).strftime("%d/%m/%Y")

    fixtures, total, league_total = get_today_fixtures()

    if fixtures is None:
        send_message(
            f"BTTS — {today}\n\n"
            "Impossible de récupérer les matchs."
            + diagnostics_text()
        )
        return

    if not fixtures:
        send_message(
            f"⚽ BTTS — {today}\n\n"
            f"Matchs API : {total}\n"
            f"Dans les compétitions suivies : {league_total}\n"
            "Aucun match à venir aujourd’hui."
            + diagnostics_text()
        )
        return

    evaluated = []
    insufficient = 0

    for fixture in fixtures:
        if API_BLOCKED:
            break

        if API_CALLS + 3 > MAX_API_CALLS:
            add_error("Analyse partielle : budget API atteint.")
            break

        try:
            result = calculate_score(fixture)
        except (KeyError, TypeError, ValueError):
            result = None

        if result is None:
            insufficient += 1
            continue

        evaluated.append(result)
        print(f"{result['match']} : {result['score']}/100", flush=True)

    evaluated.sort(key=lambda r: r["score"], reverse=True)

    results = [
        r for r in evaluated if r["score"] >= MIN_SCORE
    ][:MAX_ENVOIS]

    selected_ids = {r["id"] for r in results}

    others = [
        r for r in evaluated
        if r["id"] not in selected_ids
    ][:6]

    summary = (
        f"À venir : {len(fixtures)} | "
        f"analysés : {len(evaluated)} | "
        f"données insuffisantes : {insufficient}"
    )

    message = (
        f"⚽ BTTS OUI — {today}\n"
        f"Filtre : indice ≥ {MIN_SCORE}/100\n\n"
    )

    if results:
        for index, result in enumerate(results, 1):
            message += (
                f"{index}. {result['match']}\n"
                f"🏆 {result['league']} | "
                f"{result['kickoff']:%H:%M} Paris | "
                f"{result['score']}/100\n"
                f"✅ {result['reason']}\n"
            )

            if result["warnings"]:
                message += (
                    "⚠️ "
                    + " ; ".join(result["warnings"])
                    + "\n"
                )

            message += "\n"
    else:
        message += f"Aucun indice ≥ {MIN_SCORE}/100.\n\n"

    if others:
        message += "Meilleurs matchs sous le seuil :\n"
        message += "\n".join(
            f"• {r['match']} : {r['score']}/100"
            for r in others
        )
        message += "\n\n"

    message += (
        summary
        + "\n\nIndice statistique, pas une probabilité garantie."
        + "\nVérifie absences et compositions avant de jouer."
        + diagnostics_text()
    )

    send_message(message)

    print(
        f"Terminé : {API_CALLS} appels API, "
        f"{len(results)} sélections.",
        flush=True,
    )


if __name__ == "__main__":
    main()
