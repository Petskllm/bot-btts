# bot_btts.py
# Bot Telegram BTTS Oui - Version gratuite optimisée (25 analyses / 10 envois max)

import os
import time
import sqlite3
import requests
import schedule
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("API_FOOTBALL_KEY")

API_HOST = "v3.football.api-sports.io"
HEADERS = {
    "x-apisports-key": API_KEY
}

# Championnats prioritaires (meilleure qualité de données)
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

MAX_ANALYSES = 25          # Maximum de matchs analysés en profondeur
MAX_ENVOIS = 10            # Maximum de matchs envoyés sur Telegram
MIN_SCORE = 65

PARIS_TZ = pytz.timezone("Europe/Paris")

# ====================== BASE DE DONNÉES ======================
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

def get_stats():
    conn = sqlite3.connect("predictions.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM predictions WHERE result IS NOT NULL")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM predictions WHERE result = 'Oui'")
    wins = c.fetchone()[0]
    conn.close()
    if total == 0:
        return "Aucune prédiction avec résultat enregistré pour le moment."
    taux = round((wins / total) * 100, 1)
    return f"📊 Stats BTTS\nPrédictions vérifiées : {total}\nRéussites : {wins}\nTaux de réussite : {taux}%"

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

def get_fixtures_today():
    today = datetime.now(PARIS_TZ).strftime("%Y-%m-%d")
    all_fixtures = []

    for league_id in LEAGUES.keys():
        data = api_get("fixtures", {
            "date": today,
            "league": league_id,
            "season": datetime.now().year  # simplifié, à améliorer si besoin
        })
        all_fixtures.extend(data)
        time.sleep(0.4)  # petite pause pour respecter le rate limit

    # On limite à 25 matchs maximum
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
    """
    Retourne (score, raison_principale, point_vigilance)
    """
    try:
        teams = fixture["teams"]
        home = teams["home"]
        away = teams["away"]
        league_id = fixture["league"]["id"]
        season = fixture["league"]["season"]
        fixture_id = fixture["fixture"]["id"]

        # --- 1. Stats équipes ---
        home_stats = get_team_stats(home["id"], league_id, season)
        time.sleep(0.35)
        away_stats = get_team_stats(away["id"], league_id, season)
        time.sleep(0.35)

        if not home_stats or not away_stats:
            return 0, "Données insuffisantes", ""

        # Forme BTTS approximative via buts
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

        # Score partiel
        score = 0
        raisons = []

        # Critère 2 : buts marqués + encaissés (25 pts)
        avg_goals = (home_gf + home_ga + away_gf + away_ga) / 2
        if avg_goals >= 3.0:
            score += 25
            raisons.append("Fort volume de buts")
        elif avg_goals >= 2.5:
            score += 18
            raisons.append("Bon volume de buts")
        elif avg_goals >= 2.1:
            score += 10

        # Critère 1 & 3 : approximation BTTS / clean sheets via moyenne
        # (API free ne donne pas toujours le % BTTS exact facilement)
        btts_proxy = min(home_gf, away_gf) + min(home_ga, away_ga)
        if btts_proxy >= 2.4:
            score += 28
            raisons.append("Les deux équipes marquent souvent")
        elif btts_proxy >= 1.9:
            score += 18
        elif btts_proxy >= 1.5:
            score += 8

        # Critère 4 : H2H
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

        # Critère 7 : contexte simple
        # (on reste léger pour économiser les requêtes)
        score += 5  # base contexte

        # Limite 100
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

    # Tri + limite 10
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

        # Sauvegarde
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
    except Exception as e:
        print(f"Erreur envoi Telegram: {e}")

# ====================== COMMANDES TELEGRAM ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot BTTS Oui actif.\n\n"
        "Commandes :\n"
        "/test → lance une analyse maintenant\n"
        "/status → vérifie que le bot tourne\n"
        "/stats → taux de réussite"
    )

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Analyse en cours... (peut prendre 1-2 minutes)")
    analyse_du_jour()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot en ligne et fonctionnel.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_stats())

# ====================== PLANIFICATION ======================
def run_scheduler():
    schedule.every().day.at("10:00").do(analyse_du_jour)

    while True:
        schedule.run_pending()
        time.sleep(30)

# ====================== MAIN ======================
if __name__ == "__main__":
    init_db()

    # Lancement du bot Telegram
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stats", stats))

    print("Bot démarré. Envoi automatique tous les jours à 10h00 (Paris).")
    
    # On lance le scheduler dans un thread séparé serait mieux,
    # mais pour simplicité débutant on lance d'abord le polling
    # et on utilise un job plus tard si besoin.
    
    # Version simple : on démarre le polling + on planifie via schedule dans un autre processus
    # Pour un débutant, le plus simple est de lancer le bot et d'utiliser un cron ou de garder schedule
    
    import threading
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    app.run_polling()
