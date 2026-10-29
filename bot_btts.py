import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — SPORTAPI
# Marchés : équipe marque / BTTS / Over 1,5
# Publication : maximum 10 matchs sur Telegram
#
# Les probabilités sont des estimations non calibrées.
# Ce code ne garantit ni résultat ni rentabilité.
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MAX_MATCHES = 10
MAX_MARKETS_PER_MATCH = 2
MIN_PROBABILITY = 0.70
MIN_SEASON_MATCHES = 6

MAX_API_CALLS = 80
MIN_INTERVAL = 1.0
TIMEOUT = 25

# Noms exacts observés + variantes.
# Cette liste n'est pas encore toute l'Europe.
LEAGUES = {
    "Premier League",
    "English Premier League",
    "Spanish La Liga",
    "La Liga",
    "Italian Serie A",
    "Serie A",
    "German Bundesliga",
    "Bundesliga",
    "French Ligue 1",
    "Ligue 1",

    "UEFA Champions League",
    "UEFA Europa League",
    "UEFA Conference League",
    "UEFA Europa Conference League",
    "UEFA Women's Champions League",

    "Portuguese Primeira Liga",
    "Primeira Liga",
    "Dutch Eredivisie",
    "Eredivisie",
    "Belgian Pro League",
    "Belgian First Division A",
    "Jupiler Pro League",
    "Turkish Super Lig",
    "Süper Lig",
    "Swiss Super League",
    "Austrian Football Bundesliga",
    "Austrian Bundesliga",
    "Polish Ekstraklasa",
    "Ekstraklasa",
    "Swedish Allsvenskan",
    "Allsvenskan",
    "Norwegian Eliteserien",
    "Eliteserien",
    "Finnish Veikkausliiga",
    "Veikkausliiga",

    "English League Cup",
    "Spanish Copa Del Rey",
}

LEAGUE_NAMES = {name.casefold() for name in LEAGUES}

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-bot-diagnostic/2.0",
})

CACHE = {}
ERRORS = []
API_CALLS = 0
LAST_CALL = None
API_BLOCKED = False

MARKETS = {
    "home": "marque au moins un but",
    "away": "marque au moins un but",
    "btts": "BTTS — Oui",
    "over15": "Plus de 1,5 but",
}


# ---------------- SECURITE / TELEGRAM ----------------

def mask(value):
    text = str(value)
    for secret in (API_KEY, TOKEN, CHAT_ID):
        if secret:
            text = text.replace(secret, "[MASQUE]")
    return text


def log_error(message):
    message = mask(message)[:400]
    print("DIAGNOSTIC :", message, flush=True)
    if message not in ERRORS:
        ERRORS.append(message)


def send_message(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")

    # Découpage volontairement conservateur.
    for start in range(0, len(text), 3000):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": text[start:start + 3000],
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            raise RuntimeError("Erreur réseau Telegram.") from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or not payload.get("ok"):
            raise RuntimeError(
                f"Telegram : HTTP {response.status_code}, "
                + mask(payload.get("description", "réponse invalide"))
            )


# ---------------- API ----------------

def api_get(endpoint):
    global API_CALLS, LAST_CALL, API_BLOCKED

    if endpoint in CACHE:
        return CACHE[endpoint]

    if API_BLOCKED or API_CALLS >= MAX_API_CALLS:
        log_error("Budget API atteint ou accès bloqué.")
        return None

    if LAST_CALL is not None:
        wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
        if wait > 0:
            time.sleep(wait)

    API_CALLS += 1
    LAST_CALL = time.monotonic()

    print(
        f"API {API_CALLS}/{MAX_API_CALLS} : {endpoint}",
        flush=True,
    )

    try:
        response = SESSION.get(
            f"{BASE_URL}/{endpoint}",
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        log_error(f"{endpoint} : erreur réseau.")
        return None

    if response.status_code in (401, 403, 429):
        API_BLOCKED = True

    if not response.ok:
        log_error(f"{endpoint} : HTTP {response.status_code}.")
        return None

    try:
        payload = response.json()
    except ValueError:
        log_error(f"{endpoint} : réponse non JSON.")
        return None

    if isinstance(payload, dict):
        if payload.get("success") is False:
            log_error(
                f"{endpoint} : "
                + str(payload.get("message", "échec signalé par API"))
            )
            return None

    CACHE[endpoint] = payload
    return payload


def extract_list(payload, keys):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                nested = extract_list(value, keys)
                if nested is not None:
                    return nested

    return None


# ---------------- LECTURE DES DONNEES ----------------

def kickoff_of(fixture):
    raw = fixture.get("kickoff_utc")
    if not isinstance(raw, str) or not raw:
        return None

    try:
        value = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    # Le champ est explicitement nommé kickoff_utc.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(PARIS)


def number(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        result = float(value)
    except (ValueError, TypeError):
        return None

    return result if math.isfinite(result) else None


def first_number(*values):
    for value in values:
        result = number(value)
        if result is not None:
            return result
    return None


def find_team_row(rows, wanted_id):
    for row in rows:
        if not isinstance(row, dict):
            continue

        team = row.get("team")
        team = team if isinstance(team, dict) else {}

        row_id = team.get("id")
        if row_id is None:
            row_id = row.get("team_id")

        if row_id is not None and str(row_id) == str(wanted_id):
            return row

    return None


def profile_from_row(row):
    if not isinstance(row, dict):
        return None

    all_stats = row.get("all")
    all_stats = all_stats if isinstance(all_stats, dict) else {}

    goals = all_stats.get("goals")
    goals = goals if isinstance(goals, dict) else {}

    played = first_number(
        row.get("played"),
        row.get("playedGames"),
        all_stats.get("played"),
    )
    gf = first_number(
        row.get("goals_for"),
        row.get("goalsFor"),
        goals.get("for"),
    )
    ga = first_number(
        row.get("goals_against"),
        row.get("goalsAgainst"),
        goals.get("against"),
    )

    # Ne pas remplacer des statistiques absentes par zéro.
    if played is None or gf is None or ga is None:
        return None

    if played < MIN_SEASON_MATCHES or gf < 0 or ga < 0:
        return None

    return {
        "played": int(played),
        "gf": gf / played,
        "ga": ga / played,
    }


# ---------------- ANALYSE ----------------

def analyse_match(fixture, standings_cache, reasons):
    league_id = fixture.get("league_id")

    if league_id is None:
        reasons["Identifiant de ligue absent"] += 1
        return None

    cache_key = str(league_id)

    if cache_key not in standings_cache:
        payload = api_get(f"standings/{league_id}")
        rows = extract_list(
            payload,
            ("data", "standings", "table", "rows", "response"),
        )

        standings_cache[cache_key] = rows

        if rows:
            # Diagnostic si le format du classement diffère.
            print(
                f"EXEMPLE CLASSEMENT {league_id} :",
                mask(rows[0])[:1800],
                flush=True,
            )

    rows = standings_cache[cache_key]

    if not rows:
        reasons["Classement absent ou format non reconnu"] += 1
        return None

    home_row = find_team_row(rows, fixture.get("home_id"))
    away_row = find_team_row(rows, fixture.get("away_id"))

    if home_row is None or away_row is None:
        reasons["Equipe introuvable dans le classement"] += 1
        return None

    home = profile_from_row(home_row)
    away = profile_from_row(away_row)

    if home is None or away is None:
        reasons["Stats absentes ou moins de 6 matchs joués"] += 1
        return None

    # Modèle Poisson simple basé sur les moyennes de saison.
    # Ce n'est pas le modèle de forme récente de l'ancien bot.
    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)
    total = lambda_home + lambda_away

    probabilities = {
        "home": p_home,
        "away": p_away,
        "btts": p_home * p_away,
        "over15": 1 - math.exp(-total) * (1 + total),
    }

    picks = [
        (market, probability)
        for market, probability in probabilities.items()
        if probability >= MIN_PROBABILITY
    ]
    picks.sort(key=lambda item: item[1], reverse=True)

    return {
        "fixture": fixture,
        "kickoff": kickoff_of(fixture),
        "picks": picks[:MAX_MARKETS_PER_MATCH],
        "best": picks[0][1] if picks else 0,
        "home": home,
        "away": away,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
    }


# ---------------- EXECUTION ----------------

def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Secrets TELEGRAM_TOKEN ou CHAT_ID absents."
        )

    if not API_KEY:
        send_message("❌ Le secret API_KEY est absent.")
        return

    now = datetime.now(PARIS)
    today = now.date()
    reasons = Counter()
    standings_cache = {}

    payload = api_get(f"fixtures/date/{today.isoformat()}")
    fixtures = extract_list(
        payload,
        ("data", "fixtures", "matches", "events", "response"),
    )

    if fixtures is None:
        send_message(
            "❌ Impossible de lire les matchs SportAPI.\n\n"
            + "\n".join(ERRORS[:4])
            + "\nRegarde les logs GitHub."
        )
        print(
            "FORMAT REPONSE :",
            mask(payload)[:1800],
            flush=True,
        )
        return

    league_names = sorted({
        str(f.get("league_name") or "INCONNU")
        for f in fixtures
        if isinstance(f, dict)
    })

    print("LIGUES RECUES :", league_names, flush=True)

    if fixtures:
        print(
            "EXEMPLE MATCH :",
            mask(fixtures[0])[:2200],
            flush=True,
        )

    eligible = []
    seen = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            reasons["Format match invalide"] += 1
            continue

        league = str(fixture.get("league_name") or "").strip()

        if league.casefold() not in LEAGUE_NAMES:
            reasons["Competition hors liste Europe"] += 1
            continue

        kickoff = kickoff_of(fixture)

        if kickoff is None:
            reasons["Date illisible"] += 1
            continue

        if kickoff.date() != today:
            reasons["Autre jour en heure de Paris"] += 1
            continue

        if kickoff <= now:
            reasons["Match deja commence ou passe"] += 1
            continue

        status = str(fixture.get("status") or "").strip().casefold()

        if status not in {"scheduled", "not started", "ns"}:
            reasons[f"Statut non retenu : {status or 'absent'}"] += 1
            continue

        if (
            fixture.get("home_id") is None
            or fixture.get("away_id") is None
        ):
            reasons["Identifiants equipes absents"] += 1
            continue

        identifier = fixture.get("id")
        if identifier is None:
            identifier = (
                fixture.get("home_id"),
                fixture.get("away_id"),
                str(fixture.get("kickoff_utc")),
            )

        if identifier in seen:
            continue

        seen.add(identifier)
        eligible.append(fixture)

    eligible.sort(key=kickoff_of)

    results = []
    analysed = 0
    partial = False

    for fixture in eligible:
        if API_BLOCKED or API_CALLS >= MAX_API_CALLS:
            partial = True
            break

        try:
            result = analyse_match(
                fixture, standings_cache, reasons
            )
        except (TypeError, ValueError, KeyError, OverflowError):
            reasons["Format statistique non reconnu"] += 1
            continue

        if result is not None:
            analysed += 1
            if result["picks"]:
                results.append(result)

    results.sort(key=lambda item: item["best"], reverse=True)
    selected = results[:MAX_MATCHES]
    selected.sort(key=lambda item: item["kickoff"])

    lines = [
        "⚽ ANALYSE FOOTBALL",
        now.strftime("📅 %d/%m/%Y · %H:%M Paris"),
        "",
        f"📥 Matchs reçus : {len(fixtures)}",
        f"🇪🇺 Matchs à venir dans la liste : {len(eligible)}",
        f"📊 Matchs avec statistiques exploitables : {analysed}",
        f"📤 Matchs sélectionnés : {len(selected)}/10",
        "",
    ]

    for index, result in enumerate(selected, 1):
        fixture = result["fixture"]
        home_name = str(fixture.get("home_team") or "Domicile")
        away_name = str(fixture.get("away_team") or "Exterieur")

        lines.extend([
            f"{index}. {home_name} — {away_name}",
            f"🏆 {fixture.get('league_name')}",
            f"🕘 {result['kickoff'].strftime('%H:%M')}",
        ])

        for market, probability in result["picks"]:
            if market == "home":
                label = f"{home_name} marque"
            elif market == "away":
                label = f"{away_name} marque"
            else:
                label = MARKETS[market]

            lines.append(
                f"• {label} : estimation {probability:.0%}"
            )

        lines.extend([
            (
                "📊 Echantillon saison : "
                f"{result['home']['played']} / "
                f"{result['away']['played']} matchs"
            ),
            (
                "⚽ Moyennes projetees du modele : "
                f"{result['lambda_home']:.2f} — "
                f"{result['lambda_away']:.2f}"
            ),
            "",
        ])

    if not selected:
        lines.extend([
            "Aucune selection publiable avec les donnees disponibles.",
            "Cela ne signifie pas qu'il n'existe aucun match.",
            "",
        ])

    if reasons:
        lines.append("🔎 Diagnostic :")
        for reason, count in reasons.most_common(8):
            lines.append(f"• {reason} : {count}")

    if not eligible:
        lines.extend([
            "",
            "🏆 Competitions recues :",
            *league_names[:40],
        ])

    if partial:
        lines.append(
            "\n⚠️ Analyse partielle : budget API ou acces bloque."
        )

    if ERRORS:
        lines.extend(["", "🛠️ API :", *ERRORS[:3]])

    lines.extend([
        "",
        f"Requetes de ce lancement : {API_CALLS}",
        "Modele simple sur moyennes de saison, non calibre.",
        "Estimations sans garantie ; aucun pari n'est sur.",
    ])

    print("DIAGNOSTIC FINAL :", dict(reasons), flush=True)
    send_message("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "ERREUR FATALE :",
            type(exc).__name__,
            mask(exc),
            flush=True,
        )
        raise SystemExit(1)
