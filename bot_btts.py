import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ============================================================
# SPORTAPI — FOOTBALL
#
# Tous les matchs du jour encore à venir, tous pays.
# Historique équipe + domicile/extérieur + récence.
#
# Marchés :
# - équipe +0,5 but
# - BTTS Oui
# - plus de 1,5 but
#
# Filtre : 70 à 80 % inclus, AVANT arrondi.
# Maximum 15 matchs, 2 marchés par match.
# Pas de remplissage artificiel pour atteindre 10 matchs.
#
# Les tirs cadrés historiques sont collectés pour contrôle,
# mais ne modifient pas arbitrairement les probabilités.
#
# Modèle expérimental NON calibré.
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

# Au maximum 3 matchs passés par équipe pour le contrôle
# des tirs cadrés. Ces appels sont effectués après l'analyse.
SHOT_HISTORY_N = 3
MAX_SHOT_CALLS = 60

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-history-bot/5.0",
})

CACHE = {}
TEAM_CACHE = {}
STANDINGS_CACHE = {}
ERRORS = []
REASONS = Counter()

CALLS = 0
SHOT_CALLS = 0
LAST_CALL = None
BLOCKED = False
PARTIAL = False
STARTED = time.monotonic()


# ---------------- LOGS / SECURITE ----------------

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
    print(f"API {CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

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
        if value.lower() in {"true", "1"}:
            return True
        if value.lower() in {"false", "0"}:
            return False
    return None


# ---------------- CLASSEMENTS : SOURCE DE SECOURS ----------------

def standings_row(league_id, team_id):
    key = str(league_id)

    if key not in STANDINGS_CACHE:
        STANDINGS_CACHE[key] = get_list(
            api_get(f"standings/{league_id}")
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


# ---------------- HISTORIQUE DES EQUIPES ----------------

def parse_results(matches, team_id, today, slug=None):
    history = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        # FT uniquement : évite les scores incluant une prolongation.
        if str(match.get("status") or "").upper() != "FT":
            continue

        date = date_of(match)
        if date is None:
            continue

        age = (today - date).days
        if not 0 < age <= MAX_HISTORY_DAYS:
            continue

        hs = number(match.get("home_score"))
        aws = number(match.get("away_score"))
        if hs is None or aws is None or hs < 0 or aws < 0:
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
            "gf": hs if is_home else aws,
            "ga": aws if is_home else hs,
        })

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
        hs = number(match.get("home_score"))
        aws = number(match.get("away_score"))

        if (
            date is None or home is None
            or hs is None or aws is None
            or hs < 0 or aws < 0
        ):
            continue

        if not 0 < (today - date).days <= MAX_HISTORY_DAYS:
            continue

        history.append({
            "id": None,
            "date": date,
            "venue": "home" if home else "away",
            "gf": hs if home else aws,
            "ga": aws if home else hs,
        })

    return merge_history(history)


def team_history(team_id, league_id, today):
    key = str(team_id)

    if key in TEAM_CACHE:
        return TEAM_CACHE[key]

    payload = api_get(f"teams/{team_id}")
    history = parse_results(
        get_list(payload, "matches"), team_id, today
    )

    team = payload.get("team") if isinstance(payload, dict) else {}
    team = team if isinstance(team, dict) else {}
    slug = team.get("slug")
    row = None

    # Ne jamais fabriquer un slug à partir du nom.
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

    # Si l'historique détaillé est insuffisant,
    # secours sur la forme du classement, sans mélanger
    # des doublons identifiés différemment.
    if len(history) < MIN_HISTORY:
        if row is None:
            row = standings_row(league_id, team_id)

        fallback = history_from_form(row, today)
        if len(fallback) > len(history):
            history = fallback
            print(
                f"EQUIPE {team_id} : historique de secours "
                f"sur forme classement ({len(history)} matchs)",
                flush=True,
            )

    TEAM_CACHE[key] = history
    return history


# ---------------- MODELE ----------------

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
        "btts": average(
            lambda m: float(m["gf"] > 0 and m["ga"] > 0)
        ),
        "over15": average(
            lambda m: float(m["gf"] + m["ga"] >= 2)
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
        match for match in history if match["venue"] == venue
    ][:VENUE_N]

    # Réduction du poids domicile/extérieur si échantillon faible.
    venue_weight = (
        min(0.50, len(local_matches) / (len(local_matches) + 6))
        if len(local_matches) >= 3 else 0
    )

    local = (
        weighted_stats(local_matches)
        if venue_weight else overall
    )

    result = {
        key: (
            (1 - venue_weight) * overall[key]
            + venue_weight * local[key]
        )
        for key in overall
    }

    result["n"] = len(recent)
    result["venue_n"] = len(local_matches)
    return result


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

    lh = (home["gf"] + away["ga"]) / 2
    la = (away["gf"] + home["ga"]) / 2

    ph = 1 - math.exp(-lh)
    pa = 1 - math.exp(-la)
    total = lh + la

    probabilities = {
        "home": ph,
        "away": pa,
        "btts": (
            0.75 * ph * pa
            + 0.25 * (home["btts"] + away["btts"]) / 2
        ),
        "over15": (
            0.75 * (1 - math.exp(-total) * (1 + total))
            + 0.25 * (home["over15"] + away["over15"]) / 2
        ),
    }

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
        "echantillons :", home["n"], away["n"],
        "dom/ext :", home["venue_n"], away["venue_n"],
        "probabilites :", probabilities,
        flush=True,
    )

    return {
        "fixture": fixture,
        "picks": picks,
        "best": picks[0][1] if picks else 0,
        "lh": lh,
        "la": la,
    }


# ---------------- TIRS CADRES : CONTROLE HISTORIQUE ----------------

def inspect_shots(team_id):
    global SHOT_CALLS

    observed = []

    for match in TEAM_CACHE.get(str(team_id), [])[:SHOT_HISTORY_N]:
        match_id = match.get("id")
        if match_id is None:
            continue

        endpoint = f"fixtures/{match_id}/stats"

        if endpoint not in CACHE:
            if SHOT_CALLS >= MAX_SHOT_CALLS or not budget_available():
                break
            SHOT_CALLS += 1

        payload = api_get(endpoint)
        if not isinstance(payload, dict):
            continue
        if payload.get("available") is not True:
            continue

        data = payload.get("data")
        if not isinstance(data, dict):
            continue

        side = data.get(match["venue"])
        if not isinstance(side, dict):
            continue

        shots = number(side.get("shots_on_target"))
        if shots is not None and shots >= 0:
            observed.append(shots)

    if observed:
        print(
            f"TIRS CADRES EQUIPE {team_id} : "
            f"{sum(observed) / len(observed):.2f} "
            f"sur {len(observed)} matchs. "
            "Controle uniquement, pas de correction probabiliste.",
            flush=True,
        )


# ---------------- AFFICHAGE ----------------

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

        lines.append(
            f"{index}. {home} — {away} · "
            f"{kickoff_of(fixture).strftime('%H:%M')}"
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
            f"{result['lh']:.2f} — {result['la']:.2f}"
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

    today = datetime.now(PARIS).date()
    payload = api_get(f"fixtures/date/{today.isoformat()}")

    if not isinstance(payload, dict) or not isinstance(
        payload.get("data"), list
    ):
        send_message(
            "⚠️ Analyse indisponible : matchs SportAPI illisibles."
        )
        return

    eligible = []
    seen = set()
    now = datetime.now(PARIS)

    for fixture in payload["data"]:
        if not isinstance(fixture, dict):
            continue

        kickoff = kickoff_of(fixture)
        if kickoff is None:
            REASONS["Horaire illisible"] += 1
            continue

        if kickoff.date() != today or kickoff <= now:
            REASONS["Autre jour ou match deja commence"] += 1
            continue

        if str(fixture.get("status") or "").casefold() != "scheduled":
            REASONS["Statut non admissible"] += 1
            continue

        if any(
            fixture.get(key) is None
            for key in ("home_id", "away_id", "league_id")
        ):
            REASONS["Identifiants absents"] += 1
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

    eligible.sort(key=kickoff_of)

    results = []
    analysed = 0

    # Priorité à l'analyse des équipes, avant les appels de contrôle.
    for fixture in eligible:
        if not budget_available():
            PARTIAL = True
            break

        if kickoff_of(fixture) <= datetime.now(PARIS):
            REASONS["Match commence pendant le lancement"] += 1
            continue

        try:
            result = analyse(fixture, today)
        except (ValueError, TypeError, KeyError, OverflowError):
            error("Format historique non reconnu pour un match.")
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

    results.sort(
        key=lambda result: (
            -result["best"],
            kickoff_of(result["fixture"]),
        )
    )

    # Contrôle de tirs pour les équipes des meilleures sélections.
    inspected = set()

    for result in results[:MAX_MATCHES]:
        for key in ("home_id", "away_id"):
            team_id = result["fixture"][key]
            if team_id not in inspected and budget_available():
                inspect_shots(team_id)
                inspected.add(team_id)

    # Retirer les matchs ayant commencé pendant les appels.
    publication_time = datetime.now(PARIS)
    selected = [
        result for result in results
        if kickoff_of(result["fixture"]) > publication_time
    ][:MAX_MATCHES]

    print("MATCHS RECUS :", len(payload["data"]), flush=True)
    print("MATCHS A VENIR :", len(eligible), flush=True)
    print("MATCHS ANALYSABLES :", analysed, flush=True)
    print("MATCHS PUBLIES :", len(selected), flush=True)
    print("REQUETES :", CALLS, flush=True)
    print("DIAGNOSTIC :", dict(REASONS), flush=True)
    print("ERREURS :", ERRORS, flush=True)

    send_message(build_message(selected, PARTIAL))


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
