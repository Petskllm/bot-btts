import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================
# BOT FOOTBALL — SPORTAPI
#
# - Tous les pays : aucune liste de championnats.
# - Tous les matchs du jour encore à venir sont examinés,
#   dans la limite du budget API.
# - Statistiques du classement + forme récente si exploitable.
# - Marchés : équipe marque, BTTS, Over 1,5.
# - Seuil réel obligatoire : 70 % avant arrondi.
# - Maximum 15 matchs distincts, 2 marchés par match.
# - Objectif 10 minimum seulement si les critères le permettent.
#
# Modèle simple non calibré. Aucun pari n'est garanti.
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PROBABILITY = 0.70
MIN_TARGET_MATCHES = 10
MAX_MATCHES = 15
MAX_MARKETS_PER_MATCH = 2

MIN_PLAYED = 6
MIN_FORM_MATCHES = 4
MAX_FORM_AGE_DAYS = 60

MAX_API_CALLS = 150
MIN_INTERVAL = 1.0
TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-world-bot/4.1",
})

CACHE = {}
ERRORS = []
CALLS = 0
LAST_CALL = None
BLOCKED = False


# ---------------- SECURITE ET TELEGRAM ----------------

def safe(value):
    text = str(value)

    for secret in (TOKEN, API_KEY, CHAT_ID):
        if secret:
            text = text.replace(secret, "[MASQUE]")

    return text


def add_error(message):
    message = safe(message)[:350]
    print("DIAGNOSTIC :", message, flush=True)

    if message not in ERRORS:
        ERRORS.append(message)


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
            description = safe(
                payload.get("description", "reponse invalide")
            )
            raise RuntimeError(
                f"Telegram HTTP {response.status_code} : "
                f"{description}"
            )


# ---------------- API ----------------

def api_get(endpoint):
    global CALLS, LAST_CALL, BLOCKED

    if endpoint in CACHE:
        return CACHE[endpoint]

    if BLOCKED or CALLS >= MAX_API_CALLS:
        return None

    if LAST_CALL is not None:
        delay = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
        if delay > 0:
            time.sleep(delay)

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
        add_error(f"{endpoint} : erreur reseau.")
        CACHE[endpoint] = None
        return None

    if response.status_code in (401, 403, 429):
        BLOCKED = True

    if not response.ok:
        add_error(
            f"{endpoint} : HTTP {response.status_code}."
        )
        CACHE[endpoint] = None
        return None

    try:
        payload = response.json()
    except ValueError:
        add_error(f"{endpoint} : JSON invalide.")
        CACHE[endpoint] = None
        return None

    if not isinstance(payload, dict):
        add_error(f"{endpoint} : format JSON inattendu.")
        CACHE[endpoint] = None
        return None

    if payload.get("success") is False:
        add_error(
            f"{endpoint} : "
            + str(
                payload.get("error")
                or payload.get("message")
                or "echec API"
            )
        )
        CACHE[endpoint] = None
        return None

    CACHE[endpoint] = payload
    return payload


def data_list(payload):
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    return data if isinstance(data, list) else None


# ---------------- OUTILS ----------------

def number(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    return result if math.isfinite(result) else None


def first_number(*values):
    for value in values:
        result = number(value)
        if result is not None:
            return result

    return None


def kickoff_of(fixture):
    raw = fixture.get("kickoff_utc")

    if not isinstance(raw, str) or not raw.strip():
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


def find_team_row(rows, wanted_id):
    if wanted_id is None:
        return None

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


def parse_bool(value):
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


# ---------------- PROFILS STATISTIQUES ----------------

def build_profile(row, today):
    if not isinstance(row, dict):
        return None

    all_stats = row.get("all")
    all_stats = (
        all_stats if isinstance(all_stats, dict) else {}
    )

    goals = all_stats.get("goals")
    goals = goals if isinstance(goals, dict) else {}

    played = first_number(
        row.get("played"),
        row.get("playedGames"),
        all_stats.get("played"),
    )

    goals_for = first_number(
        row.get("goals_for"),
        row.get("goalsFor"),
        goals.get("for"),
    )

    goals_against = first_number(
        row.get("goals_against"),
        row.get("goalsAgainst"),
        goals.get("against"),
    )

    if (
        played is None
        or goals_for is None
        or goals_against is None
    ):
        return None

    if (
        played < MIN_PLAYED
        or goals_for < 0
        or goals_against < 0
    ):
        return None

    season_gf = goals_for / played
    season_ga = goals_against / played

    recent = []
    form = row.get("form")

    if isinstance(form, list):
        for match in form:
            if not isinstance(match, dict):
                continue

            home_score = number(match.get("home_score"))
            away_score = number(match.get("away_score"))
            is_home = parse_bool(match.get("is_home"))

            if (
                home_score is None
                or away_score is None
                or is_home is None
            ):
                continue

            if home_score < 0 or away_score < 0:
                continue

            try:
                match_date = datetime.fromisoformat(
                    str(match.get("date"))[:10]
                ).date()
            except ValueError:
                continue

            age = (today - match_date).days

            # Date seule : exclusion des résultats du jour
            # pour ne pas utiliser un résultat à heure inconnue.
            if not 0 < age <= MAX_FORM_AGE_DAYS:
                continue

            if is_home:
                gf, ga = home_score, away_score
            else:
                gf, ga = away_score, home_score

            recent.append({
                "date": match_date,
                "gf": gf,
                "ga": ga,
            })

    recent.sort(key=lambda item: item["date"], reverse=True)
    recent = recent[:6]

    profile = {
        "played": int(played),
        "gf": season_gf,
        "ga": season_ga,
        "recent_n": len(recent),
        "btts": None,
        "over15": None,
    }

    if len(recent) >= MIN_FORM_MATCHES:
        n = len(recent)

        recent_gf = sum(m["gf"] for m in recent) / n
        recent_ga = sum(m["ga"] for m in recent) / n

        profile["gf"] = (
            0.70 * season_gf + 0.30 * recent_gf
        )
        profile["ga"] = (
            0.70 * season_ga + 0.30 * recent_ga
        )

        profile["btts"] = sum(
            m["gf"] > 0 and m["ga"] > 0
            for m in recent
        ) / n

        profile["over15"] = sum(
            m["gf"] + m["ga"] >= 2
            for m in recent
        ) / n

    return profile


# ---------------- MODELE ----------------

def analyse_match(fixture, rows, today):
    home_row = find_team_row(rows, fixture.get("home_id"))
    away_row = find_team_row(rows, fixture.get("away_id"))

    if home_row is None or away_row is None:
        return None, "Equipe absente du classement lu"

    home = build_profile(home_row, today)
    away = build_profile(away_row, today)

    if home is None or away is None:
        return None, "Stats absentes ou moins de 6 matchs"

    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)

    total = lambda_home + lambda_away
    p_btts = p_home * p_away
    p_over15 = 1 - math.exp(-total) * (1 + total)

    form_used = (
        home["btts"] is not None
        and away["btts"] is not None
    )

    if form_used:
        p_btts = (
            0.75 * p_btts
            + 0.25 * (home["btts"] + away["btts"]) / 2
        )

        p_over15 = (
            0.75 * p_over15
            + 0.25 * (home["over15"] + away["over15"]) / 2
        )

    probabilities = {
        "home": p_home,
        "away": p_away,
        "btts": p_btts,
        "over15": p_over15,
    }

    # Filtre appliqué AVANT tout arrondi d'affichage.
    picks = sorted(
        (
            (market, probability)
            for market, probability in probabilities.items()
            if probability >= MIN_PROBABILITY
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:MAX_MARKETS_PER_MATCH]

    return {
        "fixture": fixture,
        "home": home,
        "away": away,
        "picks": picks,
        "best": picks[0][1] if picks else 0,
        "form_used": form_used,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
    }, None


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

    payload = api_get(
        f"fixtures/date/{today.isoformat()}"
    )
    fixtures = data_list(payload)

    if fixtures is None:
        print(
            "REPONSE MATCHS NON RECONNUE :",
            safe(payload)[:2000],
            flush=True,
        )
        send_message(
            "❌ Impossible de lire les matchs SportAPI.\n\n"
            + "\n".join(ERRORS[:4])
            + "\nConsulte les logs GitHub."
        )
        return

    eligible = []
    seen = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            reasons["Format de match non reconnu"] += 1
            continue

        kickoff = kickoff_of(fixture)

        if kickoff is None:
            reasons["Horaire illisible"] += 1
            continue

        if kickoff.date() != today:
            reasons["Autre jour en heure de Paris"] += 1
            continue

        if kickoff <= now:
            reasons["Match deja commence ou termine"] += 1
            continue

        status = str(
            fixture.get("status") or ""
        ).strip().casefold()

        if status != "scheduled":
            reasons[
                f"Statut exclu : {status or 'absent'}"
            ] += 1
            continue

        if any(
            fixture.get(key) is None
            for key in ("league_id", "home_id", "away_id")
        ):
            reasons["Identifiants absents"] += 1
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

    print(
        f"MATCHS RECUS : {len(fixtures)} ; "
        f"A VENIR : {len(eligible)}",
        flush=True,
    )

    standings_cache = {}
    results = []
    processed = 0
    analysed = 0
    form_count = 0
    partial = False

    for fixture in eligible:
        # Ne pas publier un match ayant commencé
        # pendant l'exécution du bot.
        if kickoff_of(fixture) <= datetime.now(PARIS):
            reasons["Match commence pendant le lancement"] += 1
            processed += 1
            continue

        league_id = str(fixture["league_id"])

        if league_id not in standings_cache:
            if BLOCKED or CALLS >= MAX_API_CALLS:
                partial = True
                break

            standings_payload = api_get(
                f"standings/{league_id}"
            )

            if standings_payload is None:
                standings_cache[league_id] = (
                    None,
                    "Erreur d'acces au classement",
                )
            else:
                rows = data_list(standings_payload)

                if rows is None:
                    standings_cache[league_id] = (
                        None,
                        "Format classement non reconnu",
                    )
                    print(
                        f"CLASSEMENT INCONNU {league_id} :",
                        safe(standings_payload)[:2200],
                        flush=True,
                    )
                elif not rows:
                    standings_cache[league_id] = (
                        None,
                        "Classement vide",
                    )
                else:
                    standings_cache[league_id] = (
                        rows,
                        None,
                    )
                    print(
                        f"EXEMPLE CLASSEMENT {league_id} :",
                        safe(rows[0])[:1600],
                        flush=True,
                    )

        processed += 1
        rows, failure = standings_cache[league_id]

        if rows is None:
            reasons[failure] += 1
            continue

        try:
            result, failure = analyse_match(
                fixture, rows, today
            )
        except (
            TypeError,
            ValueError,
            KeyError,
            OverflowError,
            ZeroDivisionError,
        ):
            reasons["Erreur de lecture statistiques"] += 1
            continue

        if result is None:
            reasons[failure] += 1
            continue

        analysed += 1
        form_count += int(result["form_used"])

        if not result["picks"]:
            reasons["Aucun marche a 70 % minimum"] += 1
            continue

        results.append(result)

    # Dernière vérification des horaires avant publication.
    publication_time = datetime.now(PARIS)
    still_upcoming = []

    for result in results:
        if kickoff_of(result["fixture"]) > publication_time:
            still_upcoming.append(result)
        else:
            reasons["Selection commencee avant publication"] += 1

    # Classement par meilleure estimation NON arrondie.
    # A égalité : deuxième estimation, puis horaire.
    still_upcoming.sort(
        key=lambda result: (
            -result["best"],
            -result["picks"][-1][1],
            kickoff_of(result["fixture"]),
        )
    )

    selected = still_upcoming[:MAX_MATCHES]

    lines = [
        "⚽ SELECTION FOOTBALL — TOUS PAYS",
        publication_time.strftime(
            "📅 %d/%m/%Y · %H:%M Paris"
        ),
        "",
        f"📥 Matchs recus du jour : {len(fixtures)}",
        f"🌍 Matchs a venir au lancement : {len(eligible)}",
        f"🔎 Matchs examines : {processed}",
        f"📊 Matchs avec stats exploitables : {analysed}",
        f"📈 Dont forme recente utilisee : {form_count}",
        f"✅ Matchs admissibles a 70 % minimum : {len(still_upcoming)}",
        f"📤 Matchs selectionnes : {len(selected)}/15",
        "",
        "Seuil obligatoire : 70 % avant arrondi.",
        "Classement par meilleure estimation du modele.",
        "",
    ]

    for rank, result in enumerate(selected, 1):
        fixture = result["fixture"]

        home_name = str(
            fixture.get("home_team") or "Domicile"
        )
        away_name = str(
            fixture.get("away_team") or "Exterieur"
        )

        labels = {
            "home": f"{home_name} marque",
            "away": f"{away_name} marque",
            "btts": "BTTS — Oui",
            "over15": "Plus de 1,5 but",
        }

        lines.extend([
            f"#{rank} {home_name} — {away_name}",
            f"🏆 {fixture.get('league_name') or 'Competition'}",
            f"🕘 {kickoff_of(fixture).strftime('%H:%M')} Paris",
        ])

        for market, probability in result["picks"]:
            lines.append(
                f"• {labels[market]} : "
                f"estimation {probability:.0%}"
            )

        lines.append(
            "📊 Classement : "
            f"{result['home']['played']} / "
            f"{result['away']['played']} matchs renseignes"
        )

        if result["form_used"]:
            lines.append(
                "📈 Forme utilisee : "
                f"{result['home']['recent_n']} / "
                f"{result['away']['recent_n']} matchs"
            )
        else:
            lines.append(
                "📈 Moyennes du classement uniquement"
            )

        lines.extend([
            (
                "⚽ Moyennes projetees du modele : "
                f"{result['lambda_home']:.2f} — "
                f"{result['lambda_away']:.2f}"
            ),
            "",
        ])

    if len(selected) < MIN_TARGET_MATCHES:
        lines.extend([
            "⚠️ Moins de 10 matchs remplissent les criteres.",
            "Le seuil de 70 % reste obligatoire.",
            "Aucune selection ajoutee artificiellement.",
            "",
        ])

    if partial:
        lines.extend([
            "⚠️ Analyse partielle : budget API ou acces bloque.",
            (
                f"Matchs non examines : "
                f"{len(eligible) - processed}"
            ),
            "",
        ])

    if reasons:
        lines.append("🔎 Diagnostic :")

        for reason, count in reasons.most_common(10):
            lines.append(f"• {reason} : {count}")

    if ERRORS:
        lines.extend([
            "",
            "🛠️ API :",
            *ERRORS[:4],
        ])

    lines.extend([
        "",
        f"Requetes de ce lancement : {CALLS}",
        "",
        "Modele simple non calibre.",
        "Saison et phase des classements : a verifier.",
        "70 % est une estimation, pas une garantie.",
        "Sans comparaison aux cotes, ce classement ne mesure "
        "pas la rentabilite des paris.",
    ])

    print(
        "DIAGNOSTIC FINAL :",
        dict(reasons),
        flush=True,
    )

    send_message("\n".join(lines))


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
