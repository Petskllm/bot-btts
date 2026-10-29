import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — SPORTAPI
#
# Tous les pays, matchs du jour encore à venir.
#
# Historique :
# - jusqu'à 10 résultats récents par équipe ;
# - domicile/extérieur si suffisamment de résultats ;
# - forme du classement en secours.
#
# Marchés :
# - équipe +0,5 but ;
# - BTTS Oui ;
# - plus de 1,5 but.
#
# Filtre obligatoire : 70 à 80 % inclus, AVANT arrondi.
# Maximum : 15 matchs, 2 marchés par match.
#
# Telegram : pronostics + horaires + buts projetés.
# Diagnostics détaillés : logs GitHub uniquement.
#
# Pas d'appels de tirs cadrés : ils ne participent pas
# encore au calcul.
#
# Modèle expérimental non calibré, sans garantie.
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PROB = 0.70
MAX_PROB = 0.80

MAX_MATCHES = 15
MAX_MARKETS = 2

HISTORY_N = 10
MIN_HISTORY = 6
VENUE_N = 6
MAX_HISTORY_DAYS = 240
MAX_STALE_DAYS = 60
DECAY = 0.90

MAX_API_CALLS = 350
MIN_INTERVAL = 0.5
MAX_RUNTIME_SECONDS = 600
TIMEOUT = 20

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-history-bot/5.1",
})

CACHE = {}
TEAM_CACHE = {}
STANDINGS_CACHE = {}
ERRORS = []
REASONS = Counter()

CALLS = 0
LAST_CALL = None
BLOCKED = False
PARTIAL = False
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
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            raise RuntimeError("Erreur reseau Telegram.") from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or not payload.get("ok"):
            raise RuntimeError(
                f"Telegram HTTP {response.status_code} : "
                + safe(payload.get("description", "erreur"))
            )


# ---------------- API ----------------

def api_get(endpoint):
    global CALLS, LAST_CALL, BLOCKED

    if endpoint in CACHE:
        return CACHE[endpoint]

    if not budget_available():
        return None

    if LAST_CALL is not None:
        wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
        if wait > 0:
            time.sleep(wait)

    LAST_CALL = time.monotonic()
    CALLS += 1

    print(
        f"API {CALLS}/{MAX_API_CALLS} : {endpoint}",
        flush=True,
    )

    try:
        response = SESSION.get(
            f"{BASE_URL}/{endpoint}",
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        error(f"{endpoint} : erreur reseau.")
        CACHE[endpoint] = None
        return None

    if response.status_code in (401, 403, 429):
        BLOCKED = True

    if not response.ok:
        error(f"{endpoint} : HTTP {response.status_code}.")
        CACHE[endpoint] = None
        return None

    try:
        payload = response.json()
    except ValueError:
        error(f"{endpoint} : JSON invalide.")
        CACHE[endpoint] = None
        return None

    if (
        not isinstance(payload, dict)
        or payload.get("success") is False
    ):
        error(f"{endpoint} : reponse invalide.")
        CACHE[endpoint] = None
        return None

    CACHE[endpoint] = payload
    return payload


def get_list(payload, key="data"):
    if not isinstance(payload, dict):
        return []

    value = payload.get(key)
    return value if isinstance(value, list) else []


def log_payload_shape(label, payload):
    if not isinstance(payload, dict):
        print(
            label,
            "type :",
            type(payload).__name__,
            flush=True,
        )
        return

    print(
        label,
        "cles principales :",
        list(payload.keys()),
        flush=True,
    )

    for key in ("matches", "results", "data"):
        value = payload.get(key)

        if isinstance(value, list):
            print(
                label,
                f"{key} : liste de {len(value)} elements",
                flush=True,
            )

            if value:
                print(
                    label,
                    f"exemple {key} :",
                    safe(value[0])[:1800],
                    flush=True,
                )

        elif isinstance(value, dict):
            print(
                label,
                f"{key} : objet avec cles",
                list(value.keys())[:25],
                flush=True,
            )


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
        value = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
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
        return datetime.fromisoformat(
            str(match.get("date"))[:10]
        ).date()
    except ValueError:
        return None


def short_name(value):
    name = " ".join(str(value or "Equipe").split())
    return name if len(name) <= 25 else name[:24] + "…"


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


# ---------------- CLASSEMENT EN SECOURS ----------------

def standings_row(league_id, team_id):
    key = str(league_id)

    if key not in STANDINGS_CACHE:
        payload = api_get(f"standings/{league_id}")
        STANDINGS_CACHE[key] = get_list(payload)

        if not STANDINGS_CACHE[key]:
            log_payload_shape(
                f"CLASSEMENT {league_id}",
                payload,
            )

    for row in STANDINGS_CACHE[key]:
        if not isinstance(row, dict):
            continue

        team = row.get("team")

        if not isinstance(team, dict):
            continue

        if str(team.get("id")) == str(team_id):
            return row

    return None


# ---------------- HISTORIQUE DETAILLE ----------------

def parse_results(matches, team_id, today, slug=None):
    history = []
    rejected = Counter()
    statuses = Counter()
    examples = {}

    def reject(reason, match):
        rejected[reason] += 1

        if reason not in examples:
            examples[reason] = {
                "id": match.get("id"),
                "status": match.get("status"),
                "date": match.get("date"),
                "kickoff_utc": match.get("kickoff_utc"),
                "home_id": match.get("home_id"),
                "away_id": match.get("away_id"),
                "home_slug": match.get("home_slug"),
                "away_slug": match.get("away_slug"),
                "home_score": match.get("home_score"),
                "away_score": match.get("away_score"),
                "champs_disponibles": list(match.keys()),
            }

    for match in matches:
        if not isinstance(match, dict):
            rejected["Match non presente comme objet JSON"] += 1
            continue

        status = str(match.get("status") or "").strip()
        statuses[status or "ABSENT"] += 1

        # Critère inchangé pour observer les rejets sans
        # modifier silencieusement les données du modèle.
        if status.upper() != "FT":
            reject("Statut different de FT", match)
            continue

        date = date_of(match)

        if date is None:
            reject("Date absente ou illisible", match)
            continue

        age = (today - date).days

        if age <= 0:
            reject("Date du jour ou future", match)
            continue

        if age > MAX_HISTORY_DAYS:
            reject("Resultat trop ancien", match)
            continue

        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))

        if home_score is None or away_score is None:
            reject("Score absent ou illisible", match)
            continue

        if home_score < 0 or away_score < 0:
            reject("Score negatif", match)
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
            reject("Equipe impossible a identifier", match)
            continue

        history.append({
            "id": match.get("id"),
            "date": date,
            "venue": "home" if is_home else "away",
            "gf": home_score if is_home else away_score,
            "ga": away_score if is_home else home_score,
        })

    print(
        f"\nHISTORIQUE EQUIPE {team_id}",
        f"slug={slug}",
        f"recus={len(matches)}",
        f"retenus={len(history)}",
        flush=True,
    )

    print(
        "STATUTS HISTORIQUE :",
        dict(statuses),
        flush=True,
    )

    print(
        "REJETS HISTORIQUE :",
        dict(rejected),
        flush=True,
    )

    if len(history) < MIN_HISTORY:
        for reason, example in examples.items():
            print(
                f"EXEMPLE REJET EQUIPE {team_id} — {reason} :",
                safe(example),
                flush=True,
            )

        if not matches:
            print(
                f"HISTORIQUE EQUIPE {team_id} : "
                "aucun match dans la liste lue.",
                flush=True,
            )

    return history


def merge_history(*groups):
    unique = {}

    for group in groups:
        for match in group:
            identifier = match.get("id")

            if identifier is None:
                identifier = (
                    match["date"],
                    match["venue"],
                    match["gf"],
                    match["ga"],
                )

            unique[identifier] = match

    return sorted(
        unique.values(),
        key=lambda match: match["date"],
        reverse=True,
    )


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

        if (
            date is None
            or home is None
            or home_score is None
            or away_score is None
            or home_score < 0
            or away_score < 0
        ):
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

    history = parse_results(
        get_list(payload, "matches"),
        team_id,
        today,
    )
    history = merge_history(history)

    team = payload.get("team") if isinstance(payload, dict) else {}
    team = team if isinstance(team, dict) else {}

    slug = team.get("slug")
    row = None

    if len(history) < MIN_HISTORY:
        log_payload_shape(
            f"REPONSE EQUIPE {team_id}",
            payload,
        )

    # Un slug doit provenir de la réponse API :
    # ne pas le fabriquer à partir du nom.
    if len(history) < HISTORY_N and not slug:
        row = standings_row(league_id, team_id)
        row_team = row.get("team") if isinstance(row, dict) else {}

        if isinstance(row_team, dict):
            slug = row_team.get("slug")

    if len(history) < HISTORY_N and slug:
        slug_payload = api_get(
            f"teams/slug/{quote(str(slug), safe='')}"
        )

        extra = parse_results(
            get_list(slug_payload, "results"),
            team_id,
            today,
            slug=str(slug),
        )

        history = merge_history(history, extra)

        if len(history) < MIN_HISTORY:
            log_payload_shape(
                f"REPONSE SLUG {slug}",
                slug_payload,
            )

    if len(history) < MIN_HISTORY:
        if row is None:
            row = standings_row(league_id, team_id)

        fallback = history_from_form(row, today)

        # Choisir la source la plus complète au lieu de
        # mélanger des résultats avec identifiants incompatibles.
        if len(fallback) > len(history):
            history = fallback

            print(
                f"EQUIPE {team_id} : secours sur forme classement, "
                f"{len(history)} resultats.",
                flush=True,
            )

    print(
        f"HISTORIQUE FINAL EQUIPE {team_id} : "
        f"{len(history)} resultats exploitables",
        flush=True,
    )

    TEAM_CACHE[key] = history
    return history


# ---------------- MODELE ----------------

def weighted_stats(matches):
    weights = [
        DECAY ** index
        for index in range(len(matches))
    ]
    total = sum(weights)

    def average(function):
        return sum(
            weight * function(match)
            for weight, match in zip(weights, matches)
        ) / total

    return {
        "gf": average(lambda match: match["gf"]),
        "ga": average(lambda match: match["ga"]),
        "btts": average(
            lambda match: float(
                match["gf"] > 0 and match["ga"] > 0
            )
        ),
        "over15": average(
            lambda match: float(
                match["gf"] + match["ga"] >= 2
            )
        ),
    }


def profile(history, venue, today):
    recent = history[:HISTORY_N]

    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    overall = weighted_stats(recent)

    local_matches = [
        match for match in history
        if match["venue"] == venue
    ][:VENUE_N]

    if len(local_matches) >= 3:
        venue_weight = min(
            0.50,
            len(local_matches) / (len(local_matches) + 6),
        )
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

    result["n"] = len(recent)
    result["venue_n"] = len(local_matches)

    return result


def analyse(fixture, today):
    home_history = team_history(
        fixture["home_id"],
        fixture["league_id"],
        today,
    )

    away_history = team_history(
        fixture["away_id"],
        fixture["league_id"],
        today,
    )

    home = profile(home_history, "home", today)
    away = profile(away_history, "away", today)

    if home is None or away is None:
        return None

    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)
    total = lambda_home + lambda_away

    probabilities = {
        "home": p_home,
        "away": p_away,
        "btts": (
            0.75 * p_home * p_away
            + 0.25 * (home["btts"] + away["btts"]) / 2
        ),
        "over15": (
            0.75 * (
                1 - math.exp(-total) * (1 + total)
            )
            + 0.25 * (home["over15"] + away["over15"]) / 2
        ),
    }

    # Seuils appliqués sur les valeurs réelles avant arrondi.
    picks = sorted(
        (
            (market, probability)
            for market, probability in probabilities.items()
            if MIN_PROB <= probability <= MAX_PROB
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:MAX_MARKETS]

    print(
        "MODELE :",
        fixture.get("home_team"),
        fixture.get("away_team"),
        "echantillons :",
        home["n"],
        away["n"],
        "dom/ext :",
        home["venue_n"],
        away["venue_n"],
        "probabilites :",
        probabilities,
        flush=True,
    )

    return {
        "fixture": fixture,
        "picks": picks,
        "best": picks[0][1] if picks else 0,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
    }


# ---------------- MESSAGE COMPACT ----------------

def build_message(selected, partial):
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

        lines.append(
            f"{index}. {home} — {away} · {kickoff}"
        )

        labels = {
            "home": f"{home} +0,5 but",
            "away": f"{away} +0,5 but",
            "btts": "BTTS Oui",
            "over15": "Plus de 1,5 but",
        }

        for market, probability in result["picks"]:
            lines.append(
                f"• {labels[market]} : {probability:.0%}"
            )

        projection = (
            f"{result['lambda_home']:.2f} — "
            f"{result['lambda_away']:.2f}"
        ).replace(".", ",")

        lines.extend([
            f"⚽ Buts projetés : {projection}",
            "",
        ])

    if not selected:
        lines.extend([
            "Aucun marché estimé entre 70 et 80 %",
            "avec un historique suffisant.",
            "",
        ])

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

    payload = api_get(
        f"fixtures/date/{today.isoformat()}"
    )

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("data"), list)
    ):
        log_payload_shape("REPONSE MATCHS", payload)

        send_message(
            "⚠️ Analyse indisponible : "
            "impossible de lire les matchs SportAPI."
        )
        return

    fixtures = payload["data"]
    eligible = []
    seen = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            REASONS["Format match non reconnu"] += 1
            continue

        kickoff = kickoff_of(fixture)

        if kickoff is None:
            REASONS["Horaire illisible"] += 1
            continue

        if kickoff.date() != today or kickoff <= now:
            REASONS["Autre jour ou match deja commence"] += 1
            continue

        status = str(
            fixture.get("status") or ""
        ).strip().casefold()

        if status != "scheduled":
            REASONS["Statut non admissible"] += 1
            continue

        if any(
            fixture.get(field) is None
            for field in ("home_id", "away_id", "league_id")
        ):
            REASONS["Identifiants absents"] += 1
            continue

        identifier = fixture.get("id")

        if identifier is None:
            identifier = (
                fixture["home_id"],
                fixture["away_id"],
                str(fixture.get("kickoff_utc")),
            )

        if identifier in seen:
            continue

        seen.add(identifier)
        eligible.append(fixture)

    eligible.sort(key=kickoff_of)

    results = []
    analysed = 0
    examined = 0

    for fixture in eligible:
        if not budget_available():
            PARTIAL = True
            break

        examined += 1

        if kickoff_of(fixture) <= datetime.now(PARIS):
            REASONS["Match commence pendant le lancement"] += 1
            continue

        try:
            result = analyse(fixture, today)
        except (
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
            ZeroDivisionError,
        ) as exc:
            error(
                "Lecture historique impossible : "
                + type(exc).__name__
            )
            REASONS["Erreur lecture historique"] += 1
            continue

        if result is None:
            REASONS["Historique insuffisant ou trop ancien"] += 1
            continue

        analysed += 1

        if result["picks"]:
            results.append(result)
        else:
            REASONS["Aucun marche entre 70 et 80 %"] += 1

    # Si une limite est atteinte pendant la dernière analyse,
    # le diagnostic doit aussi le signaler.
    if BLOCKED:
        PARTIAL = True

    results.sort(
        key=lambda result: (
            -result["best"],
            kickoff_of(result["fixture"]),
        )
    )

    publication_time = datetime.now(PARIS)

    still_upcoming = [
        result
        for result in results
        if kickoff_of(result["fixture"]) > publication_time
    ]

    selected = still_upcoming[:MAX_MATCHES]

    print("\n===== BILAN FINAL =====", flush=True)
    print("MATCHS RECUS :", len(fixtures), flush=True)
    print("MATCHS A VENIR :", len(eligible), flush=True)
    print("MATCHS EXAMINES :", examined, flush=True)
    print("MATCHS ANALYSABLES :", analysed, flush=True)
    print("MATCHS PUBLIES :", len(selected), flush=True)
    print("REQUETES :", CALLS, flush=True)
    print("ANALYSE PARTIELLE :", PARTIAL, flush=True)
    print("DIAGNOSTIC :", dict(REASONS), flush=True)
    print("ERREURS :", ERRORS, flush=True)

    send_message(
        build_message(selected, PARTIAL)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "ERREUR FATALE :",
            type(exc).__name__,
            safe(exc),
            flush=True,
        )
        raise SystemExit(1)
