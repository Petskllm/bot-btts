import os
import time
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


# ================= CONFIGURATION =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")  # clé football-data.org

BASE_URL = "https://api.football-data.org/v4"
PARIS = ZoneInfo("Europe/Paris")

MIN_SCORE = 65
MAX_ENVOIS = 10
MAX_API_CALLS = 60

# Plan gratuit football-data.org : 10 requêtes par minute.
MIN_INTERVAL = 6.5

# Compétitions du plan gratuit (code football-data.org)
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
CACHE = {}
ERRORS = []
API_CALLS = 0
API_BLOCKED = False
LAST_CALL = 0.0


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
    global API_CALLS, API_BLOCKED, LAST_CALL

    params = params or {}
    key = (endpoint, tuple(sorted(params.items())))

    if key in CACHE:
        return CACHE[key]

    if API_BLOCKED:
        return None

    if API_CALLS >= MAX_API_CALLS:
        add_error("Budget de requêtes de cette exécution atteint.")
        return None

    response = None

    for attempt in (1, 2):
        # Respecte la limite de 10 requêtes par minute.
        wait = MIN_INTERVAL - (time.time() - LAST_CALL)

        if wait > 0:
            time.sleep(wait)

        API_CALLS += 1

        print(
            f"API {API_CALLS}/{MAX_API_CALLS} : "
            f"{endpoint} | {params}",
            flush=True,
        )

        LAST_CALL = time.time()

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
            print(
                "Limite atteinte, pause de 60 s puis nouvel essai.",
                flush=True,
            )
            time.sleep(60)
            LAST_CALL = time.time()
            continue

        break

    if not response.ok:
        add_error(
            f"{endpoint} : HTTP {response.status_code} — "
            f"{response.text[:250]}"
        )

        if (
            response.status_code in {401, 429}
            or "token" in response.text.lower()
        ):
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

    CACHE[key] = payload

    print("Réponse reçue.", flush=True)

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
        "dateTo": (today + timedelta(days=1)).isoformat(),
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

            fixture_id = fixture["id"]

            if fixture_id in seen:
                continue

            seen.add(fixture_id)
            selected.append(fixture)

        except (KeyError, TypeError, ValueError):
            add_error("Un match incomplet a été ignoré.")

    selected.sort(key=kickoff_of)

    return selected, len(matches), league_matches


def get_standings(code):
    """Classements TOTAL / HOME / AWAY d'une compétition."""
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
    total = standings.get("TOTAL", {}).get(team_id)

    if not total or total["played"] < 5:
        return None

    part = standings.get(side.upper(), {}).get(team_id)

    if part and part["played"] >= 4:
        scope = side
        row = part
    else:
        scope = "total"
        row = total

    if row["played"] <= 0:
        return None

    return {
        "gf": row["gf"] / row["played"],
        "ga": row["ga"] / row["played"],
        "scope": scope,
    }


def get_recent_form(team_id):
    data = api_get(f"teams/{team_id}/matches", {
        "status": "FINISHED",
    })

    raw_matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(raw_matches, list):
        return None

    now = datetime.now(PARIS)
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


def get_h2h_rate(match_id):
    data = api_get(f"matches/{match_id}/head2head", {
        "limit": 6,
    })

    raw_matches = data.get("matches") if isinstance(data, dict) else None

    if not isinstance(raw_matches, list):
        return None

    outcomes = []
    now = datetime.now(PARIS)

    for match in raw_matches:
        try:
            if match.get("status") != "FINISHED":
                continue

            age = (now - kickoff_of(match)).days

            if age < 0 or age > 3 * 366:
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


# ================= INDICE STATISTIQUE =================

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
        h2h_rate = get_h2h_rate(fixture["id"])

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

    home_name = home.get("shortName") or home.get("name")
    away_name = away.get("shortName") or away.get("name")

    return {
        "id": fixture["id"],
        "match": f"{home_name} - {away_name}",
        "league": fixture["competition"]["name"],
        "kickoff": kickoff,
        "score": score,
        "reason": (
            "BTTS sur les derniers matchs : "
            f"{home_name} "
            f"{home_form['btts_count']}/{home_form['n']}, "
            f"{away_name} "
            f"{away_form['btts_count']}/{away_form['n']}"
        ),
        "warnings": warnings,
    }


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

    for fixture in fixtures:
        if API_BLOCKED:
            break

        # Un match peut demander jusqu'à 3 requêtes.
        if API_CALLS + 4 > MAX_API_CALLS:
            add_error(
                "Analyse partielle : budget de requêtes atteint."
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
        + "\n\nAbsences et compositions : non fournies par "
        "football-data.org, à vérifier avant de jouer."
        + "\nIndice statistique, pas une probabilité garantie."
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
