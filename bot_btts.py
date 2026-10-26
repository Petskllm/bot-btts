# bot_btts.py
# Version GitHub Actions - BTTS Oui (25 analyses / 10 envois max)

import os
import time
import sqlite3
import requests
import pytz
from datetime import datetime

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")

API_HOST = "v3.football.api-sports.io"
HEADERS = {
    "x-apisports-key": API_KEY
}

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

MAX_ANALYSES = 25
MAX_ENVOIS = 10
MIN_SCORE = 65

PARIS_TZ = pytz.timezone("Europe/Paris")

# ====================== BASE DE DONNÉES (éphémère) ======================
def init_db():
    conn = sqlite3.connect("predictions.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            match TEXT,
            league TEXT,
            score REAL,
            result TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()

def save_prediction(date, match, league, score):
    conn = sqlite3.connect("predictions.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO predictions (date, match, league, score) VALUES (?, ?, ?, ?)",
        (date, match, league, score)
    )
    conn.commit()
    conn.close()

# ====================== API FOOTBALL ======================
def api_get(endpoint, params=None):
    url = f"https://{API_HOST}/{endpoint}"
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("response", [])
        else:
            print(f"Erreur API {r.status_code}: {r.text}")
            return []
    except Exception as e:
        print(f"Exception API: {e}")
        return []

def get_current_season():
    now = datetime.now(PARIS_TZ)
    return now.year if now.month >= 7 else now.year - 1

def get_fixtures_today():
    today = datetime.now(PARIS_TZ).strftime("%Y-%m-%d")
    season = get_current_season()
    all_fixtures = []

    for league_id in LEAGUES.keys():
        data = api_get("fixtures", {
            "date": today,
            "league": league_id,
            "season": season
        })
        all_fixtures.extend(data)
        time.sleep(0.4)

    return all_fixtures[:MAX_ANALYSES]

def get_team_stats(team_id, league_id, season):
    data = api_get("teams/statistics", {
        "team": team_id,
        "league": league_id,
        "season": season
    })
    return data[0] if data else None

def get_h2h(team1_id, team2_id):
    data = api_get("fixtures/headtohead", {
        "h2h": f"{team1_id}-{team2_id}",
        "last": 8
    })
    return data

# ====================== CALCUL DU SCORE BTTS ======================
def calculate_btts_score(fixture):
    try:
        teams = fixture["teams"]
        home = teams["home"]
        away = teams["away"]
        league_id = fixture["league"]["id"]
        season = fixture["league"]["season"]

        home_stats = get_team_stats(home["id"], league_id, season)
        time.sleep(0.35)
        away_stats = get_team_stats(away["id"], league_id, season)
        time.sleep(0.35)

        if not home_stats or not away_stats:
            return 0, "Données insuffisantes", ""

        def extract_goals(stats):
            try:
                played = stats["fixtures"]["played"]["total"] or 1
                gf = stats["goals"]["for"]["total"]["total"] or 0
                ga = stats["goals"]["against"]["total"]["total"] or 0
                return gf / played, ga / played
            except:
                return 1.2, 1.2

        home_gf, home_ga = extract_goals(home_stats)
        away_gf, away_ga = extract_goals(away_stats)

        score = 0
        raisons = []

        avg_goals = (home_gf + home_ga + away_gf + away_ga) / 2
        if avg_goals >= 3.0:
            score += 25
            raisons.append("Fort volume de buts")
        elif avg_goals >= 2.5:
            score += 18
            raisons.append("Bon volume de buts")
        elif avg_goals >= 2.1:
            score += 10

        btts_proxy = min(home_gf, away_gf) + min(home_ga, away_ga)
        if btts_proxy >= 2.4:
            score += 28
            raisons.append("Les deux équipes marquent souvent")
        elif btts_proxy >= 1.9:
            score += 18
        elif btts_proxy >= 1.5:
            score += 8

        h2h = get_h2h(home["id"], away["id"])
        time.sleep(0.35)
        if h2h:
            btts_h2h = 0
            for m in h2h[:6]:
                goals = m["goals"]
                if goals["home"] is not None and goals["away"] is not None:
                    if goals["home"] > 0 and goals["away"] > 0:
                        btts_h2h += 1
            if len(h2h) >= 3:
                pct = btts_h2h / min(len(h2h), 6)
                if pct >= 0.65:
                    score += 10
                    raisons.append("H2H souvent BTTS")
                elif pct >= 0.45:
                    score += 5

        score += 5
        score = min(score, 100)

        raison_principale = raisons[0] if raisons else "Statistiques offensives correctes"
        vigilance = ""
        if score < 75 and avg_goals < 2.3:
            vigilance = "Volume de buts un peu juste"

        return round(score), raison_principale, vigilance

    except Exception as e:
        print(f"Erreur calcul score: {e}")
        return 0, "Erreur de calcul", ""

# ====================== ANALYSE DU JOUR ======================
def analyse_du_jour():
    print(f"[{datetime.now(PARIS_TZ)}] Lancement de l'analyse...")
    fixtures = get_fixtures_today()

    if not fixtures:
        send_message("⚽ Aucun match trouvé aujourd'hui ou erreur API.")
        return

    results = []

    for fix in fixtures:
        score, raison, vigilance = calculate_btts_score(fix)
        if score >= MIN_SCORE:
            match_name = f"{fix['teams']['home']['name']} - {fix['teams']['away']['name']}"
            heure = datetime.fromisoformat(fix["fixture"]["date"].replace("Z", "+00:00")).astimezone(PARIS_TZ).strftime("%H:%M")
            league = fix["league"]["name"]
            results.append({
                "match": match_name,
                "heure": heure,
                "score": score,
                "raison": raison,
                "vigilance": vigilance,
                "league": league
            })

    results = sorted(results, key=lambda x: x["score"], reverse=True)[:MAX_ENVOIS]

    today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")

    if not results:
        send_message("⚽ Aucun BTTS Oui ne dépasse 65% aujourd’hui.")
        return

    message = f"⚽ BTTS OUI — {today} — FILTRE 65%\n\n"

    for i, r in enumerate(results, 1):
        message += f"{i}. {r['match']} — {r['heure']} — {r['score']}%\n"
        message += f"✅ {r['raison']}\n"
        if r["vigilance"]:
            message += f"⚠️ {r['vigilance']}\n"
        message += "\n"

        save_prediction(
            datetime.now(PARIS_TZ).strftime("%Y-%m-%d"),
            r["match"],
            r["league"],
            r["score"]
        )

    send_message(message.strip())
    print("Analyse terminée et message envoyé.")

def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        print("Message Telegram envoyé avec succès")
    except Exception as e:
        print(f"Erreur envoi Telegram: {e}")

# ====================== MAIN ======================
if __name__ == "__main__":
    init_db()
    analyse_du_jour()
