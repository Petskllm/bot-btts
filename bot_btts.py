import os
import time
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


# ================= CONFIGURATION =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://v3.football.api-sports.io"
PARIS = ZoneInfo("Europe/Paris")

MIN_SCORE = 65
MAX_ENVOIS = 10
MAX_API_CALLS = 85

LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "Champions League",
    3: "Europa League",
    94: "Primeira Liga",
    88: "Eredivisie",
    203: "Süper Lig",
    71: "Serie A Brésil",
    128: "Ligue 1 Argentine",
}

SESSION = requests.Session()
CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False


# ================= TELEGRAM =================

def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Secret TELEGRAM_TOKEN ou CHAT_ID absent."
        )

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3500:
            if current:
                chunks.append(current)
            current = ""

        while len(line) > 3500:
            chunks.append(line[:3500])
            line = line[3500:]

        current += line

    if current:
        chunks.append(current)

    for chunk in chunks:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                },
                timeout=25,
            )
        except requests.RequestException:
            raise RuntimeError(
                "Erreur réseau pendant l'envoi Telegram."
            ) from None

        if not response.ok:
            raise RuntimeError(
                f"Erreur Telegram : HTTP {response.status_code}"
            )

        if not response.json().get("ok"):
            raise RuntimeError("Message refusé par Telegram.")


# ================= API =================

def add_error(message):
    message = str(message)

    for secret in (API_KEY, TOKEN):
        if secret:
            message = message.replace(secret, "[MASQUÉ]")

    message = message[:450]

    if message not in ERRORS:
        ERRORS.append(message)

    print(f"DIAGNOSTIC : {message}", flush=True)


def api_get(endpoint, params=None):
    global API_CALLS, API_BLOCKED

    params = params or {}
    key = (endpoint, tuple(sorted(params.items())))

    if key in CACHE:
        return CACHE[key]

    if API_BLOCKED:
        return None

    if API_CALLS >= MAX_API_CALLS:
        add_error("Budget de requêtes de cette exécution atteint.")
        return None

    API_CALLS += 1

    print(
        f"API {API_CALLS}/{MAX_API_CALLS} : "
        f"{endpoint} | {params}",
        flush=True,
    )

    time.sleep(1.1)

    try:
        response = SESSION.get(
            f"{BASE_URL}/{endpoint}",
            headers={"x-apisports-key": API_KEY},
            params=params,
            timeout=25,
        )
    except requests.RequestException:
        add_error(f"{endpoint} : erreur réseau.")
        return None

    if not response.ok:
        add_error(
            f"{endpoint} : HTTP {response.status_code} — "
            f"{response.text[:250]}"
        )

        if response.status_code in {401, 403, 429}:
            API_BLOCKED = True

        return None

    try:
        payload = response.json()
    except ValueError:
        add_error(f"{endpoint} : réponse JSON invalide.")
        return None

    if not isinstance(payload, dict):
        add_error(f"{endpoint} : format de réponse invalide.")
        return None

    if payload.get("errors"):
        errors = payload["errors"]
        add_error(f"{endpoint} : {errors}")

        error_text = str(errors).lower()

        if any(term in error_text for term in (
            "request limit",
            "requests limit",
            "rate limit",
            "api key",
            "token",
        )):
            API_BLOCKED = True

        return None

    data = payload.get("response")

    if not isinstance(data, (list, dict)):
        add_error(f"{endpoint} : champ response invalide.")
        return None

    CACHE[key] = data

    print(
        f"Résultats reçus : {len(data)}",
        flush=True,
    )

    return data


def diagnostics_text():
    if not ERRORS:
        return ""

    return "\n\nDiagnostic API :\n" + "\n".join(
        f"• {error}" for error in ERRORS[:3]
    )


# ================= MATCHS =================

def kickoff_of(fixture):
    raw = fixture["fixture"]["date"]

    return datetime.fromisoformat(
        raw.replace("Z", "+00:00")
    ).astimezone(PARIS)


def get_today_fixtures():
    today = datetime.now(PARIS).strftime("%Y-%m-%d")

    data = api_get("fixtures", {
        "date": today,
        "timezone": "Europe/Paris",
    })

    if not isinstance(data, list):
        return None, 0, 0

    now = datetime.now(PARIS)
    selected = []
    league_matches = 0
    seen = set()

    for fixture in data:
        try:
            if fixture["league"]["id"] not in LEAGUES:
                continue

            league_matches += 1

            if fixture["fixture"]["status"]["short"] != "NS":
                continue

            if kickoff_of(fixture) <= now:
                continue

            fixture_id = fixture["fixture"]["id"]

            if fixture_id in seen:
                continue

            seen.add(fixture_id)
            selected.append(fixture)

        except (KeyError, TypeError, ValueError):
            add_error("Un match incomplet a été ignoré.")

    selected.sort(key=kickoff_of)

    return selected, len(data), league_matches


def get_team_stats(team_id, league_id, season):
    data = api_get("teams/statistics", {
        "team": team_id,
        "league": league_id,
        "season": season,
    })

    return data if isinstance(data, dict) and data else None


def extract_rates(stats, side):
    played = stats["fixtures"]["played"]
    goals_for = stats["goals"]["for"]["total"]
    goals_against = stats["goals"]["against"]["total"]

    total_played = int(played.get("total") or 0)
    side_played = int(played.get(side) or 0)

    if total_played < 5:
        return None

    scope = side if side_played >= 4 else "total"
    games = int(played.get(scope) or 0)

    gf = goals_for.get(scope)
    ga = goals_against.get(scope)

    if games <= 0 or gf is None or ga is None:
        return None

    return {
        "gf": float(gf) / games,
        "ga": float(ga) / games,
        "scope": scope,
    }


def get_recent_form(team_id):
    data = api_get("fixtures", {
        "team": team_id,
        "last": 5,
        "status": "FT",
        "timezone": "Europe/Paris",
    })

    if not isinstance(data, list):
        return None

    matches = []

    for match in data:
        try:
            if match["fixture"]["status"]["short"] != "FT":
                continue

            gh = match["goals"]["home"]
            ga = match["goals"]["away"]

            if gh is None or ga is None:
                continue

            home_id = match["teams"]["home"]["id"]
            away_id = match["teams"]["away"]["id"]

            if team_id == home_id:
                gf, conceded = gh, ga
            elif team_id == away_id:
                gf, conceded = ga, gh
            else:
                continue

            matches.append({
                "gf": gf,
                "ga": conceded,
                "date": kickoff_of(match),
            })

        except (KeyError, TypeError, ValueError):
            continue

    matches.sort(key=lambda item: item["date"], reverse=True)
    matches = matches[:5]

    if len(matches) < 3:
        return None

    n = len(matches)
    btts_count = sum(
        match["gf"] > 0 and match["ga"] > 0
        for match in matches
    )

    return {
        "n": n,
        "gf": sum(match["gf"] for match in matches) / n,
        "ga": sum(match["ga"] for match in matches) / n,
        "btts": btts_count / n,
        "btts_count": btts_count,
        "last_date": matches[0]["date"],
    }


def get_h2h_rate(home_id, away_id):
    data = api_get("fixtures/headtohead", {
        "h2h": f"{home_id}-{away_id}",
        "last": 6,
        "status": "FT",
    })

    if not isinstance(data, list):
        return None

    outcomes = []
    now = datetime.now(PARIS)

    for match in data:
        try:
            if match["fixture"]["status"]["short"] != "FT":
                continue

            age = (now - kickoff_of(match)).days

            if age < 0 or age > 3 * 366:
                continue

            gh = match["goals"]["home"]
            ga = match["goals"]["away"]

            if gh is not None and ga is not None:
                outcomes.append(gh > 0 and ga > 0)

        except (KeyError, TypeError, ValueError):
            continue

    if len(outcomes) < 3:
        return None

    return sum(outcomes) / len(outcomes)


# ================= INDICE STATISTIQUE =================

def calculate_score(fixture):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    league_id = fixture["league"]["id"]
    season = fixture["league"]["season"]

    home_stats = get_team_stats(
        home["id"], league_id, season
    )
    away_stats = get_team_stats(
        away["id"], league_id, season
    )

    if not home_stats or not away_stats:
        return None

    home_rates = extract_rates(home_stats, "home")
    away_rates = extract_rates(away_stats, "away")

    if not home_rates or not away_rates:
        return None

    home_form = get_recent_form(home["id"])
    away_form = get_recent_form(away["id"])

    if not home_form or not away_form:
        return None

    home_attack = (
        0.70 * home_rates["gf"]
        + 0.30 * home_form["gf"]
    )
    home_defence = (
        0.70 * home_rates["ga"]
        + 0.30 * home_form["ga"]
    )
    away_attack = (
        0.70 * away_rates["gf"]
        + 0.30 * away_form["gf"]
    )
    away_defence = (
        0.70 * away_rates["ga"]
        + 0.30 * away_form["ga"]
    )

    expected_home = (home_attack + away_defence) / 2
    expected_away = (away_attack + home_defence) / 2

    # Modèle simplifié : indice non calibré,
    # à ne pas confondre avec une probabilité réelle.
    base = (
        (1 - math.exp(-expected_home))
        * (1 - math.exp(-expected_away))
        * 100
    )

    recent_rate = (
        home_form["btts"] + away_form["btts"]
    ) / 2

    score = base + 10 * (recent_rate - 0.5)

    if score >= MIN_SCORE - 2:
        h2h_rate = get_h2h_rate(home["id"], away["id"])

        if h2h_rate is not None:
            score += 4 * (h2h_rate - 0.5)

    score = round(max(0, min(100, score)), 1)

    warnings = []

    if (
        home_rates["scope"] == "total"
        or away_rates["scope"] == "total"
    ):
        warnings.append("recul domicile/extérieur limité")

    kickoff = kickoff_of(fixture)

    for form in (home_form, away_form):
        rest_hours = (
            kickoff - form["last_date"]
        ).total_seconds() / 3600

        if 0 < rest_hours < 72:
            warnings.append("repos inférieur à 72 h")
            break

    return {
        "id": fixture["fixture"]["id"],
        "match": f"{home['name']} - {away['name']}",
        "league": fixture["league"]["name"],
        "kickoff": kickoff,
        "score": score,
        "reason": (
            "BTTS sur les derniers matchs : "
            f"{home['name']} "
            f"{home_form['btts_count']}/{home_form['n']}, "
            f"{away['name']} "
            f"{away_form['btts_count']}/{away_form['n']}"
        ),
        "warnings": warnings,
    }


# ================= ABSENCES ET COMPOSITIONS =================

def add_match_context(result):
    fixture_id = result["id"]

    injuries = api_get("injuries", {
        "fixture": fixture_id
    })

    if injuries is None:
        result["warnings"].append("absences non vérifiables")

    elif not injuries:
        result["warnings"].append(
            "aucune absence remontée, sans garantie"
        )

    elif isinstance(injuries, list):
        players = {}

        for injury in injuries:
            player = injury.get("player", {})
            name = player.get("name")

            if name:
                players[player.get("id") or name] = name

        if players:
            names = list(players.values())
            label = ", ".join(names[:3])

            if len(names) > 3:
                label += f" +{len(names) - 3}"

            result["warnings"].append(
                f"absences signalées : {label}"
            )
        else:
            result["warnings"].append(
                "absences signalées à vérifier"
            )

    lineups = api_get("fixtures/lineups", {
        "fixture": fixture_id
    })

    if isinstance(lineups, list) and len(lineups) >= 2:
        complete = all(
            len(team.get("startXI", [])) == 11
            for team in lineups[:2]
        )

        if complete:
            result["warnings"].append(
                "compositions disponibles, impact non calculé"
            )
        else:
            result["warnings"].append(
                "compositions incomplètes"
            )
    else:
        result["warnings"].append(
            "compositions indisponibles"
        )

    # Aucun bonus ou malus inventé :
    # le nombre d'absents ne mesure pas leur importance.


# ================= ANALYSE =================

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Ajoute les secrets TELEGRAM_TOKEN et CHAT_ID."
        )

    if not API_KEY:
        send_message(
            "Le secret GitHub API_KEY est absent. "
            "Ajoute-le dans les paramètres du dépôt."
        )
        raise RuntimeError("API_KEY absent.")

    today = datetime.now(PARIS).strftime("%d/%m/%Y")

    fixtures, total, league_total = get_today_fixtures()

    if fixtures is None:
        send_message(
            f"BTTS — {today}\n\n"
            "Impossible de récupérer les matchs."
            + diagnostics_text()
        )
        raise RuntimeError(
            "Récupération des matchs impossible."
        )

    if not fixtures:
        send_message(
            f"⚽ BTTS — {today}\n\n"
            f"Matchs retournés par l’API : {total}\n"
            f"Dans les compétitions suivies : {league_total}\n"
            "Aucun match à venir à analyser dans cette liste."
            + diagnostics_text()
        )
        return

    evaluated = []
    insufficient = 0
    attempted = 0

    reserve = 2 * min(MAX_ENVOIS, len(fixtures))

    for fixture in fixtures:
        if API_BLOCKED:
            break

        if API_CALLS + 5 > MAX_API_CALLS - reserve:
            add_error(
                "Analyse partielle : budget réservé "
                "aux absences et compositions."
            )
            break

        attempted += 1

        try:
            result = calculate_score(fixture)
        except (KeyError, TypeError, ValueError) as exc:
            print(
                f"Match ignoré : {type(exc).__name__}",
                flush=True,
            )
            result = None

        if result is None:
            insufficient += 1
            continue

        evaluated.append(result)

        print(
            f"{result['match']} : {result['score']}/100",
            flush=True,
        )

    evaluated.sort(
        key=lambda result: result["score"],
        reverse=True,
    )

    results = [
        result for result in evaluated
        if result["score"] >= MIN_SCORE
    ][:MAX_ENVOIS]

    summary = (
        f"À venir : {len(fixtures)} | "
        f"analysés : {len(evaluated)} | "
        f"données insuffisantes : {insufficient}"
    )

    if attempted < len(fixtures):
        summary += (
            f"\nNon traités : {len(fixtures) - attempted}"
        )

    if not results:
        message = (
            f"⚽ BTTS OUI — {today}\n"
            f"Aucun indice ≥ {MIN_SCORE}/100.\n\n"
            f"{summary}"
        )

        if evaluated:
            message += (
                "\n\nMeilleurs indices, NON sélectionnés :\n"
            )
            message += "\n".join(
                f"• {result['match']} : "
                f"{result['score']}/100"
                for result in evaluated[:5]
            )

        message += diagnostics_text()
        send_message(message)
        return

    for result in results:
        add_match_context(result)

    message = (
        f"⚽ BTTS OUI — {today}\n"
        f"Filtre : indice ≥ {MIN_SCORE}/100\n\n"
    )

    for index, result in enumerate(results, 1):
        message += (
            f"{index}. {result['match']}\n"
            f"🏆 {result['league']} | "
            f"{result['kickoff']:%H:%M} Paris | "
            f"{result['score']}/100\n"
            f"✅ {result['reason']}\n"
        )

        warnings = list(dict.fromkeys(result["warnings"]))

        if warnings:
            message += (
                "⚠️ " + " ; ".join(warnings) + "\n"
            )

        message += "\n"

    message += (
        summary
        + "\n\nIndice statistique, pas une probabilité garantie."
        + diagnostics_text()
    )

    send_message(message)

    print(
        f"Terminé : {API_CALLS} appels API, "
        f"{len(results)} sélections envoyées.",
        flush=True,
    )


if __name__ == "__main__":
    main()
