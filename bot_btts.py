import csv
import json
import math
import os
import re
import time
import unicodedata
import itertools
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from api_football_btts import format_report as format_api_football_report
from api_football_btts import validate_fixtures as validate_api_football_fixtures

# ============================================================
# BOT FOOTBALL — SPORTAPI — VERSION 6.9.2
#
# Marchés :
# - Domicile +0,5 but (72-85 %)
# - Extérieur +0,5 but (72-85 %)
# - BTTS Oui (72-85 %)
# - Plus de 1,5 but (72-85 %)
# - Équipe : plus de 2,5 tirs cadrés (70 % minimum)
#
# Correction 6.2.1 :
# - Les tirs cadrés sont associés aux identifiants réels des équipes
# - Suppression buteurs et H2H
# - Cache disque des statistiques de tirs cadrés
#
# Corrections 6.2.2 :
# - Vérification des résultats : on traite les dates récentes (les vieilles
#   lignes introuvables ne bloquent plus les nouvelles) et on les expire
# - Probabilité tirs cadrés : vraie loi de Poisson pour "3 ou plus"
# - Un match avec 0 tir cadré n'est plus traité comme une donnée absente
# - Plus de plancher sur la probabilité "extérieur marque"
# - Tirs cadrés : enregistrés dans le journal mais NON publiés (non validés)
#
# Version 6.3.0 :
# - Ticket du jour (4 matchs, cote juste visée ~10) choisi sur la forme des
#   5 derniers matchs + probabilité 50-72 %, tous marchés buts
# - Nouveaux marchés journalisés : victoire, +1,5 but équipe, +2,5 buts
# - Journal : on enregistre dès 50 % (published = 2 pour le ticket)
#
# Version 6.4.0 (revue complète) :
# Corrections :
# 1. API : un 404 (stats absentes d'un petit championnat, slug inconnu) ne
#    compte plus comme un "échec d'affilée". Avant, 8 matchs sans stats de
#    tirs cadrés suffisaient à bloquer TOUTE l'analyse.
# 2. Tirs cadrés : les stats d'un ancien match étaient lues avec les
#    identifiants du match du jour (mauvais adversaire, et mauvaise équipe
#    pour l'extérieur). On garde maintenant les vrais home_id / away_id.
# 3. Tirs cadrés : le lieu (domicile / extérieur) se décalait dès qu'un
#    match sans stats était sauté.
# 4. Ticket : "équipe +0,5 but" n'avait pas de score de forme et ne pouvait
#    donc jamais entrer dans le ticket.
# 5. Ticket : si la cote cible n'est pas atteinte, on prend la combinaison
#    la mieux notée (avant : la plus grosse cote = la plus risquée).
# 6. Telegram : nouvel essai (réseau / 429 / 5xx), plus de plantage si la
#    réponse n'est pas du JSON, un seul envoi par jour (FORCE_SEND=1).
# 7. Journal : un en-tête différent ne fait plus repartir de zéro,
#    écriture atomique (.tmp puis remplacement), migration 6.3 -> 6.4.
# Mineurs : messages d'erreur complets, User-Agent à jour, constante morte
# AWAY_PROB_FLOOR signalée (gardée), bonus domicile appliqué une seule fois, ids des équipes
# enregistrés dans le journal (vérification tirs cadrés).
# Améliorations :
# 8. Filtre de forme (5 derniers matchs des 2 équipes) : pas de victoire
#    d'une équipe sur 2 défaites d'affilée, pas de "+0,5 but" pour une
#    équipe muette 2 fois de suite, l'équipe doit marquer dans >= 3/5 et
#    l'adversaire encaisser dans >= 3/5, BTTS / +1,5 / +2,5 vérifiés aussi.
# 9. Cote minimale par jambe : jamais sous MIN_LEG_ODDS (1,25).
# 10. Bilan de rentabilité : réussite et ROI par marché (cote jouée si la
#     colonne odds_taken est remplie, sinon cote min), calibration par
#     tranche, score de Brier, fichier data/bilan.csv.
# 11. Forme V/N/D affichée sous chaque pronostic.
# 12. Tirs cadrés calculés seulement s'il reste du budget API.
# 14. Enjeu (motivation) d'après le classement déjà lu (standings) :
#     affiché, et un léger bonus/malus dans la note du ticket.
#     Cotes bookmaker et blessures : voir TODO (endpoint non utilisé ici).
#
# Version 6.5.0 (d'après ton bilan 30 jours) :
# - Enjeu / classement : affichés UNIQUEMENT pour un vrai championnat
#   (format "league", pas de groupes, pas de phase/stage, saison en cours,
#   tableau de 8 à 30 équipes, les 2 équipes dedans, au moins 3 matchs
#   joués). Plus de "🔥 titre/Europe" sur les sélections, Ligue des nations,
#   coupes, ni de "2e/1er sur 64". Dans le doute, la ligne n'est pas affichée.
# - "Domicile +0,5" mis en avant (89 % réussi pour 81 % annoncé, ROI +14 %) :
#   petit bonus de classement (MARKET_RANK_BONUS) dans les pronos et le
#   ticket. La probabilité affichée et la cote min ne changent pas, et un
#   pari doit toujours passer les filtres (72-85 %, forme, cote >= 1,25).
#   "Extérieur +0,5" (ROI +6 %) reçoit un plus petit bonus.
# - BTTS n'est plus proposé (1/3, ROI -53 %) ni dans les pronos ni dans le
#   ticket. Il reste calculé et enregistré dans le journal (published = 0)
#   pour suivre sa calibration ; les anciennes lignes BTTS restent lues.
#
# Version 6.6.0 (offre gratuite SportAPI, 3 lancements par jour) :
# - Cache disque du jour (data/cache/, purgé après 3 jours) : équipes,
#   classements, H2H, buteurs, compos... Les 2e et 3e lancements relisent
#   le cache et ne paient presque plus d'appels. Les stats / compos /
#   événements d'un match terminé sont gardés pour toujours (ils ne
#   changent plus). Les matchs du jour sont redemandés avec ETag (304).
# - Quota : lecture de X-RateLimit-Remaining / X-RateLimit-Month-Remaining.
#   Budget du jour = quota du mois restant / jours restants (sinon 230),
#   85 % pour le 1er lancement, le reste pour les suivants. Arrêt propre
#   avant d'épuiser le quota (jamais de 429 "mois").
# - Message Telegram ULTRA léger (format validé) : une ligne par pick,
#   une ligne d'alerte seulement s'il y en a une, ticket en 2 lignes,
#   bilan en 1 ligne. Le détail complet (forme, buts dom/ext, clean
#   sheets, matchs sans marquer, repos, H2H, enjeu...) est écrit dans
#   data/fiches/AAAA-MM-JJ_HHMM.txt.
# - Infos par pick (sans appel en plus) : buts pour / contre à domicile
#   et à l'extérieur (5 derniers), % clean sheets, % matchs sans marquer,
#   jours de repos (+ alerte si <= 3 jours).
# - H2H (5 derniers) pour les picks publiés seulement : alerte si l'équipe
#   a peu marqué contre cet adversaire.
# - Indices (jamais affirmés) : meilleur buteur absent de la compo du
#   dernier match, joueur expulsé au dernier match. Seulement si le budget
#   le permet, sinon rien n'est affiché.
# - Ticket : toujours 4 matchs, cote 10 minimum ; recherche élargie
#   (2 marchés max par match dans la sélection, puis réserve plus large)
#   avant de dire "cote 10 non atteinte".
# - Cotes réelles (optionnel) : seulement si la variable ODDS_API_KEY
#   existe (The Odds API, 500 crédits gratuits / mois). Sinon aucune
#   ligne et aucun appel. Max 7 appels (14 crédits) par jour, cache du jour.
#   Cote affichée seulement pour +1,5 / +2,5 buts et victoire (le marché
#   "équipe +0,5 but" n'existe pas chez eux).
# - Jamais d'info inventée : une ligne sans donnée n'est pas affichée.
#
# Version 6.6.1 (message Telegram, format choisi par Betpulse) :
# - Chaque pick affiche la vraie cote (The Odds API) quand elle existe pour
#   ce marché, sinon la cote min du bot précédée de "~" (estimation).
#   "✅ value" seulement si la vraie cote dépasse la cote min du bot.
# - Ticket : "🎟️ TICKET · cote X", jambes séparées par " / ".
# - Une seule ligne d'alerte courte "⚠️ ..." : buteur absent, expulsé,
#   mauvais H2H, et repos seulement si <= 2 jours (avant : 3, trop souvent).
# - Noms courts : sélections nationales ("Football Union of Russia" ->
#   "Russia", "Bosnia-Herzegovina" -> "Bosnia"...) ; dans les libellés
#   "match" (Dom-Ext), Town / United / City / FC sont retirés.
# - Bilan : "📊 30j : 77% · ROI 0%" (plus de "-0%").
#
# Version 6.6.2 (affichage Telegram, modèle de Betpulse) :
# - Retour à la présentation par heure (⏰ 14h) et par match :
#   "N. Dom — Ext · HH:MM", "🏆 Ligue", une ligne par marché
#   "• Marché : 83% · 💰 min 1,25" (ou "💰 cote 1,40 ✅ value" si vraie
#   cote), "⚽ Buts projetés", et UNE ligne "⚠️ ..." seulement s'il y a
#   une vraie alerte. Tous les marchés retenus du match sont affichés.
# - Plus de forme récente / enjeu / classement dans Telegram (gardés dans
#   la fiche data/fiches/).
# - Ticket : même présentation par jambe ("cote juste" ou vraie cote),
#   puis la cote totale ou "cote 10 non atteinte (meilleur X)".
# - Découpage Telegram par blocs de match, sous la limite de 4096.
#
# Version 6.6.3 (ticket : grands championnats seulement) :
# - Les jambes du ticket ne viennent plus que des ligues de TICKET_LEAGUES
#   (grands championnats + compétitions des sélections A), celles que
#   Stake propose. Les petites ligues (Highland / Lowland, National
#   League N / S...) sont exclues du ticket. Les pronos du jour ne
#   changent pas (toutes les ligues). Tout le reste est identique.
# - Liste modifiable ci-dessous (TICKET_LEAGUES, TICKET_LEAGUE_IDS).
#
# Version 6.7.0 (backtest des marchés : 5 333 matchs, 70 jours) :
# - Nouveaux pronos "cote plus haute" : Domicile +1,5 but et Plus de 2,5 buts,
#   ajoutés aux pronos du jour (mêmes filtres de forme). Ils sont publiés
#   en plus des marchés habituels, sans les remplacer.
# - Correction de calibration (stable sur les 2 moitiés du backtest) :
#   Domicile +1,5 but +5 points, Extérieur +1,5 but -4 points. Elle touche
#   aussi les cotes justes du ticket et les probabilités du journal.
# - Les lignes du journal de ces marchés ont published = 1 comme les autres :
#   on les sépare par la colonne market.
#
# Version 6.8.0 (corrections après le run du 20/11) :
# 1. Pronos aussi limités aux ligues de TICKET_LEAGUES (grands championnats
#    + sélections A dispo sur Stake), y compris les pronos "cote plus
#    haute". Réglage : PRONOS_LIGUES_AUTORISEES.
# 2. Ticket construit sur les VRAIES cotes quand The Odds API les donne :
#    une jambe sous 1,20 de vraie cote est refusée (Bayern 1,07 ne passe
#    plus), la cote totale est celle des bookmakers ; la cote juste du bot
#    ne sert qu'en secours et est marquée "(estim.)". Toujours le meilleur
#    combiné cohérent, même sous 10.
# 4. Ticket : seulement les matchs du jour entre l'heure du lancement et
#    23:59 (heure de Paris), jamais avant 08:00 : plus de match de 04:00
#    si GitHub lance le bot en retard dans la nuit.
# 5. Plus de vraies cotes : Belgique, Brésil, Argentine, Conference League,
#    sélections (Ligue des nations, qualifs Coupe du monde / Euro, CAN,
#    Copa América) ajoutées ; les ligues du ticket passent en premier dans
#    les 7 appels du jour (offre gratuite respectée).
# 6. Bilan 30 j : seulement les marchés publiés aujourd'hui (plus les anciens
#    buteurs / BTTS / tirs cadrés), ROI à la vraie cote quand elle est
#    connue (cote jouée odds_taken, sinon cote marché odds_market enregistrée
#    automatiquement), sinon à la cote min — et c'est écrit.
#
# Version 6.8.1 :
# - Marchés Dom +1,5 / Ext +1,5 supprimés (pronos, ticket, "cote plus
#   haute", bilan) : The Odds API gratuite ne les propose pas. Ils restent
#   seulement calculés dans le journal (published = 0) pour la calibration.
# - Mode nuit : lancé entre 00:00 et 06:00 (Paris), le bot ne prend que les
#   matchs de maintenant à 08:00 (MLS, Amérique du Sud...), mêmes ligues et
#   mêmes règles de vraies cotes. Titre "PRONOSTICS DE LA NUIT" et "TICKET
#   DE NUIT". Les lancements de jour gardent maintenant -> 23:59.
# - Budget The Odds API avec 3 lancements : 5 appels pour les lancements de
#   jour + 2 pour la nuit = 7 appels (14 crédits) par jour, environ 434 par
#   mois (< 500 gratuits). Sans lancement de nuit : NUIT_ACTIVE = False et
#   les lancements de jour reprennent les 7 appels.
#
# Version 6.9.0 :
# - 🎉 MÉGA FUN : cote totale >= 50, 6 matchs max (1 sélection par match),
#   1,65 min par sélection (vraie cote si dispo, sinon cote juste
#   "(estim.)"). Sélections soutenues par la forme (forme >= 50 %), proba
#   modèle >= 38 % (jamais de grosse cote au hasard) : le bot garde la
#   combinaison la PLUS PROBABLE qui atteint 50. Sinon : meilleur combiné
#   cohérent, affiché "Cote 50 non atteinte".
# - 🛡️ ULTRA SAFE : seulement équipe +0,5 but, 2 sélections max, les deux
#   équipes les plus sûres de marquer avec cote estimée >= 1,20. Cote
#   estimée (pas ce marché sur The Odds API) : à vérifier sur Stake.
# - Les 2 tickets : ligues Stake, même fenêtre que le ticket du jour,
#   enregistrés dans le journal (colonne tickets : jour / mega / safe) et
#   bilan 30 j par ticket ("🎟️ Tickets 30j").
# - Fatigue : équipe qui a joué il y a 3 jours ou moins -> -5 % de buts
#   attendus pour elle et +3 % pour l'adversaire ; Coupe d'Europe dans les
#   4 derniers jours -> -2 % en plus (sans aucun appel API en plus).
# - Absences (indices) : SportAPI n'a PAS de liste blessés / suspendus. Le
#   bot regarde la compo du dernier match : meilleur buteur absent -> -6 %
#   de buts attendus, autre buteur (3 buts et +) absent -> -3 %, expulsé au
#   dernier match -> -3 % ; total plafonné à -12 %. Seulement ligues Stake,
#   40 appels max par lancement (compos / événements gardés en cache).
# - Fatigue + absences modifient les probabilités des pronos et des 3 tickets.
# - Un seul lancement auto à 10h ; budget The Odds API compté par jour de
#   Paris (7 appels = 14 crédits, relances et lancement de nuit compris) :
#   434 crédits / mois max. Pas d'appel de cotes pour les marchés que l'API
#   ne couvre pas (équipe +0,5).
#
# Version 6.9.1 :
# - Journal : prob_base = probabilité AVANT fatigue / absences (probability
#   reste la probabilité finale publiée). On pourra ainsi mesurer, sur le
#   journal, si ces ajustements améliorent ou dégradent le modèle.
# - Absences désactivées (ABSENCES_ACTIF = False) : les indices tirés de la
#   compo du dernier match ne sont pas validés (noms mal reconnus, joueurs
#   ménagés). Réactivables en remettant True.
#
# Version 6.9.2 (mode sans limite) :
# - MODE_SANS_LIMITE = True : plus de plafond de 15 matchs, plus de budget
#   quotidien auto-imposé (le bot analyse tous les matchs du jour), temps
#   d'exécution porté à 18 min. Le bot s'arrête seulement quand le quota
#   RÉEL de l'API est presque épuisé (lu dans les en-têtes X-RateLimit),
#   donc jamais de blocage brutal : au pire "Analyse partielle".
# - Sur l'offre gratuite (14 000 requêtes / mois), ce mode épuise le quota
#   plus vite. Remettre MODE_SANS_LIMITE = False pour revenir au budget
#   réparti sur le mois.
# - Tirs cadrés non calculés (SOT_ACTIF = False) : abandonnés, et ils
#   coûtaient 2 requêtes par match.
# - Plus de plafond de 12 matchs par championnat (MAX_PER_LEAGUE).
# - Les ligues autorisées (PRONOS_LIGUES_AUTORISEES) ne changent pas.
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "0").strip().lower() in {"1", "true", "yes"}

BASE_URL = "https://sportapi.ai/api"
PARIS = ZoneInfo("Europe/Paris")

MIN_PROB = 0.72
MAX_PROB = 0.85

SOT_LINE = 2.5
SOT_MIN_PROB = 0.70
SOT_MAX_PROB = 0.92
SOT_HISTORY_N = 5
SOT_CACHE_FILE = "data/sot_stats_cache.json"

ODDS_MARGIN = 1.03
LOG_MIN_PROB = 0.50

MODE_SANS_LIMITE = True   # 6.9.2 : voir l'en-tête (quota réel seulement)
SOT_ACTIF = False         # 6.9.2 : tirs cadrés abandonnés (2 requêtes / match)
MAX_MATCHES = 500 if MODE_SANS_LIMITE else 15

# 6.9.9 : sortie simplifiée — pronostics normaux + Fun journée uniquement.
TICKET_ACTIF = False
TICKET_LEGS = 4
TICKET_MIN_PROB = 0.55       # 4 jambes permettent réellement de viser une cote ~10
TICKET_MAX_PROB = 0.75
TICKET_TARGET_ODDS = 10.0
TICKET_FORM_N = 5
TICKET_POOL = 24
TICKET_FORM_WEIGHT = 0.40
MAX_MARKETS = 3

MAX_PER_LEAGUE = 999 if MODE_SANS_LIMITE else 12   # 6.9.2
PRIORITY_LEAGUE_IDS = set()

EXCLUDE_YOUTH = True
YOUTH_PATTERN = re.compile(
    r"\bU[- ]?(1[5-9]|2[0-3])\b|\byouth\b|\bjuniors?\b"
    r"|\bprimavera\b|\breserves?\b|\bamateurs?\b|\bII$",
    re.IGNORECASE,
)

PRIOR_K = 4.0
PRIORS = {
    "gf": 1.35,
    "ga": 1.35,
    "btts": 0.52,
    "over15": 0.75,
}

HISTORY_N = 10
MIN_HISTORY = 6
VENUE_N = 6
MAX_HISTORY_DAYS = 240
MAX_STALE_DAYS = 60
DECAY = 0.95

HOME_ADJUST = 0.02
AWAY_ADJUST = -0.02
HOME_PROB_CAP = 0.88
AWAY_PROB_FLOOR = 0.60  # plus utilisé depuis 6.2.2, gardé pour compatibilité

# ---- Filtre de forme (5 derniers matchs) : la forme prime sur le modèle ----
FORM_MIN_SCORED = 3      # l'équipe a marqué dans au moins 3 des 5 derniers
FORM_MIN_CONCEDED = 3    # l'adversaire a encaissé dans au moins 3 des 5
LOSING_STREAK_MAX = 2    # 2 défaites d'affilée -> pas de victoire sèche
DRY_STREAK_MAX = 2       # 2 matchs sans marquer -> pas de "+0,5 but"
FORM_MIN_RATE = 0.40     # +1,5 / +2,5 : au moins 40 % sur les 2 équipes
BTTS_FORM_MIN_RATE = 0.60  # BTTS exige au moins 60 % récent en moyenne
BTTS_TICKET_MIN_PROB = 0.60  # pas de BTTS faible dans les tickets fun

# ---- Cote minimale par jambe : refus sous 1,20, marge de sécurité 1,25 ----
MIN_LEG_ODDS = 1.25

# ---- Tirs cadrés (non publiés) : seulement s'il reste du budget API ----
SOT_BUDGET_SHARE = 0.60

# ---- Enjeu (classement) ----
TOP_ZONE = 4             # 4 premiers : titre / Europe
RELEGATION_ZONE = 3      # 3 derniers : maintien
MOTIVATION_BONUS = 0.03  # bonus / malus sur la note du ticket

# 6.5.0 : enjeu / classement seulement pour un vrai championnat
STANDINGS_MIN_SIZE = 8
STANDINGS_MAX_SIZE = 30   # "sur 64" = tableau de coupe / agrégé -> ignoré
STANDINGS_MIN_PLAYED = 3
NON_LEAGUE_PATTERN = re.compile(
    r"cup|coupe|copa|coppa|pokal|beker|ta[cç]a|trophy|troph[ée]e|shield"
    r"|super ?cup|supercoupe|nations|qualif|friendl|amica|world|mondial"
    r"|euro|africa|afcon|\bcan\b|asian|gold cup|concacaf|conmebol|uefa|fifa"
    r"|\bcaf\b|\bafc\b|\bofc\b|champions league|\bchampions\b|europa|conference league|libertadores"
    r"|sudamericana|play-?offs?|barrage|group|groupe|international",
    re.IGNORECASE,
)

# 6.5.0 : bonus de CLASSEMENT (pas de probabilité) selon ton bilan 30 jours
MARKET_RANK_BONUS = {"home": 0.03, "away": 0.01}
# 6.5.0 : marchés calculés et journalisés mais jamais proposés
NOT_PROPOSED = {"home15", "away15"}
# 6.8.1 : marchés sans vraie cote sur The Odds API gratuite -> plus proposés
REMOVED_MARKETS = {"home15", "away15"}

# 6.7.0 : marchés "cote plus haute" (zone de probabilité publiée, après correction)
HIGH_ODDS_PICKS = {"over25": (0.58, 0.75)}   # 6.8.1 : Dom +1,5 supprimé
HIGH_ODDS_MAX_PER_MATCH = 2    # au plus 2 de ces marchés par match
HIGH_ODDS_EXTRA_MATCHES = 6    # matchs ajoutés qui n'ont QUE ce type de prono
EXTRA_ADJUST = {"home15": 0.05, "away15": -0.04}   # d'après le backtest

# ---- 6.6.0 : cache disque + quota SportAPI (offre gratuite) ----
DISK_CACHE_DIR = "data/cache"
CACHE_KEEP_DAYS = 3
PERMANENT_ENDPOINT = re.compile(r"^fixtures/\d+/(stats|lineups|events)$")
DAILY_BUDGET_DEFAULT = 230   # 7 000 / mois ÷ 30 si l'API ne donne pas son quota
FIRST_RUN_SHARE = 0.85       # le 1er lancement garde 15 % pour les suivants
QUOTA_RESERVE = 15           # on s'arrête avant d'épuiser le quota
ENRICH_RESERVE = 40          # appels gardés pour H2H / buteurs / cartons

# ---- 6.6.0 : infos par pick ----
SHOWN_MARKETS = 10           # 6.6.2 : tous les marchés retenus du match
H2H_N = 5
H2H_ALERT_MAX = 2            # l'équipe a marqué <= 2 fois sur 5 H2H -> alerte
REST_ALERT_DAYS = 2
TOPSCORER_MIN_GOALS = 3
LINEUP_MAX_AGE_DAYS = 14
FICHES_DIR = "data/fiches"
TICKET_POOL_WIDE = 40
TICKET_PER_MATCH = 2

# ---- 6.6.3 : ligues autorisées dans le TICKET (pas dans les pronos) ----
# Chaque ligne : (motif sur le nom de la ligue, pays ou None, motif exclu).
# Le nom est comparé en minuscules sans accents ("Süper Lig" -> "super lig").
# Si un pays est donné, la ligue doit être de ce pays (champ league_zone /
# league_geo de SportAPI, ou pays/adjectif dans le nom) : "Premier League"
# seul ne suffit donc pas (Nigerian Premier League refusée).
# Pour ajouter une ligue : ajoute une ligne. Pour forcer un id SportAPI :
# mets-le dans TICKET_LEAGUE_IDS.
TICKET_LEAGUES = [
    # --- championnats ---
    (r"\bpremier league\b", "England", r"\b(2|u ?\d\d|women|cup)\b|national"),
    (r"\bla ?liga\b|primera division", "Spain", r"\b(2|segunda|hypermotion|f)\b"),
    (r"\bserie a\b", "Italy", r"women|femminile"),
    (r"\bbundesliga\b", "Germany", r"\b2\b|2\. |women|frauen"),
    (r"\bligue 1\b", "France", r"women|feminine"),
    (r"\bchampionship\b", "England", r"national|women"),
    (r"\bleague (one|two|1|2)\b", "England", r"national|women|premier"),
    (r"\beredivisie\b", "Netherlands", r"women|vrouwen"),
    (r"primeira liga|liga portugal", "Portugal", r"\b2\b|women"),
    (r"pro league|jupiler|first division a", "Belgium", r"women|challenger"),
    (r"\bpremiership\b", "Scotland", r"women|championship"),
    (r"super lig", "Turkey", r"\b1\b|women"),
    (r"pro league|professional league|roshn|saudi arabian league", "Saudi Arabia", r"\b(1|first)\b|women"),
    (r"major league soccer|\bmls\b", "USA", r"next pro|women"),
    (r"\bserie a\b|brasileirao", "Brazil", r"women|feminino"),
    (r"liga profesional|primera division|superliga", "Argentina", r"nacional|women|reserv"),
    # --- coupes d'Europe des clubs ---
    (r"^(uefa )?(champions|europa|europa conference|conference) league", None, r"women|youth"),
    # --- sélections A ---
    (r"world cup", None, r"club|futsal|beach"),
    (r"^uefa nations league", None, None),
    (r"concacaf nations league.*\bleague a\b", None, None),
    (r"^(uefa )?euro(pean championship)?\b", None, r"futsal"),
    (r"africa cup of nations|\bafcon\b", None, None),
    (r"copa america", None, None),
    (r"^internationals?$|international friendl|^friendlies$|friendly international", None, r"club"),
]
TICKET_LEAGUE_IDS = set()     # ids SportAPI toujours acceptés (ex. {39, 140})
PRONOS_LIGUES_AUTORISEES = True   # 6.8.0 : True = pronos aussi limités à cette liste
TICKET_MIN_LEG_ODDS = MIN_LEG_ODDS  # 6.9.8 : aucune vraie cote sous 1,25 dans un ticket
TICKET_EARLIEST_HOUR = 8          # 6.8.0 : pas de match avant 08:00 dans le ticket
# 6.8.1 : mode nuit (lancement entre 00:00 et NIGHT_RUN_END_HOUR, Paris)
NUIT_ACTIVE = False               # False = pas de lancement de nuit programmé (relance manuelle possible)
NIGHT_RUN_END_HOUR = 6            # lancé avant 06:00 = lancement de nuit
NIGHT_WINDOW_END_HOUR = 8         # la nuit : matchs jusqu'à 08:00
NIGHT_API_CALLS = 80              # appels SportAPI max pour un lancement de nuit
NIGHT_RUN = False                 # fixé par main()
RUN_START_HOUR = 8                # 6.9.3 : pas d'envoi auto avant 08:00 Paris
RUN_END_HOUR = 22                 # 6.9.3 : pas d'envoi auto à partir de 22:00 Paris
MAX_RUNS_PER_DAY = 2              # 6.9.3 : 2 créneaux max par jour (midi + soir)
KICKOFF_MARGIN_MINUTES = 10       # 6.9.4 : ignore un match déjà commencé ou imminent
TICKET_SLOT_HOURS = 3             # 6.9.5 : ticket du jour / méga fun dans une fenêtre de 3 h

# 6.9.3 : 🎉 MÉGA FUN (solide : 4-5 sélections >= 50 %, sinon meilleur court)
MEGA_ACTIF = False
MEGA_TARGET_ODDS = 50.0
MEGA_MIN_LEGS = 4
MEGA_MAX_LEGS = 5
MEGA_MIN_LEG_ODDS = 2.00          # cote minimale pour construire une cote proche de 50
MEGA_MIN_FALLBACK_ODDS = 1.80     # cote min réelle en repli court solide
MEGA_MIN_FAIR_ODDS = 1.75         # cote juste min si pas de cote bookmaker
MEGA_MAX_LEG_ODDS = 3.00          # plafond : pas de grosse cote au hasard
MEGA_MIN_PROB = 0.55              # proba modèle minimum par sélection
MEGA_MIN_FORM = 0.60              # forme stricte sur les 5 derniers
MEGA_POOL = 24
# 6.9.5 : 🛡️ ULTRA SAFE désactivé (plus construit, plus affiché, plus d'appels absences)
SAFE_ACTIF = False
SAFE_MAX_LEGS = 2
SAFE_MIN_LEG_ODDS = MIN_LEG_ODDS
SAFE_MIN_PROB = 0.80
SAFE_FORM_MIN_SCORED = 4
SAFE_FORM_MIN_CONCEDED = 3
SAFE_ABSENCES_ACTIF = False
SAFE_ABSENCE_MAX_CALLS = 20
SAFE_ABSENCE_CANDIDATES = 6
SAFE_MARKETS = ("home", "away")
# 6.9.6 : 🎯 FUN JOURNÉE mixte (jusqu'à 7 solides + 3 boosts, toute la journée)
DAYFUN_ACTIF = True
DAYFUN_MAX_LEGS = 10
DAYFUN_SOLID_LEGS = 8             # sélections solides (>= 70 %, équipe +0,5 / +1,5)
DAYFUN_BOOST_LEGS = 2             # au plus 2 sélections valeur entre 61 % et 70 %
DAYFUN_MIN_LEGS = 6               # en dessous : ticket plus court, ou rien si < DAYFUN_HARD_MIN
DAYFUN_HARD_MIN = 4               # moins de 4 sélections : pas de ticket, juste une note
DAYFUN_MIN_PROB = 0.70
DAYFUN_MIN_LEG_ODDS = MIN_LEG_ODDS
DAYFUN_MARKETS = ("home", "away", "over15")
DAYFUN_EXTRA_MARKETS = ("btts",)
DAYFUN_BOOST_MIN_PROB = 0.61
DAYFUN_BOOST_MIN_ODDS = 1.43         # cote juste minimale à 70 % ; 1,64 à 61 %
DAYFUN_BOOST_MAX_ODDS = 2.20
DAYFUN_BOOST_MARKETS = ("home", "over15")
DAYFUN_TARGET_ODDS = 30.0         # cote cible minimale du combiné journée
TICKET_KIND_LABELS = {
    "jour": "Jour", "mega": "Méga fun", "dayfun": "Fun journée", "safe": "Ultra safe",
}
TICKET_TAGS_SINCE = (6, 9)        # colonne tickets remplie depuis 6.9.0
# 6.9.0 : marchés que The Odds API gratuite couvre (sinon : aucun appel)
REAL_ODDS_MARKETS = {"over15", "over25", "win_home", "win_away"}
# 6.9.7 : over25 reste suivi en pronostic simple, mais est retire de tous les
# tickets apres un ROI negatif sur les cotes reelles disponibles.
TICKET_EXCLUDED_MARKETS = {"over25"}

def ticket_market_allowed(candidate):
    """Marchés autorisés dans les tickets, distincts des pronostics simples."""
    return candidate.get("market") not in TICKET_EXCLUDED_MARKETS

# 6.9.0 : fatigue (calendrier déjà chargé, 0 appel en plus)
FATIGUE_ACTIF = True
FATIGUE_REST_DAYS = 3             # joué il y a 3 jours ou moins
FATIGUE_ATTACK = 0.05             # -5 % de buts attendus pour l'équipe
FATIGUE_LEAK = 0.03               # +3 % pour l'adversaire
FATIGUE_EURO_DAYS = 4
FATIGUE_EURO_EXTRA = 0.02         # Coupe d'Europe en semaine : -2 % en plus
EURO_CUP_PATTERN = re.compile(r"champions league|europa league|conference league", re.IGNORECASE)
# 6.9.0 : absences (indices : compo / cartons du dernier match)
ABSENCES_ACTIF = False
ABSENCE_SCORER = 0.06             # meilleur buteur absent
ABSENCE_OTHER = 0.03              # autre buteur (3 buts et +) absent
ABSENCE_RED = 0.03                # expulsé au dernier match
ABSENCE_CAP = 0.12
ABSENCE_MAX_CALLS = 40            # appels SportAPI max par lancement
ABSENCE_KEY_PLAYERS = 3
ABSENCE_STATE = {"calls": 0, "checked": 0, "lineups": 0}
# 6.8.0 : marchés publiés aujourd'hui (le bilan 30 j ne compte qu'eux)
CURRENT_PUBLISHED_MARKETS = {"home", "away", "over15", "over25"}
TICKET_LEAGUE_EXCLUDE = (     # jamais dans le ticket, quelle que soit la ligne
    r"women|feminin|\bu ?\d\d\b|youth|junior|reserve|amateur|futsal"
    r"|beach|esoccer|\bii\b|highland|lowland|national league n"
    r"|national league s|national league north|national league south"
    r"|cymru premier|national league"
)
COUNTRY_WORDS = {
    "England": ("england", "english"),
    "Spain": ("spain", "spanish"),
    "Italy": ("italy", "italian"),
    "Germany": ("germany", "german"),
    "France": ("france", "french"),
    "Netherlands": ("netherlands", "dutch", "holland"),
    "Portugal": ("portugal", "portuguese"),
    "Belgium": ("belgium", "belgian"),
    "Scotland": ("scotland", "scottish"),
    "Turkey": ("turkey", "turkiye", "turkish"),
    "Saudi Arabia": ("saudi",),
    "USA": ("usa", "us", "united states", "american"),
    "Brazil": ("brazil", "brasil", "brazilian"),
    "Argentina": ("argentina", "argentine", "argentinian"),
}

# ---- 6.6.0 : cotes réelles (optionnel, The Odds API) ----
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "eu"
ODDS_MARKETS = "h2h,totals"          # 2 crédits par appel
ODDS_MAX_CALLS_PER_DAY = 7           # 14 crédits / jour ≈ 434 / mois (< 500)
ODDS_NIGHT_CALLS = 2                 # 6.8.1 : part du lancement de nuit (si NUIT_ACTIVE)
ODDS_MIN_REMAINING = 30
# (motif du nom de ligue SportAPI, pays SportAPI ou None, clé The Odds API)
# Chaque clé est revérifiée à l'exécution dans /v4/sports (appel gratuit).
ODDS_SPORT_KEYS = [
    ("2 bundesliga", "Germany", "soccer_germany_bundesliga2"),
    ("bundesliga", "Germany", "soccer_germany_bundesliga"),
    ("premier league", "England", "soccer_epl"),
    ("championship", "England", "soccer_efl_champ"),
    ("league one", "England", "soccer_england_league1"),
    ("league two", "England", "soccer_england_league2"),
    ("ligue 1", "France", "soccer_france_ligue_one"),
    ("ligue 2", "France", "soccer_france_ligue_two"),
    ("serie a", "Italy", "soccer_italy_serie_a"),
    ("serie b", "Italy", "soccer_italy_serie_b"),
    ("segunda", "Spain", "soccer_spain_segunda_division"),
    ("la liga", "Spain", "soccer_spain_la_liga"),
    ("eredivisie", "Netherlands", "soccer_netherlands_eredivisie"),
    ("primeira", "Portugal", "soccer_portugal_primeira_liga"),
    ("premiership", "Scotland", "soccer_spl"),
    ("super lig", "Turkey", "soccer_turkey_super_league"),
    (r"major league soccer|\bmls\b", "USA", "soccer_usa_mls"),
    ("champions league", None, "soccer_uefa_champs_league"),
    ("europa league", None, "soccer_uefa_europa_league"),
    # 6.8.0 : ligues du ticket qui manquaient (clés revérifiées dans /v4/sports)
    ("conference league", None, "soccer_uefa_europa_conference_league"),
    ("pro league|jupiler|first division a", "Belgium", "soccer_belgium_first_div"),
    ("pro league|professional league|roshn|saudi arabian league", "Saudi Arabia", "soccer_saudi_arabia_pro_league"),
    ("serie a|brasileirao", "Brazil", "soccer_brazil_campeonato"),
    ("liga profesional|primera division|superliga", "Argentina", "soccer_argentina_primera_division"),
    ("^uefa nations league", None, "soccer_uefa_nations_league"),
    ("world cup qualif.*(uefa|europe)", None, "soccer_fifa_world_cup_qualifiers_europe"),
    ("world cup qualif.*(conmebol|south america)", None, "soccer_fifa_world_cup_qualifiers_south_america"),
    ("^(fifa )?world cup$", None, "soccer_fifa_world_cup"),
    ("^(uefa )?euro(pean championship)?.*qualif", None, "soccer_uefa_euro_qualification"),
    ("^(uefa )?euro(pean championship)?( \\d{4})?$", None, "soccer_uefa_european_championship"),
    ("africa cup of nations|afcon", None, "soccer_africa_cup_of_nations"),
    ("copa america", None, "soccer_conmebol_copa_america"),
]

# ---- Bilan / envoi ----
STATS_DAYS = 30
SUMMARY_FILE = "data/bilan.csv"
SENT_FILE = "data/dernier_envoi.txt"
ONE_SEND_PER_SLOT = True  # True = 1 envoi par créneau (midi / soir) et tickets figés dans la journée

# TODO (non codé, endpoint inconnu dans ce code) :
# - cotes bookmaker : publier seulement si proba modèle > 1 / cote
# - blessures / compositions : baisser l'espérance de buts sans le buteur

MAX_API_CALLS = 600 if MODE_SANS_LIMITE else 350
MAX_SETTLE_DATES = 8
PENDING_EXPIRY_DAYS = 7
FINISHED_STATUSES = {"FT", "AET", "PEN", "PENS", "AP", "AFTER_PEN", "AFTER_ET", "FINISHED"}
MODEL_VERSION = "6.9.6"
MIN_INTERVAL = 0.5
MAX_RUNTIME_SECONDS = 1080 if MODE_SANS_LIMITE else 600
TIMEOUT = 20
MAX_RETRIES = 2
RETRY_PAUSE = 60
MAX_CONSECUTIVE_FAILS = 8

PREDICTIONS_FILE = "data/predictions.csv"
FIELDS_V63 = [
    "date", "fixture_id", "match", "league", "kickoff",
    "market", "player", "probability", "prob_base",
    "published", "model_version", "result",
]
# 6.4.0 : home_id / away_id (vérification tirs cadrés) et odds_taken
# (cote réellement jouée, à remplir à la main pour le vrai ROI, ex : 1,45)
FIELDS = FIELDS_V63 + ["home_id", "away_id", "odds_taken"]
# 6.8.0 : vraie cote marché (The Odds API) enregistrée automatiquement
FIELDS = FIELDS + ["odds_market"]
# 6.9.0 : tickets où figure la ligne (jour / mega / safe, séparés par ;)
FIELDS = FIELDS + ["tickets"]

SESSION = requests.Session()
SESSION.headers.update({
    "X-Api-Key": API_KEY,
    "User-Agent": "football-history-bot/6.9.0",
})

CACHE = {}
TEAM_CACHE = {}
STANDINGS_CACHE = {}
STANDINGS_META = {}

# 6.6.0 : cache disque / quota
DISK = {"day": None, "data": {}, "etag": {}, "dirty": False}
PERM = {"loaded": False, "data": {}, "dirty": False}
USAGE = {}
QUOTA = {}
RUN_CAP = None
CACHE_HITS = 0
ABSENT = {"__absent__": True}
SOT_CACHE = {}

ERRORS = []
REASONS = Counter()
CALLS = 0
LAST_CALL = None
BLOCKED = False
PARTIAL = False
FAILS = 0
STARTED = time.monotonic()


# ---------------- SECURITE / LOGS ----------------

def safe(value):
    text = str(value)
    for secret in (TOKEN, API_KEY, CHAT_ID, ODDS_API_KEY):
        if secret:
            text = text.replace(secret, "[MASQUE]")
    return text


def error(message):
    message = safe(message)[:350]
    print("DIAGNOSTIC :", message, flush=True)
    if message not in ERRORS:
        ERRORS.append(message)


def budget_available(reserve=0):
    cap = MAX_API_CALLS if RUN_CAP is None else min(MAX_API_CALLS, RUN_CAP)
    return (
        not BLOCKED
        and CALLS < cap - reserve
        and time.monotonic() - STARTED < MAX_RUNTIME_SECONDS
    )


# ---------------- TELEGRAM ----------------

TELEGRAM_CHUNK = 4000  # limite Telegram 4096, avec marge


def send_message(text):
    if DRY_RUN:
        print("DRY_RUN : aucun message Telegram envoyé", flush=True)
        print(text, flush=True)
        return
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")

    # 6.6.2 : on coupe entre deux blocs (ligne vide = fin d'un match),
    # sous la limite Telegram (4096) ; ligne à ligne si un bloc est trop long.
    chunks = []
    current = ""

    def push_piece(piece):
        nonlocal current
        if len(current) + len(piece) <= TELEGRAM_CHUNK:
            current += piece
            return
        if current.strip():
            chunks.append(current)
        current = ""
        if len(piece) <= TELEGRAM_CHUNK:
            current = piece
            return
        for line in piece.splitlines(keepends=True):
            while len(line) > TELEGRAM_CHUNK:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:TELEGRAM_CHUNK])
                line = line[TELEGRAM_CHUNK:]
            if len(current) + len(line) > TELEGRAM_CHUNK:
                chunks.append(current)
                current = ""
            current += line

    pieces = text.split("\n\n")
    for position, piece in enumerate(pieces):
        push_piece(piece + ("\n\n" if position < len(pieces) - 1 else ""))

    if current.strip():
        chunks.append(current)
    chunks = [c.strip("\n") or c for c in chunks]

    for chunk in chunks:
        send_chunk(chunk)


def send_chunk(chunk, attempts=3):
    """Envoie un morceau avec nouvel essai (réseau, 429, 5xx)."""
    last = "erreur"
    for attempt in range(attempts):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": chunk},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            last = "reseau : " + type(exc).__name__
            time.sleep(3 * (attempt + 1))
            continue

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if response.ok and payload.get("ok"):
            return

        last = (
            f"HTTP {response.status_code} : "
            + safe(payload.get("description", "erreur"))
        )
        if response.status_code == 429 or response.status_code >= 500:
            params = payload.get("parameters")
            pause = number(params.get("retry_after")) if isinstance(params, dict) else None
            time.sleep(min(pause or 5 * (attempt + 1), 60))
            continue
        break  # 400 / 401 / 403 : inutile de réessayer

    raise RuntimeError("Telegram " + last)


def current_slot(now=None):
    """Créneau d'envoi (heure de Paris) : midi (08-15h59), soir (16-21h59), nuit."""
    now = (now or datetime.now(PARIS)).astimezone(PARIS)
    if RUN_START_HOUR <= now.hour < 16:
        return "midi"
    if 16 <= now.hour < RUN_END_HOUR:
        return "soir"
    return "nuit"


def _read_sent_slots(today):
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as file:
            raw = file.read().strip()
    except OSError:
        return set()
    day_iso = today.isoformat()
    if ":" not in raw:
        return set()
    day_part, slots_part = raw.split(":", 1)
    if day_part.strip() != day_iso:
        return set()
    return {s.strip() for s in slots_part.split(",") if s.strip()}


def already_sent(today, slot=None):
    """Évite le double envoi sur un même créneau (midi / soir)."""
    if not ONE_SEND_PER_SLOT or os.getenv("FORCE_SEND", "").strip() == "1":
        return False
    # 6.9.7 : un lancement manuel (Run workflow) envoie toujours
    if os.getenv("GITHUB_EVENT_NAME", "").strip() == "workflow_dispatch":
        return False
    slot = slot or current_slot()
    return slot in _read_sent_slots(today)


def mark_sent(today, slot=None):
    try:
        os.makedirs(os.path.dirname(SENT_FILE), exist_ok=True)
        slot = slot or current_slot()
        slots = _read_sent_slots(today)
        slots.add(slot)
        order = {"midi": 0, "soir": 1, "nuit": 2}
        ordered = sorted(slots, key=lambda s: (order.get(s, 9), s))
        with open(SENT_FILE, "w", encoding="utf-8") as file:
            file.write(f"{today.isoformat()}:{','.join(ordered)}")
    except OSError as exc:
        error(f"Marqueur d'envoi impossible : {type(exc).__name__} {exc}")


# ---------------- API ----------------

def failed(endpoint):
    global FAILS, BLOCKED
    CACHE[endpoint] = None
    FAILS += 1
    if FAILS >= MAX_CONSECUTIVE_FAILS:
        BLOCKED = True
        error("Trop d'echecs d'affilee : analyse arretee.")
    return None


def retry_pause(response):
    try:
        return max(RETRY_PAUSE, int(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return RETRY_PAUSE


def api_get(endpoint, force_refresh=False):
    global CALLS, LAST_CALL, BLOCKED, FAILS, CACHE_HITS

    if not force_refresh and endpoint in CACHE:
        return CACHE[endpoint]

    # 6.6.0 : cache disque (jour / permanent)
    found, cached, etag = (False, None, None) if force_refresh else disk_lookup(endpoint)
    if found and not is_fresh(endpoint):
        CACHE_HITS += 1
        CACHE[endpoint] = cached
        return cached

    for attempt in range(MAX_RETRIES + 1):
        if not budget_available():
            if found:  # copie un peu ancienne plutôt que rien
                CACHE[endpoint] = cached
                return cached
            return None

        if LAST_CALL is not None:
            wait = MIN_INTERVAL - (time.monotonic() - LAST_CALL)
            if wait > 0:
                time.sleep(wait)

        LAST_CALL = time.monotonic()
        CALLS += 1
        print(f"API {CALLS}/{MAX_API_CALLS} : {endpoint}", flush=True)

        try:
            if found and etag:
                response = SESSION.get(
                    f"{BASE_URL}/{endpoint}",
                    timeout=TIMEOUT,
                    headers={"If-None-Match": etag},
                )
            else:
                response = SESSION.get(
                    f"{BASE_URL}/{endpoint}",
                    timeout=TIMEOUT,
                )
        except requests.RequestException:
            error(f"{endpoint} : erreur reseau.")
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            return failed(endpoint)

        read_quota(response)

        if response.status_code == 304 and found:
            FAILS = 0
            CACHE[endpoint] = cached
            return cached

        if response.status_code == 429:
            pause = retry_pause(response)
            elapsed = time.monotonic() - STARTED
            if attempt < MAX_RETRIES and elapsed + pause < MAX_RUNTIME_SECONDS:
                error(f"{endpoint} : limite atteinte, pause {pause} s.")
                time.sleep(pause)
                continue
            error(f"{endpoint} : limite de requetes depassee.")
            BLOCKED = True
            return None

        if response.status_code == 401:
            error("Cle API refusee (401).")
            BLOCKED = True
            return None

        if response.status_code == 404:
            # Ressource absente (stats non couvertes, slug inconnu...) :
            # ce n'est PAS une panne -> pas d'échec d'affilée.
            REASONS["Ressource absente (404)"] += 1
            CACHE[endpoint] = None
            disk_store(endpoint, None)
            return None

        if response.status_code >= 500 and attempt < MAX_RETRIES:
            error(f"{endpoint} : HTTP {response.status_code}, nouvel essai.")
            time.sleep(3 * (attempt + 1))
            continue

        if not response.ok:
            error(f"{endpoint} : HTTP {response.status_code}.")
            return failed(endpoint)

        try:
            payload = response.json()
        except ValueError:
            error(f"{endpoint} : JSON invalide.")
            return failed(endpoint)

        if not isinstance(payload, dict) or payload.get("success") is False:
            error(f"{endpoint} : reponse invalide.")
            return failed(endpoint)

        FAILS = 0
        CACHE[endpoint] = payload
        disk_store(endpoint, payload, response.headers.get("ETag") if hasattr(response, "headers") else None)
        return payload

    return None


# ---------------- CACHE DISQUE / QUOTA (6.6.0) ----------------

def paris_today():
    return datetime.now(PARIS).date()


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(temp, path)
    except (OSError, TypeError, ValueError) as exc:
        error(f"Ecriture {path} impossible : {type(exc).__name__}")


def day_cache_path(day):
    return os.path.join(DISK_CACHE_DIR, f"{day.isoformat()}.json")


def load_disk_cache(day=None):
    day = day or paris_today()
    if DISK["day"] == day.isoformat():
        return
    data = read_json(day_cache_path(day), {})
    DISK.update({
        "day": day.isoformat(),
        "data": data.get("data", {}) if isinstance(data.get("data"), dict) else {},
        "etag": data.get("etag", {}) if isinstance(data.get("etag"), dict) else {},
        "dirty": False,
    })
    if not PERM["loaded"]:
        perm = read_json(os.path.join(DISK_CACHE_DIR, "permanent.json"), {})
        PERM.update({"loaded": True, "data": perm, "dirty": False})
    purge_disk_cache(day)


def purge_disk_cache(day):
    """Supprime les caches du jour plus vieux que CACHE_KEEP_DAYS."""
    try:
        names = os.listdir(DISK_CACHE_DIR)
    except OSError:
        return
    for name in names:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})\.json$", name)
        if not match:
            continue
        try:
            age = (day - datetime.fromisoformat(match.group(1)).date()).days
        except ValueError:
            continue
        if age > CACHE_KEEP_DAYS:
            try:
                os.remove(os.path.join(DISK_CACHE_DIR, name))
            except OSError:
                pass


def save_disk_cache():
    if DISK["day"] and DISK["dirty"]:
        write_json(
            day_cache_path(datetime.fromisoformat(DISK["day"]).date()),
            {"data": DISK["data"], "etag": DISK["etag"]},
        )
        DISK["dirty"] = False
    if PERM["loaded"] and PERM["dirty"]:
        write_json(os.path.join(DISK_CACHE_DIR, "permanent.json"), PERM["data"])
        PERM["dirty"] = False


def is_fresh(endpoint):
    """Données qui bougent dans la journée : liste des matchs d'aujourd'hui
    et d'hier (scores). Redemandées à chaque lancement (avec ETag)."""
    match = re.match(r"^fixtures/date/(\d{4}-\d{2}-\d{2})$", endpoint)
    if not match:
        return False
    try:
        day = datetime.fromisoformat(match.group(1)).date()
    except ValueError:
        return True
    return (paris_today() - day).days <= 1


def disk_lookup(endpoint):
    """Renvoie (trouvé, payload, etag)."""
    load_disk_cache()
    if endpoint in PERM["data"]:
        return True, PERM["data"][endpoint], None
    if endpoint in DISK["data"]:
        value = DISK["data"][endpoint]
        payload = None if value == ABSENT else value
        return True, payload, DISK["etag"].get(endpoint)
    return False, None, None


def disk_store(endpoint, payload, etag=None):
    load_disk_cache()
    if payload is not None and PERMANENT_ENDPOINT.match(endpoint):
        # stats / compos / événements : on ne garde que les matchs terminés
        if payload.get("available") is not False:
            PERM["data"][endpoint] = payload
            PERM["dirty"] = True
            return
    DISK["data"][endpoint] = ABSENT if payload is None else payload
    if etag:
        DISK["etag"][endpoint] = etag
    DISK["dirty"] = True
    if len(DISK["data"]) % 25 == 0:
        save_disk_cache()


def header_number(response, name):
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return number(value)


def read_quota(response):
    """Lit le quota restant et resserre le budget de ce lancement."""
    global RUN_CAP
    day_left = header_number(response, "X-RateLimit-Remaining")
    month_left = header_number(response, "X-RateLimit-Month-Remaining")
    if day_left is not None:
        QUOTA["day"] = int(day_left)
    if month_left is not None:
        QUOTA["month"] = int(month_left)
        if not USAGE.get("share_from_api"):
            # 1re lecture du jour : part du jour = quota du mois / jours restants
            now = datetime.now(timezone.utc)
            days_left = month_days_left(now)
            used = int(USAGE.get("calls", 0)) + CALLS
            USAGE["share"] = int((QUOTA["month"] + used) / days_left)
            USAGE["share_from_api"] = True
            RUN_CAP = run_allowance()
    limits = []
    if "day" in QUOTA:
        limits.append(CALLS + QUOTA["day"] - QUOTA_RESERVE)
    if "month" in QUOTA:
        limits.append(CALLS + QUOTA["month"] - QUOTA_RESERVE)
    if limits:
        RUN_CAP = max(0, min([RUN_CAP if RUN_CAP is not None else MAX_API_CALLS] + limits))


def month_days_left(now):
    """Jours restants dans le mois (UTC), aujourd'hui compris."""
    if now.month == 12:
        first_next = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        first_next = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return max(1, (first_next.date() - now.date()).days)


def usage_path():
    return os.path.join(DISK_CACHE_DIR, "usage.json")


def run_allowance():
    """Appels permis pour CE lancement d'après la part du jour."""
    if MODE_SANS_LIMITE:
        # 6.9.2 : seul le quota réel (en-têtes X-RateLimit) limite le bot
        return MAX_API_CALLS
    share = int(USAGE.get("share") or DAILY_BUDGET_DEFAULT)
    before = int(USAGE.get("calls_before_run", 0))
    target = share if before > 0 else int(share * FIRST_RUN_SHARE)
    if NIGHT_RUN:
        # 6.8.1 : la nuit = peu de matchs ; petit budget fixe (la part du
        # jour UTC peut déjà être consommée par les lancements de 12h / 20h)
        return min(MAX_API_CALLS, NIGHT_API_CALLS)
    return max(0, min(MAX_API_CALLS, target - before))


def init_budget(day=None):
    """À appeler au début de main : charge l'usage du jour et fixe RUN_CAP."""
    global RUN_CAP
    day = (day or datetime.now(timezone.utc).date()).isoformat()
    data = read_json(usage_path(), {})
    if data.get("day") != day:
        data = {"day": day, "calls": 0, "runs": 0,
                "odds_calls": 0, "odds_remaining": data.get("odds_remaining"),
                # 6.8.1 : compteur de nuit gardé (la nuit chevauche 2 jours UTC)
                "night_day": data.get("night_day"),
                "night_odds_calls": data.get("night_odds_calls", 0),
                # 6.9.0 : compteur de cotes par jour de Paris
                "odds_paris_day": data.get("odds_paris_day"),
                "odds_paris_calls": data.get("odds_paris_calls", 0)}
    paris_day = paris_today().isoformat()
    if data.get("paris_day") != paris_day:
        data["paris_day"] = paris_day
        data["paris_runs"] = 0
    USAGE.clear()
    USAGE.update(data)
    USAGE["calls_before_run"] = int(USAGE.get("calls", 0))
    RUN_CAP = run_allowance()
    print(
        f"BUDGET : {RUN_CAP} appels pour ce lancement "
        f"(deja {USAGE['calls_before_run']} aujourd'hui, part du jour "
        f"{USAGE.get('share') or DAILY_BUDGET_DEFAULT})",
        flush=True,
    )


def save_usage(count_run=True):
    if not USAGE:
        return
    USAGE["calls"] = int(USAGE.get("calls_before_run", 0)) + CALLS
    if count_run:
        USAGE["runs"] = int(USAGE.get("runs", 0)) + 1
        USAGE["paris_runs"] = int(USAGE.get("paris_runs", 0)) + 1
    data = {k: v for k, v in USAGE.items() if k != "calls_before_run"}
    data.update({"quota_day": QUOTA.get("day"), "quota_month": QUOTA.get("month")})
    write_json(usage_path(), data)


def get_list(payload, key="data"):
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return value if isinstance(value, list) else []


# ---------------- CACHE TIRS CADRÉS ----------------

def load_sot_cache():
    if SOT_CACHE:
        return SOT_CACHE

    if os.path.exists(SOT_CACHE_FILE):
        try:
            with open(SOT_CACHE_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                SOT_CACHE.update(data)
        except (OSError, ValueError):
            pass

    return SOT_CACHE


def save_sot_cache():
    try:
        os.makedirs(os.path.dirname(SOT_CACHE_FILE), exist_ok=True)
        temp = SOT_CACHE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(SOT_CACHE, file, ensure_ascii=False)
        os.replace(temp, SOT_CACHE_FILE)
    except OSError as exc:
        # Un cache non sauvegardé ne doit pas faire perdre l'analyse
        error(f"Cache tirs cadres non sauvegarde : {type(exc).__name__}")


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
        return datetime.fromisoformat(str(match.get("date"))[:10]).date()
    except ValueError:
        return None


def short_name(value):
    name = " ".join(str(value or "Equipe").split())
    return name if len(name) <= 25 else name[:24] + "…"


def is_youth(fixture):
    return any(
        YOUTH_PATTERN.search(str(fixture.get(field) or ""))
        for field in ("home_team", "away_team")
    )


def league_label(fixture):
    for key in ("league_name", "league", "competition"):
        value = fixture.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            return short_name(value)
    return None


def prioritise(fixtures):
    ordered = sorted(
        fixtures,
        key=lambda f: (
            0 if str(f.get("league_id")) in PRIORITY_LEAGUE_IDS else 1,
            kickoff_of(f),
        ),
    )

    kept = []
    taken = Counter()

    for fixture in ordered:
        league = str(fixture.get("league_id"))
        if (
            league not in PRIORITY_LEAGUE_IDS
            and taken[league] >= MAX_PER_LEAGUE
        ):
            REASONS["Plafond par ligue atteint"] += 1
            continue
        taken[league] += 1
        kept.append(fixture)

    return kept


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


# ---------------- CLASSEMENT ----------------

def standings_row(league_id, team_id):
    key = str(league_id)
    if key not in STANDINGS_CACHE:
        payload = api_get(f"standings/{league_id}")
        STANDINGS_CACHE[key] = get_list(payload)
        # 6.5.0 : on garde aussi le format / la saison / les groupes
        if isinstance(payload, dict):
            STANDINGS_META[key] = {
                "format": payload.get("format"),
                "season": payload.get("season"),
                "stage": payload.get("stage"),
                "groups": payload.get("groups"),
                "league": payload.get("league"),
            }

    for row in STANDINGS_CACHE[key]:
        if not isinstance(row, dict):
            continue
        team = row.get("team")
        if isinstance(team, dict) and str(team.get("id")) == str(team_id):
            return row
    return None


# ---------------- HISTORIQUE EQUIPE ----------------

def parse_results(matches, team_id, today, slug=None):
    history = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        if str(match.get("status") or "").upper() != "FT":
            continue

        date = date_of(match)
        if date is None:
            continue

        age = (today - date).days
        if age <= 0 or age > MAX_HISTORY_DAYS:
            continue

        home_score = number(match.get("home_score"))
        away_score = number(match.get("away_score"))
        if home_score is None or away_score is None:
            continue
        if home_score < 0 or away_score < 0:
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
            "gf": home_score if is_home else away_score,
            "ga": away_score if is_home else home_score,
            # 6.4.0 : vrais identifiants du match historique (tirs cadrés)
            "home_id": match.get("home_id"),
            "away_id": match.get("away_id"),
            # 6.9.0 : compétition (fatigue Coupe d'Europe)
            "league_name": match.get("league_name") or "",
        })

    return history


def merge_history(*groups):
    unique = {}
    for group in groups:
        for match in group:
            identifier = match.get("id")
            if identifier is None:
                identifier = (
                    match["date"], match["venue"],
                    match["gf"], match["ga"],
                )
            unique[identifier] = match

    return sorted(unique.values(), key=lambda m: m["date"], reverse=True)


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

        if None in (date, home, home_score, away_score):
            continue
        if home_score < 0 or away_score < 0:
            continue
        if not 0 < (today - date).days <= MAX_HISTORY_DAYS:
            continue

        history.append({
            "id": None,
            "date": date,
            "venue": "home" if home else "away",
            "gf": home_score if home else away_score,
            "ga": away_score if home else home_score,
            "home_id": None,
            "away_id": None,
        })

    return merge_history(history)


def team_history(team_id, league_id, today):
    key = str(team_id)
    if key in TEAM_CACHE:
        return TEAM_CACHE[key]

    payload = api_get(f"teams/{team_id}")
    history = parse_results(get_list(payload, "matches"), team_id, today)
    history = merge_history(history)

    team = payload.get("team") if isinstance(payload, dict) else {}
    team = team if isinstance(team, dict) else {}
    slug = team.get("slug")
    row = None

    if len(history) < HISTORY_N and not slug:
        row = standings_row(league_id, team_id)
        row_team = row.get("team") if isinstance(row, dict) else {}
        if isinstance(row_team, dict):
            slug = row_team.get("slug")

    if len(history) < HISTORY_N and slug:
        slug_payload = api_get(f"teams/slug/{quote(str(slug), safe='')}")
        extra = parse_results(
            get_list(slug_payload, "results"),
            team_id,
            today,
            slug=str(slug),
        )
        history = merge_history(history, extra)

    if len(history) < MIN_HISTORY:
        if row is None:
            row = standings_row(league_id, team_id)

        fallback = history_from_form(row, today)
        if len(fallback) > len(history):
            history = fallback

    print(
        f"HISTORIQUE FINAL EQUIPE {team_id} : "
        f"{len(history)} resultats exploitables",
        flush=True,
    )

    TEAM_CACHE[key] = history
    return history


# ---------------- MODELE EQUIPE ----------------

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
        "btts": average(lambda m: float(m["gf"] > 0 and m["ga"] > 0)),
        "over15": average(lambda m: float(m["gf"] + m["ga"] >= 2)),
    }


def profile(history, venue, today):
    recent = history[:HISTORY_N]
    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    overall = weighted_stats(recent)

    local_matches = [
        m for m in history if m["venue"] == venue
    ][:VENUE_N]

    if len(local_matches) >= 3:
        venue_weight = min(0.50, len(local_matches) / (len(local_matches) + 6))
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

    n = len(recent)
    for metric, prior in PRIORS.items():
        result[metric] = (
            result[metric] * n + prior * PRIOR_K
        ) / (n + PRIOR_K)

    result["n"] = n
    result["venue_n"] = len(local_matches)
    return result


def estimate(home, away):
    lambda_home = (home["gf"] + away["ga"]) / 2
    lambda_away = (away["gf"] + home["ga"]) / 2

    p_home = 1 - math.exp(-lambda_home)
    p_away = 1 - math.exp(-lambda_away)

    # 6.4.0 : l'avantage du terrain est déjà dans les stats domicile /
    # extérieur quand on en a au moins 3 -> le bonus ne s'applique que sinon
    home_adjust = HOME_ADJUST if home.get("venue_n", 0) < 3 else 0.0
    away_adjust = AWAY_ADJUST if away.get("venue_n", 0) < 3 else 0.0
    p_home = min(p_home + home_adjust, HOME_PROB_CAP)
    p_away = min(max(p_away + away_adjust, 0.0), HOME_PROB_CAP)

    total = lambda_home + lambda_away

    btts = (
        0.75 * p_home * p_away
        + 0.25 * (home["btts"] + away["btts"]) / 2
    )

    over15 = (
        0.75 * (1 - math.exp(-total) * (1 + total))
        + 0.25 * (home["over15"] + away["over15"]) / 2
    )

    return lambda_home, lambda_away, {
        "home": p_home,
        "away": p_away,
        "btts": btts,
        "over15": over15,
    }


# ---------------- TICKET (FORME + MARCHÉS BUTS) ----------------

def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def win_probabilities(lambda_home, lambda_away, limit=12):
    home_pmf = [poisson_pmf(i, lambda_home) for i in range(limit)]
    away_pmf = [poisson_pmf(i, lambda_away) for i in range(limit)]
    p_home = sum(
        home_pmf[i] * away_pmf[j]
        for i in range(limit) for j in range(limit) if i > j
    )
    p_away = sum(
        home_pmf[i] * away_pmf[j]
        for i in range(limit) for j in range(limit) if i < j
    )
    return p_home, p_away


def extra_probabilities(lambda_home, lambda_away):
    """Marchés supplémentaires (journalisés, jamais publiés comme pronostics)."""
    total = lambda_home + lambda_away
    p_win_home, p_win_away = win_probabilities(lambda_home, lambda_away)
    extra = {
        "win_home": p_win_home,
        "win_away": p_win_away,
        "home15": 1 - math.exp(-lambda_home) * (1 + lambda_home),
        "away15": 1 - math.exp(-lambda_away) * (1 + lambda_away),
        "over25": 1 - math.exp(-total) * (1 + total + total ** 2 / 2),
    }
    # 6.7.0 : correction mesurée sur le backtest (5 333 matchs)
    for market, shift in EXTRA_ADJUST.items():
        extra[market] = min(max(extra[market] + shift, 0.0), 0.95)
    return extra


def high_odds_picks(probabilities, extra, blocked):
    """6.7.0 : pronos 'cote plus haute' (Domicile +1,5 / Plus de 2,5 buts)."""
    prob = {**probabilities, **extra}
    picks = []
    for market, (low, high) in HIGH_ODDS_PICKS.items():
        p = prob.get(market)
        if p is None or market in blocked or market in NOT_PROPOSED:
            continue
        if low <= p <= high:
            picks.append((market, p))
    picks.sort(key=lambda item: -item[1])
    return picks[:HIGH_ODDS_MAX_PER_MATCH]


def form_stats(history):
    last = history[:TICKET_FORM_N]
    n = len(last)
    if n < 3:
        return None

    def share(function):
        return sum(1 for m in last if function(m)) / n

    points = sum(
        3 if m["gf"] > m["ga"] else 1 if m["gf"] == m["ga"] else 0
        for m in last
    )
    letters = [
        "V" if m["gf"] > m["ga"] else "N" if m["gf"] == m["ga"] else "D"
        for m in reversed(last)
    ]

    def streak(function):
        count = 0
        for m in last:  # du plus récent au plus ancien
            if not function(m):
                break
            count += 1
        return count

    return {
        "n": n,
        "points": points,
        "pts_rate": points / (3 * n),
        "scored": share(lambda m: m["gf"] > 0),
        "conceded": share(lambda m: m["ga"] > 0),
        "loss_streak": streak(lambda m: m["gf"] < m["ga"]),
        "dry_streak": streak(lambda m: m["gf"] == 0),
        "g2": share(lambda m: m["gf"] >= 2),
        "c2": share(lambda m: m["ga"] >= 2),
        "o15": share(lambda m: m["gf"] + m["ga"] >= 2),
        "o25": share(lambda m: m["gf"] + m["ga"] >= 3),
        "btts": share(lambda m: m["gf"] > 0 and m["ga"] > 0),
        "gf": int(sum(m["gf"] for m in last)),
        "ga": int(sum(m["ga"] for m in last)),
        "letters": " ".join(letters),
    }


def blocked_markets(home_form, away_form):
    """Filtre de forme sur les 5 derniers matchs des deux équipes.

    Renvoie {marché: raison} pour chaque marché interdit. Sans forme
    exploitable (moins de 3 matchs), on interdit tout : pas de pari à
    l'aveugle.
    """
    every = ("home", "away", "btts", "over15", "over25",
             "win_home", "win_away", "home15", "away15")
    if home_form is None or away_form is None:
        return {m: "forme récente insuffisante" for m in every}

    blocked = {}
    sides = (
        ("home", home_form, away_form),
        ("away", away_form, home_form),
    )
    for side, team, opponent in sides:
        n = team["n"]
        scored = round(team["scored"] * n)
        conceded = round(opponent["conceded"] * opponent["n"])
        # seuils ramenés au nombre de matchs dispo (3 sur 5 -> 2 sur 3)
        min_scored = math.ceil(FORM_MIN_SCORED * n / TICKET_FORM_N)
        min_conceded = math.ceil(FORM_MIN_CONCEDED * opponent["n"] / TICKET_FORM_N)

        # Équipe sur une série de défaites : jamais sa victoire sèche
        if team["loss_streak"] >= LOSING_STREAK_MAX:
            blocked[f"win_{side}"] = f"{team['loss_streak']} défaites d'affilée"
        # Adversaire qui ne perd plus : pas de victoire non plus si
        # l'équipe ne gagne pas plus souvent que lui
        elif team["pts_rate"] <= opponent["pts_rate"]:
            blocked[f"win_{side}"] = "forme pas meilleure que l'adversaire"

        if team["dry_streak"] >= DRY_STREAK_MAX:
            reason = f"{team['dry_streak']} matchs sans marquer"
            blocked[side] = reason
            blocked[f"{side}15"] = reason
        elif scored < min_scored:
            blocked[side] = f"a marqué dans {scored}/{n} seulement"
            blocked[f"{side}15"] = blocked[side]
        elif conceded < min_conceded:
            blocked[side] = (
                f"adversaire a encaissé dans {conceded}/{opponent['n']} seulement"
            )
            blocked[f"{side}15"] = blocked[side]

        # +1,5 but équipe : il faut des matchs à 2 buts et une défense poreuse
        if f"{side}15" not in blocked and (team["g2"] + opponent["c2"]) / 2 < FORM_MIN_RATE:
            blocked[f"{side}15"] = "trop peu de matchs à 2 buts marqués/encaissés"

    # BTTS : les deux équipes doivent pouvoir marquer
    if "home" in blocked or "away" in blocked:
        blocked["btts"] = blocked.get("home") or blocked.get("away")
    elif (home_form["btts"] + away_form["btts"]) / 2 < BTTS_FORM_MIN_RATE:
        blocked["btts"] = "peu de BTTS sur les 5 derniers"

    if (home_form["o15"] + away_form["o15"]) / 2 < FORM_MIN_RATE + 0.2:
        blocked["over15"] = "peu de matchs à 2 buts ou plus"
    if (home_form["o25"] + away_form["o25"]) / 2 < FORM_MIN_RATE:
        blocked["over25"] = "peu de matchs à 3 buts ou plus"

    return blocked


def ticket_candidates(probabilities, extra, home_form, away_form, motivation=None):
    """Un candidat par marché : probabilité modèle + forme des 5 derniers matchs."""
    if home_form is None or away_form is None:
        return []

    prob = {**probabilities, **extra}
    blocked = blocked_markets(home_form, away_form)
    motivation = motivation or {}
    form = {
        # 6.4.0 : "équipe +0,5 but" a enfin un score de forme
        "home": (home_form["scored"] + away_form["conceded"]) / 2,
        "away": (away_form["scored"] + home_form["conceded"]) / 2,
        "win_home": (home_form["pts_rate"] + 1 - away_form["pts_rate"]) / 2,
        "win_away": (away_form["pts_rate"] + 1 - home_form["pts_rate"]) / 2,
        "home15": (home_form["g2"] + away_form["c2"]) / 2,
        "away15": (away_form["g2"] + home_form["c2"]) / 2,
        "over15": (home_form["o15"] + away_form["o15"]) / 2,
        "over25": (home_form["o25"] + away_form["o25"]) / 2,
        "btts": (home_form["btts"] + away_form["btts"]) / 2,
    }

    candidates = []
    for market, form_rate in form.items():
        p = prob.get(market)
        if p is None or market in blocked or market in NOT_PROPOSED:
            continue
        if market == "btts" and p < BTTS_TICKET_MIN_PROB:
            continue
        score = (1 - TICKET_FORM_WEIGHT) * p + TICKET_FORM_WEIGHT * form_rate
        score += motivation_adjust(market, motivation)
        score += MARKET_RANK_BONUS.get(market, 0.0)
        candidates.append({
            "market": market,
            "prob": p,
            "form": form_rate,
            "score": score,
        })
    return candidates


def ticket_league_ok(fixture):
    """6.6.3 : la ligue du match est-elle autorisée dans le ticket ?"""
    if fixture.get("league_id") is not None and fixture.get("league_id") in TICKET_LEAGUE_IDS:
        return True
    if str(fixture.get("league_id")) in {str(i) for i in TICKET_LEAGUE_IDS}:
        return True
    name = norm_text(fixture.get("league_name"))
    if not name or re.search(TICKET_LEAGUE_EXCLUDE, name):
        return False
    zones = {norm_text(fixture.get(k)) for k in ("league_zone", "league_geo", "country")}
    zones.discard("")
    for pattern, country, exclude in TICKET_LEAGUES:
        if not re.search(pattern, name):
            continue
        if exclude and re.search(exclude, name):
            continue
        if country is None:
            return True
        words = COUNTRY_WORDS.get(country, (country.lower(),))
        if norm_text(country) in zones or any(w in zones for w in words):
            return True
        padded = f" {name} "
        if any(f" {w} " in padded for w in words):
            return True
    return False


def is_night_run(now):
    """6.8.1 : lancement de nuit = entre 00:00 et NIGHT_RUN_END_HOUR (Paris)."""
    return now.astimezone(PARIS).hour < NIGHT_RUN_END_HOUR


def kickoff_still_open(kickoff, now=None):
    """6.9.4 : le match n'a pas commencé et ne commence pas dans la marge
    de sécurité (KICKOFF_MARGIN_MINUTES), heure de Paris."""
    if kickoff is None:
        return False
    now = now or datetime.now(PARIS)
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=PARIS)
    else:
        kickoff = kickoff.astimezone(PARIS)
    if now.tzinfo is None:
        now = now.replace(tzinfo=PARIS)
    else:
        now = now.astimezone(PARIS)
    return kickoff > now + timedelta(minutes=KICKOFF_MARGIN_MINUTES)


def ticket_fixture_ids(legs):
    """Identifiants de matchs déjà retenus dans un ticket (jour, fun, fun journée)."""
    return {str(leg["result"]["fixture"].get("id")) for leg in (legs or []) if leg}


def item_kickoff(item):
    """Coup d'envoi Paris d'un couple (result, candidate) ou d'une jambe finie."""
    if isinstance(item, dict):
        result = item.get("result")
    else:
        result = item[0]
    return kickoff_of(result["fixture"]) if result else None


def in_same_slot(items, hours=None):
    """True si tous les coups d'envoi tiennent dans une fenêtre de `hours` heures."""
    hours = TICKET_SLOT_HOURS if hours is None else hours
    times = [t for t in (item_kickoff(i) for i in items) if t]
    if not times:
        return False
    return max(times) - min(times) <= timedelta(hours=hours)


def slot_windows(pool, hours=None):
    """Fenêtres [t, t+hours] partant de chaque coup d'envoi distinct du pool."""
    hours = TICKET_SLOT_HOURS if hours is None else hours
    timed = []
    for item in pool:
        ko = item_kickoff(item)
        if ko is not None:
            timed.append((ko, item))
    if not timed:
        return []
    windows, seen = [], set()
    for start in sorted({ko for ko, _ in timed}):
        end = start + timedelta(hours=hours)
        items = [item for ko, item in timed if start <= ko <= end]
        key = tuple(sorted(str(it[0]["fixture"].get("id")) for it in items))
        if len(items) < 2 or key in seen:
            continue
        seen.add(key)
        windows.append(items)
    return windows


def ticket_window_ok(kickoff, now):
    """Jour : maintenant + marge -> 23:59, rien avant 08:00 (6.8.0).
    Nuit (6.8.1) : maintenant + marge -> 08:00 seulement."""
    if not kickoff_still_open(kickoff, now):
        return False
    kickoff = kickoff.astimezone(PARIS)
    now = now.astimezone(PARIS)
    if kickoff.date() != now.date():
        return False
    if is_night_run(now):
        return kickoff.hour < NIGHT_WINDOW_END_HOUR
    return kickoff.hour >= TICKET_EARLIEST_HOUR


def build_ticket(all_results, now):
    """6.9.5 : jusqu'à 4 jambes, matchs différents, dans une fenêtre de
    TICKET_SLOT_HOURS heures (le meilleur créneau), ligues autorisées,
    vraies cotes quand elles existent. Si aucun créneau n'a 4 matchs,
    ticket plus court."""
    pool = []

    legs = []
    for result in all_results:
        kickoff = kickoff_of(result["fixture"])
        if not ticket_window_ok(kickoff, now):
            continue
        # 6.6.3 : grands championnats et sélections A seulement
        if not ticket_league_ok(result["fixture"]):
            continue
        for candidate in result.get("candidates", []):
            if not ticket_market_allowed(candidate):
                continue
            if TICKET_MIN_PROB <= candidate["prob"] <= TICKET_MAX_PROB:
                pool.append((result, dict(candidate)))

    pool.sort(key=lambda item: -item[1]["score"])

    # 6.8.0 : vraie cote de chaque candidat (les mieux notés d'abord : leurs
    # ligues passent en premier dans les appels du jour). Jambe refusée si
    # la vraie cote est sous TICKET_MIN_LEG_ODDS ; sinon cote juste en secours.
    pool = price_pool(pool, TICKET_MIN_LEG_ODDS, max(TICKET_MIN_LEG_ODDS, MIN_LEG_ODDS))

    # 6.6.0 : au plus TICKET_PER_MATCH marchés par match dans la sélection
    # (sinon un même match occupe la place de matchs différents)
    per_match = Counter()
    trimmed = []
    for item in pool:
        fid = str(item[0]["fixture"].get("id"))
        if per_match[fid] >= TICKET_PER_MATCH:
            continue
        per_match[fid] += 1
        trimmed.append(item)

    best = None
    for window in slot_windows(trimmed):
        cand = sorted(window, key=lambda item: -item[1]["score"])[:TICKET_POOL]
        for size in range(min(TICKET_LEGS, len(cand)), 1, -1):
            for combo in itertools.combinations(cand, size):
                if len({str(r["fixture"].get("id")) for r, _ in combo}) < size:
                    continue
                if not in_same_slot(combo):
                    continue
                total = 1.0
                for _, c in combo:
                    total *= leg_odds(c)
                mean_score = sum(c["score"] for _, c in combo) / size
                chance = math.prod(c["prob"] for _, c in combo)
                # La cote cible est nécessaire pour obtenir le rendement
                # demandé, mais la priorité est la probabilité cumulée de
                # réussite. La proximité de 10 ne sert qu'à départager.
                key = (
                    size == TICKET_LEGS,
                    total >= TICKET_TARGET_ODDS,
                    chance,
                    -abs(math.log(total / TICKET_TARGET_ODDS)),
                    mean_score,
                )
                if best is None or key > best[0]:
                    best = (key, combo)
    if not best:
        return []
    legs = [{"result": r, **c} for r, c in best[1]]
    legs.sort(key=lambda leg: kickoff_of(leg["result"]["fixture"]))
    return legs


def price_pool(pool, min_real, min_fair, max_odds=None, label="Ticket"):
    """6.9.0 : cote de chaque candidat (vraie cote si l'API la donne, sinon
    cote juste) ; refusé hors [min, max]. Aucun appel pour un marché que
    The Odds API ne couvre pas."""
    priced = []
    for result, candidate in pool:
        price = None
        if candidate["market"] in REAL_ODDS_MARKETS:
            try:
                price = real_odds_for(result["fixture"], [candidate["market"]]).get(candidate["market"])
            except Exception as exc:
                error(f"Cote ticket impossible : {type(exc).__name__} {exc}")
        if price:
            if price < min_real - 1e-9 or (max_odds and price > max_odds):
                REASONS[f"{label} : vraie cote hors limites"] += 1
                continue
            candidate.update(odds=price, real=True)
        else:
            fair = 1 / candidate["prob"]
            if fair < min_fair - 1e-9 or (max_odds and fair > max_odds):
                continue
            candidate.update(odds=fair, real=False)
        priced.append((result, candidate))
    return priced


def ticket_pool(all_results, now, keep):
    """Candidats des matchs autorisés et dans la fenêtre, triés par note."""
    pool = []
    for result in all_results:
        fixture = result["fixture"]
        if not ticket_window_ok(kickoff_of(fixture), now) or not ticket_league_ok(fixture):
            continue
        for candidate in result.get("candidates", []):
            if keep(candidate):
                pool.append((result, dict(candidate)))
    pool.sort(key=lambda item: -item[1]["score"])
    return pool


def finish_legs(combo):
    legs = [{"result": r, **c} for r, c in combo]
    legs.sort(key=lambda leg: kickoff_of(leg["result"]["fixture"]))
    return legs


def mega_form_ok(result, candidate):
    """6.9.3 : forme stricte sur les 5 derniers matchs pour le Méga fun."""
    home_form = result.get("home_form")
    away_form = result.get("away_form")
    if not home_form or not away_form:
        return False
    if home_form["n"] < 4 or away_form["n"] < 4:
        return False
    if candidate.get("form", 0.0) < MEGA_MIN_FORM:
        return False
    market = candidate["market"]
    if market == "over25":
        return (home_form["o25"] + away_form["o25"]) / 2 >= 0.50 and home_form["o15"] >= 0.60 and away_form["o15"] >= 0.60
    if market == "win_home":
        return home_form["pts_rate"] >= 0.53 and home_form["loss_streak"] == 0
    if market == "win_away":
        return away_form["pts_rate"] >= 0.53 and away_form["loss_streak"] == 0
    return True


def build_mega(all_results, now, exclude_ids=None):
    """6.9.5 : 🎉 MÉGA FUN solide : 4-5 sélections >= MEGA_MIN_PROB (55 %),
    forme stricte, cotes cibles 2,20-3,50, matchs distincts du ticket du jour,
    tous dans une fenêtre de TICKET_SLOT_HOURS heures. Si la cote 50 n'est
    pas atteignable avec des sélections solides, on publie le meilleur
    combiné court du meilleur créneau, sans empiler de paris faibles."""
    exclude_ids = {str(i) for i in (exclude_ids or [])}
    raw_pool = []
    for result in all_results:
        fixture = result["fixture"]
        if str(fixture.get("id")) in exclude_ids:
            continue
        if not ticket_window_ok(kickoff_of(fixture), now) or not ticket_league_ok(fixture):
            continue
        for candidate in result.get("candidates", []):
            if not ticket_market_allowed(candidate):
                continue
            if candidate["prob"] >= MEGA_MIN_PROB and 1 / candidate["prob"] <= MEGA_MAX_LEG_ODDS * 1.25:
                if mega_form_ok(result, candidate):
                    raw_pool.append((result, dict(candidate)))
    raw_pool.sort(key=lambda item: -item[1]["score"])

    pool = price_pool(
        raw_pool, MEGA_MIN_FALLBACK_ODDS, MEGA_MIN_FAIR_ODDS, MEGA_MAX_LEG_ODDS, "Méga fun"
    )
    # 1 sélection par match
    seen_fid = set()
    trimmed = []
    for item in pool:
        fid = str(item[0]["fixture"].get("id"))
        if fid in seen_fid:
            continue
        seen_fid.add(fid)
        trimmed.append(item)
    trimmed = trimmed[:MEGA_POOL]
    if len(trimmed) < 2:
        return []

    # 1) Essai d'atteindre 50, créneau par créneau, avec des sélections 2,20-3,50
    best = None
    for window in slot_windows(trimmed):
        high_value = [item for item in window if leg_odds(item[1]) >= MEGA_MIN_LEG_ODDS - 1e-9]
        if len(high_value) < MEGA_MIN_LEGS:
            continue
        for size in range(MEGA_MIN_LEGS, min(MEGA_MAX_LEGS, len(high_value)) + 1):
            for combo in itertools.combinations(high_value, size):
                if len({str(r["fixture"].get("id")) for r, _ in combo}) < size:
                    continue
                if not in_same_slot(combo):
                    continue
                total = 1.0
                chance = 1.0
                for _, c in combo:
                    total *= leg_odds(c)
                    chance *= c["prob"]
                if total < MEGA_TARGET_ODDS:
                    continue
                # Parmi les combinaisons atteignant 50, choisir celle dont la
                # probabilité cumulée est la plus élevée. La proximité de 50
                # ne sert qu'à départager les probabilités proches.
                key = (
                    chance,
                    -abs(math.log(total / MEGA_TARGET_ODDS)),
                    sum(c["score"] for _, c in combo) / size,
                )
                if best is None or key > best[0]:
                    best = (key, combo)
    if best is not None:
        return finish_legs(best[1])

    # 2) Sinon : meilleur combiné court solide dans le meilleur créneau
    best_short = None
    for window in slot_windows(trimmed):
        ranked = sorted(
            window,
            key=lambda item: (
                1 if leg_odds(item[1]) >= MEGA_MIN_LEG_ODDS - 1e-9 else 0,
                item[1]["prob"],
                item[1]["score"],
            ),
            reverse=True,
        )
        short_size = min(MEGA_MIN_LEGS, len(ranked))
        if short_size < 2:
            continue
        combo = ranked[:short_size]
        key = (
            short_size,
            sum(1 for _, c in combo if leg_odds(c) >= MEGA_MIN_LEG_ODDS - 1e-9),
            sum(c["prob"] for _, c in combo),
            sum(c["score"] for _, c in combo),
        )
        if best_short is None or key > best_short[0]:
            best_short = (key, combo)
    if best_short is None:
        if len(trimmed) >= 2 and in_same_slot(trimmed[:min(MEGA_MIN_LEGS, len(trimmed))]):
            return finish_legs(trimmed[:min(MEGA_MIN_LEGS, len(trimmed))])
        return []
    return finish_legs(best_short[1])


def safe_form_ok(result, market):
    """6.9.3 : forme 4/5 minimum de l'équipe qui doit marquer (et >= 3/5 encaissés en face)."""
    home_form = result.get("home_form")
    away_form = result.get("away_form")
    if not home_form or not away_form:
        return False
    side = "home" if market == "home" else "away"
    team = home_form if side == "home" else away_form
    opp = away_form if side == "home" else home_form
    if team["n"] < 4 or opp["n"] < 3:
        return False
    scored = round(team["scored"] * team["n"])
    conceded = round(opp["conceded"] * opp["n"])
    if scored < math.ceil(SAFE_FORM_MIN_SCORED * team["n"] / TICKET_FORM_N):
        return False
    if conceded < math.ceil(SAFE_FORM_MIN_CONCEDED * opp["n"] / TICKET_FORM_N):
        return False
    return True


def _safe_absence_adjust(result, candidate, today):
    """6.9.3 : vérifie les absences uniquement pour les meilleurs candidats Ultra safe."""
    if not SAFE_ABSENCES_ACTIF or not today:
        return candidate, True
    if ABSENCE_STATE["calls"] >= SAFE_ABSENCE_MAX_CALLS or not budget_available(reserve=40):
        return candidate, True
    fixture = result["fixture"]
    side = "home" if candidate["market"] == "home" else "away"
    history = result.get("home_history") if side == "home" else result.get("away_history")
    if not history:
        return candidate, True
    cut, notes = absence_of(
        fixture, side, history, today, force=True, max_calls=SAFE_ABSENCE_MAX_CALLS
    )
    if cut <= 0 and not notes:
        return candidate, True
    hp = result.get("home_profile")
    ap = result.get("away_profile")
    prob = candidate["prob"]
    if cut > 0 and hp and ap:
        adj_h = dict(hp, gf=hp["gf"] * (1 - cut)) if side == "home" else hp
        adj_a = dict(ap, gf=ap["gf"] * (1 - cut)) if side == "away" else ap
        _, _, probs = estimate(adj_h, adj_a)
        prob = probs.get(candidate["market"], prob)
    if prob < SAFE_MIN_PROB or 1 / prob < SAFE_MIN_LEG_ODDS - 1e-9:
        return candidate, False
    updated = dict(candidate)
    updated["prob"] = prob
    updated["odds"] = 1 / prob
    if notes:
        updated["alerts"] = notes
    return updated, True


def build_safe(all_results, now, today=None):
    """6.9.3 : 🛡️ ULTRA SAFE. Équipe +0,5 but uniquement, SAFE_MAX_LEGS
    matchs max : proba >= 80 %, forme 4/5, cote >= 1,20 (sinon écarté),
    vérification légère des absences dans la limite du quota."""
    raw_pool = []
    for result in all_results:
        fixture = result["fixture"]
        if not ticket_window_ok(kickoff_of(fixture), now) or not ticket_league_ok(fixture):
            continue
        for candidate in result.get("candidates", []):
            if candidate["market"] not in SAFE_MARKETS:
                continue
            if candidate["prob"] < SAFE_MIN_PROB or 1 / candidate["prob"] < SAFE_MIN_LEG_ODDS - 1e-9:
                continue
            if not safe_form_ok(result, candidate["market"]):
                continue
            raw_pool.append((result, dict(candidate)))
    raw_pool.sort(key=lambda item: (-item[1]["prob"], -item[1]["score"]))

    combo = []
    seen = set()
    checked = 0
    for result, candidate in raw_pool:
        fid = str(result["fixture"].get("id"))
        if fid in seen:
            continue
        seen.add(fid)
        if checked < SAFE_ABSENCE_CANDIDATES:
            checked += 1
            candidate, ok = _safe_absence_adjust(result, candidate, today)
            if not ok:
                REASONS["Ultra safe : écarté après absences"] += 1
                continue
        candidate.update(odds=1 / candidate["prob"], real=False)
        combo.append((result, candidate))
        if len(combo) >= SAFE_MAX_LEGS:
            break
    return finish_legs(combo)


def dayfun_boost_ok(result, candidate):
    """6.9.9 : sélection valeur 61–70 %, cote juste utile et forme confirmée."""
    if candidate.get("market") not in DAYFUN_BOOST_MARKETS:
        return False
    prob = candidate.get("prob") or 0.0
    if prob < DAYFUN_BOOST_MIN_PROB:
        return False
    odds = 1 / prob
    if odds < DAYFUN_BOOST_MIN_ODDS - 1e-9 or odds > DAYFUN_BOOST_MAX_ODDS + 1e-9:
        return False
    return mega_form_ok(result, candidate)


def is_boost_leg(leg):
    """True si la jambe est un boost (drapeau, ou cote / marché reconnus)."""
    if leg.get("boost"):
        return True
    if leg.get("market") not in DAYFUN_BOOST_MARKETS:
        return False
    prob = leg.get("prob") or 0.0
    if prob < DAYFUN_BOOST_MIN_PROB - 1e-9:
        return False
    odds = leg_odds(leg)
    return DAYFUN_BOOST_MIN_ODDS - 1e-9 <= odds <= DAYFUN_BOOST_MAX_ODDS + 1e-9


def build_dayfun(all_results, now, exclude_ids=None):
    """6.9.6 : 🎯 FUN JOURNÉE mixte. Jusqu'à 7 sélections solides (>= 70 %,
    équipe +0,5 / +1,5) et jusqu'à 3 boosts (cote juste vers 1,70-2,20, >= 55 %,
    +2,5 ou victoire, forme ok). Une sélection par match, toute la journée,
    sans appel cotes ni absences, distinct du ticket du jour et du méga fun.
    On vise 40-60 de cote ; s'il manque des boosts, on publie le réel."""
    exclude_ids = {str(i) for i in (exclude_ids or [])}
    allowed_solid = set(DAYFUN_MARKETS) | set(DAYFUN_EXTRA_MARKETS)
    pref = {m: i for i, m in enumerate(DAYFUN_MARKETS)}
    solids, boosts = [], []
    for result in all_results:
        fixture = result["fixture"]
        fid = str(fixture.get("id"))
        if fid in exclude_ids:
            continue
        if not ticket_window_ok(kickoff_of(fixture), now) or not ticket_league_ok(fixture):
            continue
        best_solid = best_solid_key = None
        best_boost = best_boost_key = None
        for candidate in result.get("candidates", []):
            market = candidate["market"]
            prob = candidate["prob"]
            if market in allowed_solid and prob >= DAYFUN_MIN_PROB and 1 / prob >= DAYFUN_MIN_LEG_ODDS - 1e-9:
                key = (0 if market in pref else 1, pref.get(market, 9), -1 / prob, -candidate.get("score", 0))
                if best_solid_key is None or key < best_solid_key:
                    best_solid_key = key
                    best_solid = dict(candidate)
            if dayfun_boost_ok(result, candidate):
                odds = 1 / prob
                key = (-odds, -candidate.get("score", 0), -prob)
                if best_boost_key is None or key < best_boost_key:
                    best_boost_key = key
                    best_boost = dict(candidate)
        # Un match peut servir de solide ou de boost : s'il n'est pas retenu
        # parmi les 7 solides, il reste disponible pour un boost.
        if best_solid:
            best_solid.update(odds=1 / best_solid["prob"], real=False, boost=False)
            solids.append((result, best_solid))
        if best_boost:
            best_boost.update(odds=1 / best_boost["prob"], real=False, boost=True)
            boosts.append((result, best_boost))
    solids.sort(key=lambda item: (-item[1]["odds"], -item[1].get("score", 0)))
    boosts.sort(key=lambda item: (-item[1]["odds"], -item[1].get("score", 0)))
    picked, seen = [], set()
    for result, candidate in solids:
        if len([c for _, c in picked if not c.get("boost")]) >= DAYFUN_SOLID_LEGS:
            break
        fid = str(result["fixture"].get("id"))
        if fid in seen:
            continue
        seen.add(fid)
        picked.append((result, candidate))
    for result, candidate in boosts:
        if len([c for _, c in picked if c.get("boost")]) >= DAYFUN_BOOST_LEGS:
            break
        if len(picked) >= DAYFUN_MAX_LEGS:
            break
        fid = str(result["fixture"].get("id"))
        if fid in seen:
            continue
        seen.add(fid)
        picked.append((result, candidate))
    if len(picked) < DAYFUN_HARD_MIN:
        return []
    legs = finish_legs(picked)
    if ticket_fair(legs) < DAYFUN_TARGET_ODDS:
        REASONS["Fun journée : cote cible non atteinte"] += 1
        return []
    return legs


def ticket_chance(legs):
    chance = 1.0
    for leg in legs:
        chance *= leg["prob"]
    return chance if legs else 0.0


def ticket_fair(legs):
    product = 1.0
    for leg in legs:
        product *= leg["prob"]
    return 1 / product if product > 0 else 0


def leg_odds(leg):
    """6.8.0 : vraie cote si connue, sinon cote juste du bot."""
    odds = leg.get("odds")
    return odds if odds else 1 / leg["prob"]


def ticket_total(legs):
    """6.8.0 : cote totale avec les vraies cotes quand elles existent."""
    product = 1.0
    for leg in legs:
        product *= leg_odds(leg)
    return product if legs else 0


def ticket_estimated(legs):
    """True si au moins une jambe est à la cote juste (estimation)."""
    return any(not leg.get("real") for leg in legs)


def best_combo(pool):
    """Meilleure combinaison de TICKET_LEGS matchs différents : la mieux
    notée parmi celles à cote juste >= cible, sinon la mieux notée."""
    best_ok = None
    best_any = None

    for combo in itertools.combinations(pool, TICKET_LEGS):
        fixtures = {str(r["fixture"].get("id")) for r, _ in combo}
        if len(fixtures) < TICKET_LEGS:
            continue

        # 6.8.0 : cible testée sur les vraies cotes (cote juste en secours)
        fair = 1.0
        for _, candidate in combo:
            fair *= leg_odds(candidate)
        mean_score = sum(c["score"] for _, c in combo) / TICKET_LEGS

        if fair >= TICKET_TARGET_ODDS:
            if best_ok is None or mean_score > best_ok[0]:
                best_ok = (mean_score, combo)
        # 6.4.0 : repli sur la combinaison la mieux notée (avant : la plus
        # grosse cote, donc la plus risquée)
        if best_any is None or mean_score > best_any[0]:
            best_any = (mean_score, combo)

    if best_ok is not None:
        combo, ok = best_ok[1], True
    elif best_any is not None:
        combo, ok = best_any[1], False
    else:
        return []

    return [{"result": r, **c, "_ok": ok} for r, c in combo]


def ordinal(rank):
    return "1er" if rank == 1 else f"{rank}e"


def season_is_current(season, today):
    """'2026/27' ou '2026' -> saison en cours ? (inconnu -> True)."""
    years = [int(y) for y in re.findall(r"(?<!\d)(\d{4})(?!\d)", str(season or ""))]
    short = re.match(r"^\s*(\d{4})\s*[/-]\s*(\d{2})\s*$", str(season or ""))
    if not years:
        return True
    if short or len(years) >= 2:
        # saison à cheval : commence en juillet/août
        start = years[0]
        expected = today.year if today.month >= 7 else today.year - 1
        return start >= expected
    year = years[0]
    return year == today.year or (today.month <= 2 and year == today.year - 1)


def standings_ok(fixture, today=None):
    """6.5.0 : le classement lu est-il bien celui d'un vrai championnat
    pour CE match ? Sinon (coupe, sélections, groupes, vieille saison,
    tableau agrégé), on n'affiche ni enjeu ni classement."""
    today = today or datetime.now(PARIS).date()
    league_id = fixture.get("league_id")
    key = str(league_id)
    if league_id is None:
        return False

    names = [str(fixture.get("league_name") or ""), league_label(fixture) or ""]
    if fixture.get("stage"):
        return False
    if any(NON_LEAGUE_PATTERN.search(name) for name in names if name):
        return False

    if key not in STANDINGS_CACHE:
        if not budget_available():
            return False
        try:
            standings_row(league_id, fixture.get("home_id"))
        except Exception as exc:
            error(f"Classement illisible : {type(exc).__name__} {exc}")
            return False

    rows = [r for r in STANDINGS_CACHE.get(key, []) if isinstance(r, dict)]
    if not STANDINGS_MIN_SIZE <= len(rows) <= STANDINGS_MAX_SIZE:
        return False

    meta = STANDINGS_META.get(key) or {}
    fmt = meta.get("format")
    if fmt is not None and str(fmt).lower() != "league":
        return False
    if meta.get("stage"):
        return False
    if isinstance(meta.get("groups"), list) and meta["groups"]:
        return False
    if any(r.get("group") for r in rows):
        return False
    league = meta.get("league")
    if isinstance(league, dict) and NON_LEAGUE_PATTERN.search(str(league.get("name") or "")):
        return False
    if not season_is_current(meta.get("season"), today):
        return False

    for side in ("home_id", "away_id"):
        row = next(
            (r for r in rows if isinstance(r.get("team"), dict)
             and str(r["team"].get("id")) == str(fixture.get(side))),
            None,
        )
        if row is None:
            return False
        played = first_number(row, "played", "games", "matches_played")
        if played is not None and played < STANDINGS_MIN_PLAYED:
            return False
        # Forme du tableau trop ancienne -> tableau d'une autre saison
        form = row.get("form")
        if isinstance(form, list) and form:
            dates = [date_of(m) for m in form if isinstance(m, dict)]
            dates = [d for d in dates if d is not None]
            if dates and (today - max(dates)).days > MAX_STALE_DAYS:
                return False
    return True


def motivation_of(fixture):
    """Enjeu des deux équipes d'après le classement (standings/{ligue}).

    Renvoie {"home": libellé|None, "away": libellé|None}. Libellés :
    "titre/Europe", "maintien", "milieu". Rien si la ligue n'a pas de
    classement (coupe, amical) ou si le budget API est épuisé.
    """
    result = {"home": None, "away": None}
    league_id = fixture.get("league_id")
    if league_id is None:
        return result
    # 6.5.0 : pas d'enjeu si ce n'est pas un vrai championnat
    if not standings_ok(fixture):
        return result

    for side in ("home", "away"):
        if not budget_available() and str(league_id) not in STANDINGS_CACHE:
            return result
        try:
            row = standings_row(league_id, fixture.get(f"{side}_id"))
        except Exception as exc:  # données inattendues : on ignore
            error(f"Classement illisible : {type(exc).__name__} {exc}")
            return result
        if not isinstance(row, dict):
            continue
        rank = first_number(row, "position", "rank", "pos", "place")
        size = len(STANDINGS_CACHE.get(str(league_id), []))
        if rank is None or size < 8:
            continue
        if rank <= TOP_ZONE:
            result[side] = "titre/Europe"
        elif rank > size - RELEGATION_ZONE:
            result[side] = "maintien"
        else:
            result[side] = "milieu"
    return result


def motivation_adjust(market, motivation):
    """Petit bonus si l'équipe concernée joue quelque chose, malus si elle
    est en milieu de tableau face à une équipe sous pression."""
    side = None
    if market in ("home", "home15", "win_home"):
        side, other = "home", "away"
    elif market in ("away", "away15", "win_away"):
        side, other = "away", "home"
    if side is None:
        return 0.0
    mine = motivation.get(side)
    theirs = motivation.get(other)
    if mine in ("titre/Europe", "maintien"):
        return MOTIVATION_BONUS
    if mine == "milieu" and theirs in ("titre/Europe", "maintien"):
        return -MOTIVATION_BONUS
    return 0.0


def motivation_text(motivation):
    labels = {"titre/Europe": "🔥 titre/Europe", "maintien": "🆘 maintien",
              "milieu": "😐 milieu"}
    home = labels.get(motivation.get("home"))
    away = labels.get(motivation.get("away"))
    if not home and not away:
        return ""
    return f"{home or '?'} | {away or '?'}"


def standing_text(fixture):
    """Classement des deux équipes (si l'API le fournit), sinon chaîne vide."""
    league_id = fixture.get("league_id")
    texts = []

    # 6.5.0 : pas de "2e/1er sur 64" tiré d'un tableau de coupe
    if not standings_ok(fixture):
        return ""

    for side in ("home_id", "away_id"):
        if not budget_available():
            return ""
        row = standings_row(league_id, fixture.get(side))
        if not isinstance(row, dict):
            return ""
        rank = first_number(row, "position", "rank", "pos", "place")
        points = first_number(row, "points", "pts")
        if rank is None:
            return ""
        text = ordinal(int(rank))
        if points is not None:
            text += f" ({int(points)} pts)"
        texts.append(text)

    size = len(STANDINGS_CACHE.get(str(league_id), []))
    suffix = f" sur {size}" if size else ""
    return f"{texts[0]} · {texts[1]}{suffix}"


# ---------------- TIRS CADRÉS ----------------

def first_number(row, *keys):
    for key in keys:
        value = number(row.get(key))
        if value is not None:
            return value
    return None


def extract_sot_from_stats(stats, home_id, away_id):
    home_sot = None
    away_sot = None

    if not isinstance(stats, dict):
        return home_sot, away_sot

    # Format 1 : dictionnaire home / away
    if isinstance(stats.get("home"), dict) and isinstance(stats.get("away"), dict):
        home_sot = first_number(
            stats["home"], "shots_on_target", "shotsOnTarget"
        )
        away_sot = first_number(
            stats["away"], "shots_on_target", "shotsOnTarget"
        )

        if home_sot is not None and away_sot is not None:
            return home_sot, away_sot

    # Format 2 : liste statistics
    rows = stats.get("statistics")
    if not isinstance(rows, list):
        rows = stats.get("data")

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            team = row.get("team")
            if isinstance(team, dict):
                team_id = str(team.get("id"))
            else:
                team_id = str(row.get("team_id") or row.get("teamId"))

            value = first_number(
                row, "shots_on_target", "shotsOnTarget", "sot"
            )

            if value is None:
                continue

            if team_id == str(home_id):
                home_sot = value
            elif team_id == str(away_id):
                away_sot = value

        # 6.4.0 : une seule équipe reconnue et exactement deux lignes ->
        # l'autre ligne est forcément l'adversaire
        if (home_sot is None) != (away_sot is None):
            values = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                team = row.get("team")
                tid = str(team.get("id")) if isinstance(team, dict) else str(
                    row.get("team_id") or row.get("teamId")
                )
                value = first_number(row, "shots_on_target", "shotsOnTarget", "sot")
                if value is not None:
                    values.append((tid, value))
            if len(values) == 2:
                if home_sot is None:
                    others = [v for t, v in values if t != str(away_id)]
                    home_sot = others[0] if len(others) == 1 else None
                else:
                    others = [v for t, v in values if t != str(home_id)]
                    away_sot = others[0] if len(others) == 1 else None

    return home_sot, away_sot


def match_sot(fixture):
    """Renvoie (tirs cadrés domicile, tirs cadrés extérieur),
    associés aux identifiants réels des équipes."""
    cache = load_sot_cache()
    fixture_id = str(fixture.get("id"))
    home_id = str(fixture.get("home_id"))
    away_id = str(fixture.get("away_id"))

    if fixture_id in cache:
        value = cache[fixture_id]
        if isinstance(value, list) and len(value) == 2:
            return tuple(value)

    payload = api_get(f"fixtures/{fixture_id}/stats")
    if not isinstance(payload, dict):
        return None

    stats = payload.get("data")
    if not isinstance(stats, dict):
        stats = payload

    home_sot, away_sot = extract_sot_from_stats(stats, home_id, away_id)

    if home_sot is None or away_sot is None:
        return None

    cache[fixture_id] = [home_sot, away_sot]
    save_sot_cache()
    return home_sot, away_sot


def sot_history(history, team_id):
    """Tirs cadrés pour / contre de l'équipe sur ses derniers matchs.
    Renvoie aussi le lieu de chaque valeur (domicile / extérieur)."""
    sot_for = []
    sot_against = []
    venues = []

    for match in history[:SOT_HISTORY_N]:
        match_id = match.get("id")
        if not match_id:
            continue

        # 6.4.0 : vrais identifiants du match historique. Avant, on prenait
        # ceux du match du jour : l'adversaire n'était jamais le bon.
        home_id = match.get("home_id")
        away_id = match.get("away_id")
        if home_id is None and away_id is None:
            home_id = team_id if match["venue"] == "home" else None
            away_id = team_id if match["venue"] == "away" else None

        historical_fixture = {
            "id": match_id,
            "home_id": home_id,
            "away_id": away_id,
        }

        result = match_sot(historical_fixture)
        if result is None:
            continue

        home_sot, away_sot = result

        if match["venue"] == "home":
            sot_for.append(home_sot)
            sot_against.append(away_sot)
        else:
            sot_for.append(away_sot)
            sot_against.append(home_sot)
        venues.append(match["venue"])

    return sot_for, sot_against, venues


def weighted_sot(values):
    if not values:
        return None

    weights = [DECAY ** i for i in range(len(values))]
    total = sum(weights)

    return sum(w * v for w, v in zip(weights, values)) / total


def sot_profile(history, venue, today, team_id):
    recent = history[:HISTORY_N]
    if len(recent) < MIN_HISTORY:
        return None

    if (today - recent[0]["date"]).days > MAX_STALE_DAYS:
        return None

    sot_for, sot_against, venues = sot_history(recent, team_id)

    if len(sot_for) < 3:
        return None

    overall_for = weighted_sot(sot_for)
    overall_against = weighted_sot(sot_against)

    local_for = []
    local_against = []

    # 6.4.0 : on suit le lieu renvoyé par sot_history (avant, le zip avec
    # "recent" se décalait dès qu'un match sans stats était sauté)
    for place, for_value, against_value in zip(venues, sot_for, sot_against):
        if place == venue:
            local_for.append(for_value)
            local_against.append(against_value)

    if len(local_for) >= 3:
        venue_weight = min(0.45, len(local_for) / (len(local_for) + 6))
        for_value = weighted_sot(local_for)
        against_value = weighted_sot(local_against)
    else:
        venue_weight = 0
        for_value = overall_for
        against_value = overall_against

    n = len(sot_for)

    result = {
        "sot_for": (
            (1 - venue_weight) * overall_for
            + venue_weight * for_value
        ),
        "sot_against": (
            (1 - venue_weight) * overall_against
            + venue_weight * against_value
        ),
    }

    for key, prior in (("sot_for", 3.2), ("sot_against", 3.2)):
        result[key] = (
            result[key] * n + prior * PRIOR_K
        ) / (n + PRIOR_K)

    result["sot_n"] = n
    return result


def sot_probability(team, opponent):
    lambda_sot = (team["sot_for"] + opponent["sot_against"]) / 2
    lambda_sot = max(lambda_sot, 0.30)

    # P(X >= 3) pour la ligne "plus de 2,5"
    return 1 - math.exp(-lambda_sot) * (
        1 + lambda_sot + lambda_sot ** 2 / 2
    )


# ---------------- COTE MINIMALE ----------------

def min_odds(probability):
    """Cote minimale à exiger : value bet (marge 3 %) et jamais sous
    MIN_LEG_ODDS."""
    if probability is None or probability <= 0:
        return None
    value = math.ceil(ODDS_MARGIN / probability * 100) / 100
    return max(value, MIN_LEG_ODDS)


def fmt_odds(value):
    return f"{value:.2f}".replace(".", ",")


# ---------------- JOURNAL / CALIBRATION ----------------

OLD_FIELDS = [
    "date", "fixture_id", "match", "league", "kickoff",
    "market", "player", "probability", "model_version", "result",
]


def read_predictions():
    old_backup = PREDICTIONS_FILE.replace(".csv", "_ancien.csv")
    if os.path.exists(old_backup):
        recovered = read_old_format(old_backup)
        if recovered:
            current = read_predictions_file()
            known = {
                (r["date"], r["fixture_id"], r["market"], r["player"])
                for r in current
            }
            current += [
                r for r in recovered
                if (r["date"], r["fixture_id"], r["market"], r["player"])
                not in known
            ]
            write_predictions(current)
            os.replace(old_backup, old_backup + ".repris")
    return read_predictions_file()


def read_old_format(path):
    with open(path, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != OLD_FIELDS:
            return []
        rows = list(reader)
    for row in rows:
        row["prob_base"] = row.get("probability", "")
        row["published"] = 1
    return [{k: r.get(k, "") for k in FIELDS} for r in rows]


def read_predictions_file():
    if not os.path.exists(PREDICTIONS_FILE):
        return []
    with open(PREDICTIONS_FILE, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        header = reader.fieldnames
        rows = list(reader)

    if header == FIELDS:
        return rows

    if header == OLD_FIELDS:
        for row in rows:
            row["prob_base"] = row.get("probability", "")
            row["published"] = 1
        return [{k: r.get(k, "") for k in FIELDS} for r in rows]

    # 6.4.0 : journal 6.3 (ou colonnes connues) -> on complète les colonnes
    # manquantes au lieu de repartir de zéro
    if header and set(header) <= set(FIELDS) and "fixture_id" in header:
        return [{k: (r.get(k) or "") for k in FIELDS} for r in rows]

    # En-tête vraiment inconnu : copie de sécurité datée, jamais écrasée
    stamp = datetime.now(PARIS).strftime("%Y%m%d_%H%M%S")
    backup = PREDICTIONS_FILE.replace(".csv", f"_inconnu_{stamp}.csv")
    os.replace(PREDICTIONS_FILE, backup)
    error(f"Journal : en-tete inconnu, fichier mis de cote ({backup}).")
    return []


def write_predictions(rows):
    os.makedirs(os.path.dirname(PREDICTIONS_FILE), exist_ok=True)
    # Écriture atomique : un plantage pendant l'écriture ne casse pas le CSV
    temp = PREDICTIONS_FILE + ".tmp"
    with open(temp, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, PREDICTIONS_FILE)


def outcome(market, home_score, away_score):
    if market == "home":
        return home_score > 0
    if market == "away":
        return away_score > 0
    if market == "btts":
        return home_score > 0 and away_score > 0
    if market == "over15":
        return home_score + away_score >= 2
    if market == "over25":
        return home_score + away_score >= 3
    if market == "win_home":
        return home_score > away_score
    if market == "win_away":
        return away_score > home_score
    if market == "home15":
        return home_score >= 2
    if market == "away15":
        return away_score >= 2
    return None


def settle_previous_predictions(today):
    rows = read_predictions()
    pending = []

    for row in rows:
        if row.get("result") not in ("pending", "", None):
            continue
        try:
            row["_date"] = datetime.fromisoformat(row["date"]).date()
        except (ValueError, KeyError):
            continue
        if row["_date"] < today:
            pending.append(row)

    if not pending:
        for row in rows:
            row.pop("_date", None)
        return

    dates = sorted(
        {
            r["_date"] for r in pending
            if (today - r["_date"]).days <= PENDING_EXPIRY_DAYS + 1
        },
        reverse=True,
    )[:MAX_SETTLE_DATES]
    finished = {}
    dead = set()

    for day in dates:
        endpoint = f"fixtures/date/{day.isoformat()}"
        payload = api_get(endpoint, force_refresh=True)
        if payload is None:
            day_disk = read_json(day_cache_path(day), {})
            cached_data = day_disk.get("data") if isinstance(day_disk.get("data"), dict) else {}
            fallback = cached_data.get(endpoint)
            if isinstance(fallback, dict):
                payload = fallback
        for fixture in get_list(payload):
            if not isinstance(fixture, dict):
                continue
            status = str(fixture.get("status") or "").upper()
            identifier = str(fixture.get("id"))
            if status in FINISHED_STATUSES:
                home = number(fixture.get("home_score"))
                away = number(fixture.get("away_score"))
                if home is not None and away is not None:
                    finished[identifier] = (home, away)
            elif status in {"POSTPONED", "CANCELLED", "CANCELED",
                            "ABANDONED", "SUSPENDED"}:
                dead.add(identifier)

    for row in pending:
        identifier = str(row.get("fixture_id"))
        age = (today - row["_date"]).days

        if identifier in dead:
            row["result"] = "annule"
            continue

        if identifier not in finished:
            if age > PENDING_EXPIRY_DAYS:
                row["result"] = "expire"
            continue

        home_score, away_score = finished[identifier]
        market = row.get("market")

        if market in ("sot_home", "sot_away"):
            sot = match_sot({
                "id": identifier,
                "home_id": row.get("home_id") or None,
                "away_id": row.get("away_id") or None,
            })

            if sot is None:
                if age > PENDING_EXPIRY_DAYS:
                    row["result"] = "expire"
                continue

            home_sot, away_sot = sot
            value = home_sot if market == "sot_home" else away_sot

            row["result"] = "gagne" if value > SOT_LINE else "perdu"
            continue

        won = outcome(market, home_score, away_score)
        if won is None:
            continue
        row["result"] = "gagne" if won else "perdu"

    for row in rows:
        row.pop("_date", None)
    write_predictions(rows)

    settled = [r for r in rows if r.get("result") in ("gagne", "perdu")]
    if settled:
        wins = sum(r["result"] == "gagne" for r in settled)
        print(f"JOURNAL : {wins}/{len(settled)} gagnes au total.", flush=True)


# ---------------- BILAN (ROI / CALIBRATION) ----------------

MARKET_SHORT = {
    "home": "Dom +0,5", "away": "Ext +0,5", "btts": "BTTS",
    "over15": "+1,5 buts", "over25": "+2,5 buts", "win_home": "Vict. dom",
    "win_away": "Vict. ext", "home15": "Dom +1,5", "away15": "Ext +1,5",
    "sot_home": "TC dom", "sot_away": "TC ext",
}


def parse_odds(value):
    """'1,45' ou '1.45' -> 1.45 ; vide ou absurde -> None."""
    odds = number(str(value or "").strip().replace(",", "."))
    return odds if odds is not None and odds > 1 else None


def version_tuple(value):
    parts = []
    for piece in str(value or "").split(".")[:2]:
        try:
            parts.append(int(piece))
        except ValueError:
            return (0, 0)
    return tuple(parts) if len(parts) == 2 else (0, 0)


def ticket_kinds(row):
    """6.9.0 : tickets d'une ligne (avant 6.9.0 : published = 2 -> jour)."""
    tags = [t for t in str(row.get("tickets") or "").split(";") if t]
    if tags:
        return tags
    if str(row.get("published")) == "2" and version_tuple(row.get("model_version")) < TICKET_TAGS_SINCE:
        return ["jour"]
    return []


def tickets_summary(rows, today, days=STATS_DAYS):
    """6.9.0 : combinés gagnés / joués par type de ticket (perdu dès qu'une
    sélection perd ; en attente tant qu'une sélection n'est pas réglée)."""
    legs = {}
    for row in rows:
        kinds = ticket_kinds(row)
        if not kinds:
            continue
        try:
            day = datetime.fromisoformat(str(row.get("date"))).date()
        except ValueError:
            continue
        if not 0 <= (today - day).days <= days:
            continue
        for kind in kinds:
            legs.setdefault((day, kind), []).append(row.get("result"))
    out = {}
    for (_, kind), results in legs.items():
        if "perdu" in results:
            won = False
        elif results and all(r == "gagne" for r in results):
            won = True
        else:
            continue
        entry = out.setdefault(kind, {"n": 0, "wins": 0})
        entry["n"] += 1
        entry["wins"] += won
    return out


def tickets_line(summary):
    tickets = (summary or {}).get("tickets") or {}
    parts = [
        f"{TICKET_KIND_LABELS[k]} {tickets[k]['wins']}/{tickets[k]['n']}"
        for k in ("jour", "mega", "dayfun", "safe") if tickets.get(k, {}).get("n")
    ]
    return f"🎟️ Tickets {STATS_DAYS}j : " + " · ".join(parts) if parts else ""


def journal_summary(rows, today, days=STATS_DAYS):
    """Bilan des lignes réglées (gagne / perdu) sur les `days` derniers jours.

    - par marché, pronostics publiés (1) et ticket (2) séparés : réussite,
      proba moyenne annoncée, ROI réel pour une mise de 1 quand
      odds_taken ou odds_market est disponible (jamais de cote estimée)
    - calibration de TOUTES les lignes journalisées par tranche de 10 %
    - score de Brier (0 = parfait, 0,25 = pile ou face)
    """
    groups = {"publie": {}, "ticket": {}}
    buckets = {}
    brier_sum = 0.0
    brier_n = 0
    real_odds = 0
    real_published = 0

    for row in rows:
        if row.get("result") not in ("gagne", "perdu"):
            continue
        try:
            day = datetime.fromisoformat(str(row.get("date"))).date()
            probability = float(row.get("probability"))
        except (TypeError, ValueError):
            continue
        if not 0 < probability < 1 or not 0 <= (today - day).days <= days:
            continue

        won = row["result"] == "gagne"
        brier_sum += (probability - won) ** 2
        brier_n += 1

        bucket = min(int(probability * 10), 9) / 10
        stats = buckets.setdefault(bucket, [0, 0, 0.0])
        stats[0] += 1
        stats[1] += won
        stats[2] += probability

        published = str(row.get("published"))
        if published not in ("1", "2"):
            continue

        # 6.8.0 : bilan des pronos = marchés publiés aujourd'hui seulement
        if published == "1" and row.get("market") not in CURRENT_PUBLISHED_MARKETS:
            continue
        # 6.8.1 : anciens tickets Dom/Ext +1,5 hors bilan
        if row.get("market") in REMOVED_MARKETS:
            continue

        # ROI uniquement sur une cote réelle ; le taux de réussite reste
        # calculable même quand la cote n'a pas été enregistrée.
        odds = parse_odds(row.get("odds_taken")) or parse_odds(row.get("odds_market"))
        if odds is not None:
            real_odds += 1
            if published == "1":
                real_published += 1
        group = groups["publie" if published == "1" else "ticket"]
        entry = group.setdefault(row.get("market"), [0, 0, 0.0, 0.0, 0])
        entry[0] += 1
        entry[1] += won
        entry[3] += probability
        if odds is not None:
            entry[2] += (odds - 1) if won else -1
            entry[4] += 1

    def finish(group):
        return {
            m: {"n": n, "wins": w, "hit": w / n,
                "roi": profit / odds_n if odds_n else None,
                "profit": profit, "real_odds_n": odds_n, "pred": p / n}
            for m, (n, w, profit, p, odds_n) in group.items()
        }

    return {
        "markets": finish(groups["publie"]),
        "ticket_markets": finish(groups["ticket"]),
        "calibration": {
            b: {"n": n, "hit": w / n, "pred": p / n}
            for b, (n, w, p) in sorted(buckets.items())
        },
        "brier": brier_sum / brier_n if brier_n else None,
        "n": brier_n,
        "real_odds": real_odds,
        "real_published": real_published,
        "tickets": tickets_summary(rows, today, days),
    }


def summary_lines(summary):
    """Bloc Telegram : bilan des pronostics publiés sur STATS_DAYS jours."""
    if not summary or not summary.get("markets"):
        return []
    lines = [f"📒 BILAN {STATS_DAYS} JOURS (pronos publiés, mise 1)"]
    total_n = total_w = 0
    total_profit = 0.0
    total_real_odds = 0
    ordered = sorted(summary["markets"].items(), key=lambda i: -i[1]["n"])
    for market, s in ordered:
        total_n += s["n"]
        total_w += s["wins"]
        total_profit += s["profit"]
        total_real_odds += s.get("real_odds_n", 0)
        roi = "n/a" if s["roi"] is None else f"{s['roi']:+.0%}"
        lines.append(
            f"• {MARKET_SHORT.get(market, market)} : {s['wins']}/{s['n']}"
            f" ({s['hit']:.0%}, annoncé {s['pred']:.0%})"
            f" · ROI réel {roi}"
        )
    total_roi = total_profit / total_real_odds if total_real_odds else None
    total_roi_text = "n/a" if total_roi is None else f"{total_roi:+.0%}"
    lines.append(
        f"Total : {total_w}/{total_n} ({total_w / total_n:.0%})"
        f" · ROI réel {total_roi_text} ({total_real_odds} cotes)"
    )
    if tickets_line(summary):
        lines.append(tickets_line(summary))
    if summary.get("brier") is not None:
        lines.append(
            f"Brier : {summary['brier']:.3f} (0,25 = pile ou face)".replace(".", ",")
        )
    if not summary.get("real_odds"):
        lines.append(
            "ℹ️ ROI réel indisponible : renseigne les cotes dans odds_taken "
            "ou conserve odds_market pour les marchés concernés."
        )
    if total_n < 30:
        lines.append(f"⚠️ {total_n} paris seulement : échantillon trop petit.")
    lines.append("")
    return lines


def write_summary_file(summary, today):
    """Écrit data/bilan.csv (un tableau lisible dans Excel / Sheets)."""
    os.makedirs(os.path.dirname(SUMMARY_FILE), exist_ok=True)
    temp = SUMMARY_FILE + ".tmp"
    with open(temp, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["date_bilan", "type", "cle", "n", "gagnes",
                         "reussite", "proba_annoncee", "roi"])
        for kind in ("markets", "ticket_markets"):
            label = "publie" if kind == "markets" else "ticket"
            for market, s in sorted(summary[kind].items()):
                writer.writerow([today.isoformat(), label, market, s["n"],
                                 s["wins"], f"{s['hit']:.3f}",
                                 f"{s['pred']:.3f}",
                                 "" if s["roi"] is None else f"{s['roi']:.3f}"])
        for tkind, s in sorted((summary.get("tickets") or {}).items()):
            n = s.get("n") or 0
            w = s.get("wins") or 0
            if n:
                writer.writerow([today.isoformat(), "ticket_combo", tkind, n,
                                 w, f"{w / n:.3f}", "", ""])
        for bucket, s in summary["calibration"].items():
            writer.writerow([today.isoformat(), "calibration",
                             f"{bucket:.1f}-{bucket + 0.1:.1f}", s["n"], "",
                             f"{s['hit']:.3f}", f"{s['pred']:.3f}", ""])
        if summary["brier"] is not None:
            writer.writerow([today.isoformat(), "brier", "", summary["n"],
                             "", "", "", f"{summary['brier']:.4f}"])
    os.replace(temp, SUMMARY_FILE)


def print_summary(summary):
    print("\n===== BILAN JOURNAL =====", flush=True)
    for kind in ("markets", "ticket_markets"):
        for market, s in summary[kind].items():
            roi = "n/a" if s["roi"] is None else f"{s['roi']:+.3f}"
            print(
                f"{kind} {market}: {s['wins']}/{s['n']} hit={s['hit']:.2f}"
                f" pred={s['pred']:.2f} roi={roi}",
                flush=True,
            )
    for bucket, s in summary["calibration"].items():
        gap = s["hit"] - s["pred"]
        print(
            f"tranche {bucket:.0%}-{bucket + 0.1:.0%} : prevu {s['pred']:.2f}"
            f" / reel {s['hit']:.2f} (ecart {gap:+.2f}, n={s['n']})",
            flush=True,
        )
    if summary["brier"] is not None:
        print(f"Brier : {summary['brier']:.4f} sur {summary['n']} lignes", flush=True)


# ---------------- INFOS PAR PICK (6.6.0) ----------------

def norm_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def last_name(value):
    tokens = [t for t in norm_text(value).split() if len(t) > 1]
    return tokens[-1] if tokens else ""


def team_details(history, venue, today):
    """Stats sans appel en plus, à partir de l'historique déjà chargé."""
    if not history:
        return {}
    details = {}
    local = [m for m in history if m["venue"] == venue][:5]
    if len(local) >= 3:
        details["venue_n"] = len(local)
        details["venue_gf"] = sum(m["gf"] for m in local) / len(local)
        details["venue_ga"] = sum(m["ga"] for m in local) / len(local)
    last = history[:10]
    if len(last) >= 5:
        details["n"] = len(last)
        details["clean_sheets"] = sum(m["ga"] == 0 for m in last) / len(last)
        details["failed_to_score"] = sum(m["gf"] == 0 for m in last) / len(last)
    rest = (today - history[0]["date"]).days
    if 0 < rest <= 60:
        details["rest"] = rest
    return details


def market_side(market):
    if market in ("home", "home15", "win_home", "sot_home"):
        return "home"
    if market in ("away", "away15", "win_away", "sot_away"):
        return "away"
    return None


def h2h_info(fixture):
    """5 derniers face-à-face (point de vue du match du jour)."""
    home_id, away_id = fixture.get("home_id"), fixture.get("away_id")
    payload = api_get(f"fixtures/h2h/{home_id}/{away_id}")
    rows = []
    for match in get_list(payload):
        if not isinstance(match, dict):
            continue
        if str(match.get("status") or "").upper() not in ("FT", "AET", "PENS"):
            continue
        hs, as_ = number(match.get("home_score")), number(match.get("away_score"))
        if hs is None or as_ is None:
            continue
        if str(match.get("home_id")) == str(home_id):
            gh, ga = hs, as_
        elif str(match.get("home_id")) == str(away_id):
            gh, ga = as_, hs
        else:
            continue
        rows.append((str(match.get("date") or ""), int(gh), int(ga)))
    rows.sort(reverse=True)
    rows = rows[:H2H_N]
    if len(rows) < 3:
        return None
    return {
        "n": len(rows),
        "home_scored": sum(gh > 0 for _, gh, _ in rows),
        "away_scored": sum(ga > 0 for _, _, ga in rows),
        "over15": sum(gh + ga >= 2 for _, gh, ga in rows),
        "scores": [f"{gh}-{ga}" for _, gh, ga in rows],
    }


def top_scorer(league_id, team_id):
    payload = api_get(f"leagues/{league_id}/topscorers?limit=100")
    best = None
    for row in get_list(payload):
        if not isinstance(row, dict) or str(row.get("team_id")) != str(team_id):
            continue
        goals = number(row.get("goals"))
        if goals is None or goals < TOPSCORER_MIN_GOALS:
            continue
        if best is None or goals > best[1]:
            best = (row.get("player_name"), goals)
    return best


def last_match(history, today):
    """Dernier match de l'équipe avec un identifiant, s'il est récent."""
    for match in history[:1]:
        if match.get("id") and (today - match["date"]).days <= LINEUP_MAX_AGE_DAYS:
            return match
    return None


def lineup_names(match):
    """Noms des joueurs ayant joué le dernier match (titulaires + entrés)."""
    payload = api_get(f"fixtures/{match['id']}/lineups")
    if not isinstance(payload, dict) or payload.get("available") is False:
        return None
    data = payload.get("data")
    side = data.get(match["venue"]) if isinstance(data, dict) else None
    if not isinstance(side, dict):
        return None
    names = set()
    starters = side.get("starters") if isinstance(side.get("starters"), list) else []
    subs = side.get("subs") if isinstance(side.get("subs"), list) else []
    for player in starters:
        if isinstance(player, dict):
            names.add(last_name(player.get("name")))
    for player in subs:
        if isinstance(player, dict) and (number(player.get("mins_played")) or 0) > 0:
            names.add(last_name(player.get("name")))
    names.discard("")
    return names if len(starters) >= 7 else None


def red_cards_last_match(match):
    payload = api_get(f"fixtures/{match['id']}/events")
    players = []
    for event in get_list(payload):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_type") or "").lower()
        if "red" in kind and str(event.get("team_side")) == match["venue"]:
            if event.get("player_name"):
                players.append(str(event["player_name"]))
    return players


def enrich_pick(result, today):
    """Infos + alertes pour un pick publié. Ne renvoie que des données
    réellement lues ; chaque bloc est sauté si le budget manque."""
    fixture = result["fixture"]
    market = shown_picks(result)[0][0] if shown_picks(result) else None
    side = market_side(market)
    info = {"alerts": []}
    home_hist = result.get("home_history") or []
    away_hist = result.get("away_history") or []
    info["home"] = team_details(home_hist, "home", today)
    info["away"] = team_details(away_hist, "away", today)
    names = {"home": short_name(fixture.get("home_team")),
             "away": short_name(fixture.get("away_team"))}

    # Repos court (seulement l'équipe sur laquelle on parie)
    for s in ([side] if side else ["home", "away"]):
        rest = info[s].get("rest")
        if rest is not None and rest <= REST_ALERT_DAYS:
            info["alerts"].append(f"{display_name(names[s])} {rest} j de repos")

    # H2H
    if budget_available():
        h2h = h2h_info(fixture)
        if h2h:
            info["h2h"] = h2h
            if side in ("home", "away") and market in ("home", "away"):
                scored = h2h[f"{side}_scored"]
                if scored <= H2H_ALERT_MAX:
                    info["alerts"].append(
                        f"H2H : {display_name(names[side])} a marqué {scored}/{h2h['n']}"
                    )
            elif market == "over15" and h2h["over15"] <= H2H_ALERT_MAX:
                info["alerts"].append(f"H2H : +1,5 buts {h2h['over15']}/{h2h['n']}")

    # Buteur absent / expulsé au dernier match (indices, à vérifier)
    for s in ([side] if side else []):
        if not budget_available():
            break
        team_id = fixture.get(f"{s}_id")
        history = home_hist if s == "home" else away_hist
        match = last_match(history, today)
        if match is None:
            continue
        scorer = top_scorer(fixture.get("league_id"), team_id)
        if scorer and budget_available():
            played = lineup_names(match)
            if played is not None and last_name(scorer[0]) not in played:
                info["scorer_absent"] = scorer
                info["alerts"].append(
                    f"{player_short(scorer[0])} absent dernier match (à vérifier)"
                )
        if budget_available():
            reds = red_cards_last_match(match)
            if reds:
                info["reds"] = reds
                info["alerts"].append(
                    ", ".join(player_short(p) for p in reds) + " expulsé dernier match"
                )
    return info


def shown_picks(result):
    return (result.get("picks") or [])[:SHOWN_MARKETS]


def fr1(value):
    return f"{value:.1f}".replace(".", ",")


def detail_lines(result, info):
    """Lignes détaillées pour la fiche (fichier), jamais pour Telegram."""
    fixture = result["fixture"]
    lines = []
    for s, label in (("home", "Dom"), ("away", "Ext")):
        d = info.get(s) or {}
        name = short_name(fixture.get(f"{s}_team"))
        parts = []
        if "venue_gf" in d:
            parts.append(
                f"{fr1(d['venue_gf'])} marqués / {fr1(d['venue_ga'])} encaissés "
                f"({d['venue_n']} derniers {'à domicile' if s == 'home' else 'à l’extérieur'})"
            )
        if "clean_sheets" in d:
            parts.append(f"clean sheets {d['clean_sheets']:.0%}")
            parts.append(f"sans marquer {d['failed_to_score']:.0%} ({d['n']} m.)")
        if "rest" in d:
            parts.append(f"repos {d['rest']} j")
        if parts:
            lines.append(f"{label} {name} : " + " · ".join(parts))
    h2h = info.get("h2h")
    if h2h:
        lines.append(
            f"H2H ({h2h['n']}) : {' '.join(h2h['scores'])} · dom a marqué "
            f"{h2h['home_scored']}/{h2h['n']} · ext {h2h['away_scored']}/{h2h['n']}"
        )
    if info.get("scorer_absent"):
        name, goals = info["scorer_absent"]
        lines.append(f"Buteur {name} ({int(goals)} buts) absent de la compo du dernier match (à vérifier)")
    if info.get("reds"):
        lines.append(f"Expulsé(s) au dernier match : {', '.join(info['reds'])} (suspension possible)")
    for market, price in (info.get("odds") or {}).items():
        lines.append(f"Cote marché {market} : {fmt_odds(price)}")
    return lines


# ---------------- COTES RÉELLES (optionnel, 6.6.0) ----------------

ODDS_STATE = {"sports": None, "events": {}}


def odds_get(path, params):
    """Appel The Odds API (jamais sans clé). Renvoie (json, headers)."""
    params = dict(params, apiKey=ODDS_API_KEY)
    try:
        response = requests.get(f"{ODDS_BASE_URL}/{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        error(f"Cotes : erreur reseau {type(exc).__name__}")
        return None, {}
    headers = getattr(response, "headers", None) or {}
    if not response.ok:
        error(f"Cotes : HTTP {response.status_code}")
        return None, headers
    try:
        return response.json(), headers
    except ValueError:
        return None, headers


def country_ok(fixture, country):
    """6.8.0 : la ligue est-elle de ce pays (zone SportAPI ou nom) ?"""
    if country is None:
        return True
    name = norm_text(fixture.get("league_name"))
    zones = {norm_text(fixture.get(k)) for k in ("league_zone", "league_geo", "country")}
    zones.discard("")
    words = COUNTRY_WORDS.get(country, (country.lower(),))
    if norm_text(country) in zones or any(w in zones for w in words):
        return True
    padded = f" {name} "
    return any(f" {w} " in padded for w in words)


def odds_sport_key(fixture):
    name = norm_text(fixture.get("league_name"))
    # 6.8.0 : pas d'appel pour une ligue hors liste (ni ticket ni pronos) ;
    # évite aussi La Liga 2 / 2. Bundesliga pris pour la 1re division
    if not name or not ticket_league_ok(fixture):
        return None
    for pattern, country, key in ODDS_SPORT_KEYS:
        if not re.search(pattern, name):
            continue
        # championnats : jamais une coupe nationale ; sélections : ok
        if country is not None and NON_LEAGUE_PATTERN.search(name):
            continue
        if country_ok(fixture, country):
            return key
    return None


def odds_day_cap():
    """6.9.0 : 7 appels par jour de Paris, tous lancements confondus."""
    return ODDS_MAX_CALLS_PER_DAY


def night_key():
    return datetime.now(PARIS).date().isoformat()


def odds_calls_left():
    """6.9.0 : un compteur par jour de Paris (10h, relances et nuit compris) ;
    un lancement de nuit en prend au plus ODDS_NIGHT_CALLS."""
    key = night_key()
    used = int(USAGE.get("odds_paris_calls", 0)) if USAGE.get("odds_paris_day") == key else 0
    left = odds_day_cap() - used
    if NIGHT_RUN:
        night = int(USAGE.get("night_odds_calls", 0)) if USAGE.get("night_day") == key else 0
        left = min(left, ODDS_NIGHT_CALLS - night)
    return left


def count_odds_call():
    key = night_key()
    if USAGE.get("odds_paris_day") != key:
        USAGE["odds_paris_day"] = key
        USAGE["odds_paris_calls"] = 0
    USAGE["odds_paris_calls"] = int(USAGE.get("odds_paris_calls", 0)) + 1
    if NIGHT_RUN:
        if USAGE.get("night_day") != night_key():
            USAGE["night_day"] = night_key()
            USAGE["night_odds_calls"] = 0
        USAGE["night_odds_calls"] = int(USAGE.get("night_odds_calls", 0)) + 1
    USAGE["odds_calls"] = int(USAGE.get("odds_calls", 0)) + 1


def odds_events(sport_key):
    """Cotes d'une ligue : cache du jour, quota mensuel respecté."""
    if not ODDS_API_KEY:
        return None
    cache_key = f"odds:{sport_key}"
    load_disk_cache()
    if cache_key in DISK["data"]:
        value = DISK["data"][cache_key]
        return None if value == ABSENT else value
    if ODDS_STATE["sports"] is None:
        sports_key = "odds:sports"
        sports = DISK["data"].get(sports_key)
        if not isinstance(sports, list):
            sports, _ = odds_get("sports/", {})  # gratuit (hors quota)
            sports = sports if isinstance(sports, list) else []
            DISK["data"][sports_key] = sports
            DISK["dirty"] = True
        ODDS_STATE["sports"] = {s.get("key") for s in sports if isinstance(s, dict)}
    if sport_key not in ODDS_STATE["sports"]:
        return None
    if odds_calls_left() <= 0:
        return None
    remaining = USAGE.get("odds_remaining")
    if remaining is not None and number(remaining) is not None and number(remaining) < ODDS_MIN_REMAINING:
        return None
    data, headers = odds_get(
        f"sports/{sport_key}/odds/",
        {"regions": ODDS_REGIONS, "markets": ODDS_MARKETS, "oddsFormat": "decimal"},
    )
    count_odds_call()
    try:
        left = headers.get("x-requests-remaining")
    except AttributeError:
        left = None
    if number(left) is not None:
        USAGE["odds_remaining"] = int(number(left))
    DISK["data"][cache_key] = data if isinstance(data, list) else ABSENT
    DISK["dirty"] = True
    return data if isinstance(data, list) else None


def same_team(a, b):
    ta, tb = set(norm_text(a).split()), set(norm_text(b).split())
    ta -= {"fc", "cf", "sc", "afc", "ac", "club", "de", "the"}
    tb -= {"fc", "cf", "sc", "afc", "ac", "club", "de", "the"}
    if not ta or not tb:
        return False
    if ta & tb:
        return True
    # "Lyon" / "Olympique Lyonnais", "Man Utd" / "Manchester United"
    return any(
        len(min(x, y, key=len)) >= 4 and (x.startswith(y) or y.startswith(x))
        for x in ta for y in tb
    )


def find_event(events, fixture):
    kickoff = kickoff_of(fixture)
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if not (same_team(event.get("home_team"), fixture.get("home_team"))
                and same_team(event.get("away_team"), fixture.get("away_team"))):
            continue
        start = kickoff_of({"kickoff_utc": event.get("commence_time")})
        if kickoff and start and abs((start - kickoff).total_seconds()) > 3 * 3600:
            continue
        return event
    return None


def market_price(event, market):
    """Cote médiane des bookmakers pour un marché du bot, si elle existe."""
    prices = []
    for book in event.get("bookmakers") or []:
        for item in (book.get("markets") or []) if isinstance(book, dict) else []:
            key = item.get("key")
            for outcome in item.get("outcomes") or []:
                price = number(outcome.get("price"))
                if price is None or price <= 1:
                    continue
                name = outcome.get("name")
                point = number(outcome.get("point"))
                if key == "totals" and name == "Over":
                    if (market == "over15" and point == 1.5) or (market == "over25" and point == 2.5):
                        prices.append(price)
                elif key == "h2h":
                    if market == "win_home" and name == event.get("home_team"):
                        prices.append(price)
                    elif market == "win_away" and name == event.get("away_team"):
                        prices.append(price)
    if not prices:
        return None
    prices.sort()
    return prices[len(prices) // 2]


def real_odds_for(fixture, markets):
    """Vraies cotes {marché: cote} d'un match ({} si pas de clé / marché)."""
    # 6.9.0 : aucun appel pour les marchés que l'API ne couvre pas
    markets = [m for m in markets or [] if m in REAL_ODDS_MARKETS]
    if not ODDS_API_KEY or not markets:
        return {}
    key = odds_sport_key(fixture)
    if not key:
        return {}
    event = find_event(odds_events(key), fixture)
    if not event:
        return {}
    prices = {}
    for market in markets:
        price = market_price(event, market)
        if price:
            prices[market] = price
    return prices


def real_odds(result):
    """Vraies cotes des marchés affichés d'un pick ({} si aucune)."""
    return real_odds_for(result["fixture"], [m for m, _ in shown_picks(result)])


# ---------------- ANALYSE ----------------

def fatigue_of(history, today):
    """6.9.0 : (baisse d'attaque, raison) d'après le calendrier déjà chargé."""
    if not FATIGUE_ACTIF or not history:
        return 0.0, ""
    cut = 0.0
    why = []
    rest = (today - history[0]["date"]).days
    if 0 < rest <= FATIGUE_REST_DAYS:
        cut += FATIGUE_ATTACK
        why.append(f"{rest} j de repos")
    euro = any(
        EURO_CUP_PATTERN.search(str(m.get("league_name") or ""))
        and 0 < (today - m["date"]).days <= FATIGUE_EURO_DAYS
        for m in history[:3]
    )
    if euro:
        cut += FATIGUE_EURO_EXTRA if cut else FATIGUE_ATTACK
        why.append("Coupe d'Europe en semaine")
    return cut, " + ".join(why)


def team_scorers(league_id, team_id):
    """Buteurs de l'équipe (3 buts et +), du meilleur au moins bon."""
    payload = api_get(f"leagues/{league_id}/topscorers?limit=100")
    rows = []
    for row in get_list(payload):
        if not isinstance(row, dict) or str(row.get("team_id")) != str(team_id):
            continue
        goals = number(row.get("goals"))
        if goals is None or goals < TOPSCORER_MIN_GOALS or not row.get("player_name"):
            continue
        rows.append((row.get("player_name"), goals))
    rows.sort(key=lambda item: -item[1])
    return rows[:ABSENCE_KEY_PLAYERS]


def absence_of(fixture, side, history, today, force=False, max_calls=None):
    """6.9.0 : (baisse d'attaque, notes). SportAPI n'a pas de liste de
    blessés : indices tirés de la compo et des cartons du dernier match."""
    if (not ABSENCES_ACTIF and not force) or not ticket_league_ok(fixture):
        return 0.0, []
    cap = max_calls if max_calls is not None else ABSENCE_MAX_CALLS
    if ABSENCE_STATE["calls"] >= cap or not budget_available(reserve=30):
        return 0.0, []
    match = last_match(history, today)
    if match is None:
        return 0.0, []
    before = CALLS
    scorers, played, reds = [], None, []
    try:
        scorers = team_scorers(fixture.get("league_id"), fixture.get(f"{side}_id"))
        if scorers:
            played = lineup_names(match)
        reds = red_cards_last_match(match)
    except Exception as exc:
        error(f"Absences impossibles : {type(exc).__name__} {exc}")
    finally:
        ABSENCE_STATE["calls"] += CALLS - before
        ABSENCE_STATE["checked"] += 1
    cut = 0.0
    notes = []
    if played is not None:
        ABSENCE_STATE["lineups"] += 1
        for rank, (name, goals) in enumerate(scorers):
            if last_name(name) not in played:
                cut += ABSENCE_SCORER if rank == 0 else ABSENCE_OTHER
                notes.append(f"{player_short(name)} ({int(goals)} buts) absent dernier match")
    for player in reds:
        cut += ABSENCE_RED
        notes.append(f"{player_short(player)} expulsé dernier match")
    return min(cut, ABSENCE_CAP), notes


def match_context(fixture, home_history, away_history, today):
    """6.9.0 : multiplicateurs de buts attendus (fatigue + absences)."""
    context = {}
    for side, history in (("home", home_history), ("away", away_history)):
        fatigue, why = fatigue_of(history, today)
        absence, notes = absence_of(fixture, side, history, today)
        context[side] = {"fatigue": fatigue, "fatigue_why": why,
                         "absence": absence, "absence_notes": notes}
    for side, other in (("home", "away"), ("away", "home")):
        cut = min(context[side]["fatigue"] + context[side]["absence"], 0.15)
        leak = FATIGUE_LEAK if context[other]["fatigue"] > 0 else 0.0
        context[side]["factor"] = (1 - cut) * (1 + leak)
    return context


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

    base_lambda_home, base_lambda_away, base_probabilities = estimate(home, away)
    base_extra = extra_probabilities(base_lambda_home, base_lambda_away)
    # 6.9.0 : fatigue + absences -> buts attendus ajustés
    context = {}
    try:
        context = match_context(fixture, home_history, away_history, today)
    except Exception as exc:
        error(f"Contexte impossible : {type(exc).__name__} {exc}")
    f_home = context.get("home", {}).get("factor", 1.0)
    f_away = context.get("away", {}).get("factor", 1.0)
    if f_home != 1.0 or f_away != 1.0:
        home = dict(home, gf=home["gf"] * f_home, ga=home["ga"] * f_away)
        away = dict(away, gf=away["gf"] * f_away, ga=away["ga"] * f_home)
        print(
            f"CONTEXTE : {fixture.get('home_team')} x{f_home:.2f} "
            f"{fixture.get('away_team')} x{f_away:.2f} "
            f"{[context[s]['fatigue_why'] for s in ('home', 'away')]} "
            f"{[context[s]['absence_notes'] for s in ('home', 'away')]}",
            flush=True,
        )
    lambda_home, lambda_away, probabilities = estimate(home, away)

    home_form = form_stats(home_history)
    away_form = form_stats(away_history)
    blocked = blocked_markets(home_form, away_form)

    # 6.4.0 : la forme récente prime sur le modèle
    eligible_picks = []
    for market, probability in probabilities.items():
        if not MIN_PROB <= probability <= MAX_PROB:
            continue
        if market in NOT_PROPOSED:
            continue
        if market in blocked:
            REASONS[f"Forme : {market} écarté"] += 1
            print(f"FORME : {market} écarté ({blocked[market]})", flush=True)
            continue
        eligible_picks.append((market, probability))

    # 6.5.0 : bonus de classement (Domicile +0,5 d'abord à proba proche)
    picks = sorted(
        eligible_picks,
        key=lambda item: item[1] + MARKET_RANK_BONUS.get(item[0], 0.0),
        reverse=True,
    )[:MAX_MARKETS]

    # Tirs cadrés (non publiés) : seulement s'il reste assez de budget API
    home_sot = away_sot = None
    run_cap = MAX_API_CALLS if RUN_CAP is None else min(MAX_API_CALLS, RUN_CAP)
    if SOT_ACTIF and CALLS < run_cap * SOT_BUDGET_SHARE:
        home_sot = sot_profile(home_history, "home", today, fixture["home_id"])
        away_sot = sot_profile(away_history, "away", today, fixture["away_id"])
    elif SOT_ACTIF:
        REASONS["Tirs cadrés sautés (budget)"] += 1

    # Enjeu : uniquement si un pronostic ou le ticket peut en avoir besoin
    motivation = {"home": None, "away": None}
    if picks or any(TICKET_MIN_PROB <= p <= TICKET_MAX_PROB
                    for p in probabilities.values()):
        motivation = motivation_of(fixture)

    sot_picks = []

    if home_sot and away_sot:
        home_sot_prob = sot_probability(home_sot, away_sot)
        away_sot_prob = sot_probability(away_sot, home_sot)

        if SOT_MIN_PROB <= home_sot_prob <= SOT_MAX_PROB:
            sot_picks.append(("sot_home", home_sot_prob))

        if SOT_MIN_PROB <= away_sot_prob <= SOT_MAX_PROB:
            sot_picks.append(("sot_away", away_sot_prob))

    # Tirs cadrés : enregistrés dans le journal (write_journal) mais non
    # publiés tant que la calibration n'est pas validée.

    extra = extra_probabilities(lambda_home, lambda_away)
    candidates = ticket_candidates(
        probabilities, extra, home_form, away_form, motivation
    )

    print(
        "MODELE :",
        fixture.get("home_team"), "-", fixture.get("away_team"),
        "probabilites :", probabilities,
        "tirs cadrés :",
        {
            "home": home_sot,
            "away": away_sot,
        },
        flush=True,
    )

    return {
        "fixture": fixture,
        "picks": picks,
        "probabilities": probabilities,
        "base": base_probabilities,
        "base_extra": base_extra,
        "context": context,
        "best": (
            max(p + MARKET_RANK_BONUS.get(m, 0.0) for m, p in picks)
            if picks else 0
        ),
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "home_sot": home_sot,
        "away_sot": away_sot,
        "extra": extra,
        "home_form": home_form,
        "away_form": away_form,
        "candidates": candidates,
        "blocked": blocked,
        "high_picks": high_odds_picks(probabilities, extra, blocked),
        "motivation": motivation,
        "home_history": home_history,
        "away_history": away_history,
        "home_profile": home,
        "away_profile": away,
    }


# ---------------- MESSAGE ----------------

def market_labels(fixture):
    home = short_name(fixture.get("home_team"))
    away = short_name(fixture.get("away_team"))

    return {
        "home": f"{home} +0,5 but",
        "away": f"{away} +0,5 but",
        "btts": "BTTS Oui",
        "over15": "Plus de 1,5 but",
        "over25": "Plus de 2,5 buts",
        "win_home": f"{home} gagne",
        "win_away": f"{away} gagne",
        "home15": f"{home} +1,5 but",
        "away15": f"{away} +1,5 but",
        "sot_home": f"{home} : plus de 2,5 tirs cadrés",
        "sot_away": f"{away} : plus de 2,5 tirs cadrés",
    }


def ticket_lines(ticket):
    lines = ["🎟️ TICKET DU JOUR (expérimental)"]

    if not ticket:
        lines.extend([
            "Pas de ticket aujourd'hui : pas assez de matchs exploitables.",
            "",
        ])
        return lines

    fair_total = 1.0
    min_total = 1.0

    for index, leg in enumerate(ticket, 1):
        result = leg["result"]
        fixture = result["fixture"]
        home = short_name(fixture.get("home_team"))
        away = short_name(fixture.get("away_team"))
        kickoff = kickoff_of(fixture).strftime("%H:%M")
        probability = leg["prob"]
        label = market_labels(fixture)[leg["market"]]

        fair_total *= 1 / probability
        min_total *= min_odds(probability)

        lines.append(f"{index}. {home} — {away} · {kickoff}")
        league = league_label(fixture)
        if league:
            lines.append(f"🏆 {league}")
        lines.append(
            f"• {label} : {probability:.0%}"
            f" · cote juste {fmt_odds(1 / probability)}"
        )

        home_form = result.get("home_form")
        away_form = result.get("away_form")
        if home_form and away_form:
            lines.append(
                f"📊 {home} : {home_form['letters']}"
                f" ({home_form['points']} pts, {home_form['gf']} buts pour"
                f" / {home_form['ga']} contre)"
            )
            lines.append(
                f"📊 {away} : {away_form['letters']}"
                f" ({away_form['points']} pts, {away_form['gf']} buts pour"
                f" / {away_form['ga']} contre)"
            )

        if leg.get("standing"):
            lines.append(f"📍 Classement : {leg['standing']}")
        enjeu = motivation_text(result.get("motivation") or {})
        if enjeu:
            lines.append(f"⚔️ Enjeu : {enjeu}")

        lines.append("")

    if fair_total < TICKET_TARGET_ODDS:
        lines.append(
            f"ℹ️ Cote cible {fmt_odds(TICKET_TARGET_ODDS)} non atteinte :"
            " ticket le mieux noté proposé."
        )

    lines.extend([
        f"🎯 Cote juste du ticket : {fmt_odds(fair_total)}"
        f" (environ 1 chance sur {fair_total:.0f})",
        f"💰 Ton bookmaker doit payer au moins {fmt_odds(min_total)} au total.",
        "⚠️ Petite mise seulement. Probabilités de 50 à 72 % pas encore "
        "validées. Blessures, météo et compositions ne sont pas dans les "
        "données du bot : vérifie-les avant de jouer.",
        "",
    ])
    return lines


def build_message(selected, partial, analysed, total, ticket=None, summary=None,
                  mega=None, safe=None, dayfun=None, frozen_kinds=None):
    lines = [
        "⚽ PRONOSTICS DU JOUR",
        datetime.now(PARIS).strftime("📅 %d/%m/%Y"),
        "",
    ]

    selected = sorted(
        selected, key=lambda r: (kickoff_of(r["fixture"]), -r["best"])
    )
    current_hour = None

    for index, result in enumerate(selected, 1):
        fixture = result["fixture"]
        home = short_name(fixture.get("home_team"))
        away = short_name(fixture.get("away_team"))
        start = kickoff_of(fixture)
        kickoff = start.strftime("%H:%M")

        if start.hour != current_hour:
            current_hour = start.hour
            lines.append(f"⏰ {current_hour:02d}h")

        lines.append(f"{index}. {home} — {away} · {kickoff}")

        league = league_label(fixture)
        if league:
            lines.append(f"🏆 {league}")

        labels = market_labels(fixture)

        for market, probability in result["picks"]:
            lines.append(
                f"• {labels[market]} : {probability:.0%}"
                f" · 💰 min {fmt_odds(min_odds(probability))}"
            )

        home_form = result.get("home_form")
        away_form = result.get("away_form")
        if home_form and away_form:
            lines.append(
                f"📊 Forme : {home_form['letters']} | {away_form['letters']}"
            )
        enjeu = motivation_text(result.get("motivation") or {})
        if enjeu:
            lines.append(f"⚔️ Enjeu : {enjeu}")

        projection = (
            f"{result['lambda_home']:.2f} — {result['lambda_away']:.2f}"
        ).replace(".", ",")

        lines.extend([
            f"⚽ Buts projetés : {projection}",
            "",
        ])

    if not selected:
        lines.extend([
            "Aucun marché estimé dans les filtres actuels.",
            "",
        ])

    frozen_kinds = frozen_kinds or set()
    if TICKET_ACTIF:
        t_lines = ticket_lines(ticket)
        if "jour" in frozen_kinds and t_lines:
            t_lines[0] = "🎟️ TICKET DU JOUR (figé)"
        lines.extend(t_lines)
    if MEGA_ACTIF and mega is not None:
        m_title = "🎉 MÉGA FUN (figé)" if "mega" in frozen_kinds else "🎉 MÉGA FUN"
        for block in extra_ticket_blocks(m_title, mega, MEGA_TARGET_ODDS):
            lines.extend([block, ""])
    if SAFE_ACTIF and safe is not None:
        s_title = "🛡️ ULTRA SAFE (figé)" if "safe" in frozen_kinds else "🛡️ ULTRA SAFE"
        for block in extra_ticket_blocks(
            s_title, safe, None,
            note=f"ℹ️ Cotes estimées : vérifie sur Stake ({fmt_odds(SAFE_MIN_LEG_ODDS)} min par sélection)",
            empty="Aucune équipe assez sûre de marquer aujourd'hui.",
        ):
            lines.extend([block, ""])
    if DAYFUN_ACTIF and dayfun is not None:
        d_title = "🎯 FUN JOURNÉE (figé)" if "dayfun" in frozen_kinds else "🎯 FUN JOURNÉE"
        for block in extra_ticket_blocks(
            d_title, dayfun, None,
            empty="Pas assez de sélections à 70 % pour un fun journée aujourd'hui.",
            mark_boost=True,
        ):
            lines.extend([block, ""])
    lines.extend(summary_lines(summary))

    lines.append(f"📈 {analysed}/{total} matchs analysés")

    if partial:
        lines.append("⚠️ Analyse partielle.")

    lines.append(
        "💰 Ne joue que si la cote du bookmaker est au moins égale "
        "à la cote min : en dessous, le pari perd sur la durée."
    )
    lines.append("ℹ️ Heures de Paris. Sans garantie.")

    return "\n".join(lines)


# 6.6.1 : noms officiels de fédérations -> nom court
NATION_NAMES = {
    "football union of russia": "Russia",
    "bosnia-herzegovina": "Bosnia",
    "bosnia and herzegovina": "Bosnia",
    "bosnia & herzegovina": "Bosnia",
    "republic of ireland": "Ireland",
    "korea republic": "South Korea",
    "korea dpr": "North Korea",
    "ir iran": "Iran",
    "china pr": "China",
    "united states": "USA",
    "cape verde islands": "Cape Verde",
    "trinidad and tobago": "Trinidad",
    "antigua and barbuda": "Antigua",
    "st. kitts and nevis": "St Kitts",
    "saint kitts and nevis": "St Kitts",
    "st. vincent / grenadines": "St Vincent",
    "st. vincent and the grenadines": "St Vincent",
    "chinese taipei": "Taiwan",
    "north macedonia": "Macedonia",
    "central african republic": "Centrafrique",
    "dr congo": "RD Congo",
    "congo dr": "RD Congo",
}
CLUB_SUFFIXES = {"town", "united", "city", "fc", "afc", "cf"}


def display_name(value):
    """Nom affiché sur Telegram (fédérations raccourcies, sinon inchangé)."""
    name = " ".join(str(value or "Equipe").split())
    name = NATION_NAMES.get(name.lower(), name)
    return short_name(name)


def compact_name(value):
    """Nom encore plus court pour les libellés "Dom-Ext" : sans Town,
    United, City, FC... (gardé si le nom deviendrait vide)."""
    name = display_name(value)
    words = name.split()
    while len(words) > 1 and words[-1].lower().strip(".") in CLUB_SUFFIXES:
        words.pop()
    while len(words) > 1 and words[0].lower().strip(".") in ("fc", "afc", "cf"):
        words.pop(0)
    return " ".join(words)


def player_short(value):
    """'J. Smith' -> 'Smith' (initiales retirées, nom gardé)."""
    words = str(value or "").split()
    kept = [w for w in words if not (len(w) <= 3 and w.endswith("."))]
    return " ".join(kept or words)


LIGHT_LABELS = {
    "home": "{home} +0,5", "away": "{away} +0,5",
    "over15": "{mhome}-{maway} +1,5", "over25": "{mhome}-{maway} +2,5",
    "home15": "{home} +1,5 but", "away15": "{away} +1,5 but",
    "win_home": "{home} gagne", "win_away": "{away} gagne",
    "btts": "BTTS {mhome}-{maway}",
}
TICKET_LABELS = dict(LIGHT_LABELS, home15="{home} +1,5", away15="{away} +1,5")


def light_label(fixture, market, labels=LIGHT_LABELS):
    template = labels.get(market, market)
    return template.format(
        home=display_name(fixture.get("home_team")),
        away=display_name(fixture.get("away_team")),
        mhome=compact_name(fixture.get("home_team")),
        maway=compact_name(fixture.get("away_team")),
    )


def odds_text(probability, price):
    """'cote 1,36 ✅ value' (vraie cote) ou 'cote ~1,25' (estimation bot)."""
    floor = min_odds(probability)
    if price:
        text = f"cote {fmt_odds(price)}"
        if floor is not None and price < floor - 1e-9:
            return f"{text} ⛔ sous le minimum {fmt_odds(floor)} — ne pas jouer"
        if floor is not None and price > floor:
            text += " ✅ value"
        return text
    return f"cote ~{fmt_odds(floor)}" if floor is not None else ""


def fmt_roi(value):
    pct = round(value * 100)
    return "0%" if pct == 0 else f"{pct:+d}%"


def fmt_short(value):
    return fmt_odds(value).rstrip("0").rstrip(",")


def tg_labels(fixture):
    """Libellés des marchés pour Telegram (noms de sélections raccourcis)."""
    named = dict(fixture)
    named["home_team"] = display_name(fixture.get("home_team"))
    named["away_team"] = display_name(fixture.get("away_team"))
    return market_labels(named)


def tg_odds(probability, price):
    """'💰 cote 1,40 ✅ value' si vraie cote, sinon '💰 min 1,25'."""
    if price:
        text = f"💰 cote {fmt_odds(price)}"
        floor = min_odds(probability)
        if floor is not None and price < floor - 1e-9:
            return f"{text} ⛔ sous le minimum {fmt_odds(floor)} — ne pas jouer"
        if floor is not None and price > floor:
            text += " ✅ value"
        return text
    return f"💰 min {fmt_odds(min_odds(probability))}"


def match_head(fixture):
    start = kickoff_of(fixture)
    head = (
        f"{display_name(fixture.get('home_team'))} — "
        f"{display_name(fixture.get('away_team'))} · {start.strftime('%H:%M')}"
    )
    league = league_label(fixture)
    return start, head, (f"🏆 {league}" if league else None)


def fmt_chance(chance):
    pct = chance * 100
    text = f"{pct:.1f}".replace(".", ",") if pct < 10 else f"{pct:.0f}"
    if chance <= 0:
        return "?"
    if chance >= 0.5:
        return f"{text} %"
    return f"{text} % (1 chance sur {1 / chance:.0f})"


def extra_ticket_blocks(title, legs, target=None, note=None, empty="Pas assez de matchs exploitables.",
                        mark_boost=False):
    """6.9.0 : blocs Telegram du méga fun / fun journée (même présentation
    que le ticket du jour + chance estimée)."""
    if not legs:
        return [f"{title}\n{empty}"]
    blocks = []
    for number_, leg in enumerate(legs, 1):
        fixture = leg["result"]["fixture"]
        _, head, league = match_head(fixture)
        probability = leg["prob"]
        if leg.get("real"):
            odds = f"cote {fmt_odds(leg['odds'])}"
            floor = min_odds(probability)
            if floor is not None and leg["odds"] > floor:
                odds += " ✅ value"
        else:
            odds = f"cote juste {fmt_odds(leg_odds(leg))} (estim.)"
        if mark_boost and is_boost_leg(leg):
            odds += " 🔥"
        lines = ([title] if number_ == 1 else []) + [f"{number_}. {head}"]
        if league:
            lines.append(league)
        lines.append(
            f"• {tg_labels(fixture).get(leg['market'], leg['market'])} :"
            f" {probability:.0%} · {odds}"
        )
        for alert in (leg.get("alerts") or []):
            lines.append(f"⚠️ {alert}")
        blocks.append("\n".join(lines))
    total = ticket_total(legs)
    estimate = ticket_estimated(legs)
    if target and total < target:
        end = [f"ℹ️ Cote {target:g} non atteinte avec sélections solides (meilleur {fmt_short(total)}{', estim.' if estimate else ''}) : combiné court retenu."]
    else:
        end = [f"💰 Cote totale : {fmt_short(total)}{' (estim.)' if estimate else ''}"]
    end.append(f"🎯 Chance estimée : {fmt_chance(ticket_chance(legs))}")
    if note:
        end.append(note)
    blocks.append("\n".join(end))
    return blocks


def build_light_message(selected, partial, ticket=None, summary=None, enrich=None, today=None,
                        mega=None, safe=None, dayfun=None, frozen_kinds=None):
    """Message Telegram (6.6.2) : présentation par heure et par match,
    sans forme / enjeu. Les blocs sont séparés par une ligne vide, ce qui
    permet de couper le message entre deux matchs."""
    enrich = enrich or {}
    frozen_kinds = frozen_kinds or set()
    day = today or datetime.now(PARIS).date()
    title = "🌙 PRONOSTICS DE LA NUIT" if NIGHT_RUN else "⚽ PRONOSTICS DU JOUR"
    ticket_title = "🎟️ TICKET DE LA NUIT" if NIGHT_RUN else "🎟️ TICKET DU JOUR"
    if "jour" in frozen_kinds:
        ticket_title += " (figé)"
    blocks = [f"{title}\n📅 {day.strftime('%d/%m/%Y')}"]

    ordered = sorted(selected, key=lambda r: (kickoff_of(r["fixture"]), -r["best"]))
    current_hour = None
    index = 0
    for result in ordered:
        picks = shown_picks(result)
        if not picks:
            continue
        fixture = result["fixture"]
        info = enrich.get(str(fixture.get("id"))) or {}
        prices = info.get("odds") if isinstance(info.get("odds"), dict) else {}
        start, head, league = match_head(fixture)
        index += 1
        lines = []
        if start.hour != current_hour:
            current_hour = start.hour
            lines.append(f"⏰ {current_hour:02d}h")
        lines.append(f"{index}. {head}")
        if league:
            lines.append(league)
        labels = tg_labels(fixture)
        for market, probability in picks:
            lines.append(
                f"• {labels.get(market, market)} : {probability:.0%}"
                f" · {tg_odds(probability, prices.get(market))}"
            )
        if result.get("lambda_home") is not None and result.get("lambda_away") is not None:
            lines.append(
                f"⚽ Buts projetés : {result['lambda_home']:.2f} — "
                f"{result['lambda_away']:.2f}".replace(".", ",")
            )
        alerts = info.get("alerts") or []
        if alerts:
            lines.append("⚠️ " + " · ".join(alerts))
        blocks.append("\n".join(lines))
    if index == 0:
        blocks.append("Aucun prono solide cette nuit." if NIGHT_RUN else "Aucun prono solide aujourd'hui.")

    if ticket:
        ticket_odds = enrich.get("__ticket_odds__") or {}
        for number_, leg in enumerate(ticket, 1):
            fixture = leg["result"]["fixture"]
            _, head, league = match_head(fixture)
            probability = leg["prob"]
            if "real" in leg:
                price = leg.get("odds") if leg.get("real") else None
            else:
                price = ticket_odds.get((str(fixture.get("id")), leg["market"]))
            if price:
                odds = f"cote {fmt_odds(price)}"
                floor = min_odds(probability)
                if floor is not None and price > floor:
                    odds += " ✅ value"
            else:
                odds = f"cote juste {fmt_odds(1 / probability)} (estim.)"
            lines = ([ticket_title] if number_ == 1 else []) + [f"{number_}. {head}"]
            if league:
                lines.append(league)
            lines.append(
                f"• {tg_labels(fixture).get(leg['market'], leg['market'])} :"
                f" {probability:.0%} · {odds}"
            )
            blocks.append("\n".join(lines))
        if any("real" in leg for leg in ticket):
            total = ticket_total(ticket)
            estimate = " (estim.)" if ticket_estimated(ticket) else ""
        else:
            total = ticket_fair(ticket)
            estimate = ""
        if total >= TICKET_TARGET_ODDS:
            blocks.append(f"💰 Cote totale : {fmt_short(total)}{estimate}")
        else:
            blocks.append(
                f"💰 Cote {TICKET_TARGET_ODDS:g} non atteinte"
                f" (meilleur {fmt_short(total)}{', estim.' if estimate else ''})"
            )
    elif TICKET_ACTIF:
        blocks.append(f"{ticket_title}\nPas assez de matchs exploitables.")

    # 6.9.0 : méga fun + ultra safe sous le ticket du jour
    if MEGA_ACTIF and mega is not None:
        m_title = "🎉 MÉGA FUN (figé)" if "mega" in frozen_kinds else "🎉 MÉGA FUN"
        blocks.extend(extra_ticket_blocks(m_title, mega, MEGA_TARGET_ODDS))
    if SAFE_ACTIF and safe is not None:
        s_title = "🛡️ ULTRA SAFE (figé)" if "safe" in frozen_kinds else "🛡️ ULTRA SAFE"
        blocks.extend(extra_ticket_blocks(
            s_title, safe, None,
            note=f"ℹ️ Cotes estimées : vérifie sur Stake ({fmt_odds(SAFE_MIN_LEG_ODDS)} min par sélection)",
            empty="Aucune équipe assez sûre de marquer aujourd'hui.",
        ))
    if DAYFUN_ACTIF and dayfun is not None:
        d_title = "🎯 FUN JOURNÉE (figé)" if "dayfun" in frozen_kinds else "🎯 FUN JOURNÉE"
        blocks.extend(extra_ticket_blocks(
            d_title, dayfun, None,
            empty="Pas assez de sélections à 70 % pour un fun journée aujourd'hui.",
            mark_boost=True,
        ))

    footer = []
    if summary and summary.get("markets"):
        n = sum(s["n"] for s in summary["markets"].values())
        wins = sum(s["wins"] for s in summary["markets"].values())
        profit = sum(s["profit"] for s in summary["markets"].values())
        if n:
            real = int(summary.get("real_published") or 0)
            if real >= n:
                label = ""
            elif real == 0:
                label = " (cote min)"
            else:
                label = f" ({real}/{n} à la vraie cote, reste cote min)"
            footer.append(
                f"📊 {STATS_DAYS}j : {wins / n:.0%} · ROI {fmt_roi(profit / n)}{label}"
            )
    if tickets_line(summary):
        footer.append(tickets_line(summary))
    if partial:
        footer.append("⚠️ Analyse partielle (quota API)")
    if footer:
        blocks.append("\n".join(footer))
    return "\n\n".join(blocks)


def write_fiches(selected, enrich, ticket, partial, analysed, total, summary, now,
                 mega=None, safe=None, dayfun=None, frozen_kinds=None):
    """Fiche complète (ancien message détaillé + infos par pick) dans
    data/fiches/ : tout le détail, sans alourdir Telegram."""
    try:
        text = build_message(
            selected, partial, analysed, total, ticket, summary,
            mega=mega, safe=safe, dayfun=dayfun, frozen_kinds=frozen_kinds,
        )
        extra = ["", "===== DÉTAIL PAR PICK ====="]
        for result in sorted(selected, key=lambda r: kickoff_of(r["fixture"])):
            fixture = result["fixture"]
            info = enrich.get(str(fixture.get("id"))) or {}
            extra.append(
                f"{short_name(fixture.get('home_team'))} — "
                f"{short_name(fixture.get('away_team'))} · "
                f"{kickoff_of(fixture).strftime('%H:%M')}"
            )
            extra.extend("  " + line for line in detail_lines(result, info))
            extra.extend("  ⚠️ " + a for a in info.get("alerts") or [])
            extra.append("")
        extra.append(f"Appels API ce lancement : {CALLS} · cache : {CACHE_HITS}")
        os.makedirs(FICHES_DIR, exist_ok=True)
        path = os.path.join(FICHES_DIR, now.strftime("%Y-%m-%d_%H%M") + ".txt")
        with open(path, "w", encoding="utf-8") as file:
            file.write(text + "\n" + "\n".join(extra) + "\n")
        return path
    except Exception as exc:
        error(f"Fiche impossible : {type(exc).__name__} {exc}")
        return None


def frozen_tickets_for_day(today):
    """6.9.3 : tickets déjà publiés aujourd'hui dans le journal (figés pour la journée)."""
    rows = read_predictions_file()
    day_iso = today.isoformat()
    frozen = {}
    for row in rows:
        if row.get("date") != day_iso or row.get("player"):
            continue
        for kind in ticket_kinds(row):
            frozen.setdefault(kind, []).append(row)
    return frozen


def legs_from_frozen_rows(rows):
    """Reconstruit des pseudo-jambes lisibles à partir des lignes figées du journal."""
    legs = []
    for row in rows:
        parts = str(row.get("match") or "").split(" - ", 1)
        home_team = parts[0].strip() if parts else "Equipe"
        away_team = parts[1].strip() if len(parts) > 1 else "Equipe"
        try:
            d = datetime.fromisoformat(str(row.get("date"))).date()
        except (ValueError, TypeError):
            d = paris_today()
        try:
            hh, mm = str(row.get("kickoff") or "12:00").split(":", 1)
            dt = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=PARIS)
        except (ValueError, TypeError):
            dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=PARIS)
        kickoff_utc = dt.astimezone(timezone.utc).isoformat()
        raw_prob = number(row.get("probability"))
        prob = min(max(raw_prob, 0.01), 0.99) if raw_prob is not None and raw_prob > 0 else 0.5
        price = parse_odds(row.get("odds_market"))
        legs.append({
            "result": {
                "fixture": {
                    "id": row.get("fixture_id"),
                    "home_team": home_team,
                    "away_team": away_team,
                    "league_name": row.get("league") or "",
                    "kickoff_utc": kickoff_utc,
                }
            },
            "market": row.get("market"),
            "prob": prob,
            "odds": price if price else 1 / prob,
            "real": bool(price),
            "_frozen": True,
        })
    legs.sort(key=lambda leg: kickoff_of(leg["result"]["fixture"]) or datetime.max.replace(tzinfo=PARIS))
    return legs


def write_journal(all_results, selected, today, ticket=(), market_odds=None, extra_tickets=None, frozen_kinds=None):
    rows = read_predictions()
    frozen_kinds = set(frozen_kinds or ())
    known = {
        (r["date"], r["fixture_id"], r["market"], r["player"])
        for r in rows
    }
    published = {
        (str(r["fixture"].get("id")), m)
        for r in selected for m, _ in shown_picks(r)
    }

    ticket_keys = set()
    tags = {}
    all_items = ([] if "jour" in frozen_kinds else [("jour", ticket)])
    for k, legs in (extra_tickets or {}).items():
        if k not in frozen_kinds:
            all_items.append((k, legs))
    for kind, legs in all_items:
        for leg in legs or []:
            key = (str(leg["result"]["fixture"].get("id")), leg["market"])
            tags.setdefault(key, set()).add(kind)
            ticket_keys.add(key)

    day_iso = today.isoformat()
    for row in rows:
        if row.get("date") != day_iso or row.get("player"):
            continue
        key = (str(row.get("fixture_id")), row.get("market"))
        # 6.9.3 : on fige les tags déjà publiés dans la journée ; on ajoute seulement les nouveaux
        old = {t for t in str(row.get("tickets") or "").split(";") if t}
        new = tags.get(key, set())
        if old or new:
            row["tickets"] = ";".join(sorted(old | new))
        # 6.9.3 : met à jour published (0 -> 2 ou 1) et odds_market sur les lignes déjà loguées
        cur_pub = str(row.get("published") or "0")
        if key in published:
            row["published"] = "1"
        elif key in ticket_keys and cur_pub != "1":
            row["published"] = "2"
        if not row.get("odds_market") and market_odds and key in market_odds and market_odds[key]:
            row["odds_market"] = f"{market_odds[key]:.2f}"

    for result in all_results:
        fixture = result["fixture"]
        fid = str(fixture.get("id"))
        base_info = {
            "date": today.isoformat(),
            "fixture_id": fid,
            "match": f"{fixture.get('home_team')} - {fixture.get('away_team')}",
            "league": fixture.get("league_name") or "",
            "kickoff": kickoff_of(fixture).strftime("%H:%M"),
            "model_version": MODEL_VERSION,
            "result": "pending",
            "home_id": str(fixture.get("home_id") or ""),
            "away_id": str(fixture.get("away_id") or ""),
            "odds_taken": "",
            "odds_market": "",
        }

        all_markets = dict(result["probabilities"])
        all_markets.update(result.get("extra") or {})

        if result.get("home_sot") and result.get("away_sot"):
            all_markets["sot_home"] = sot_probability(
                result["home_sot"], result["away_sot"]
            )
            all_markets["sot_away"] = sot_probability(
                result["away_sot"], result["home_sot"]
            )

        for market, probability in all_markets.items():
            # 6.9.0 : une sélection de ticket est toujours journalisée
            if probability < LOG_MIN_PROB and (fid, market) not in tags:
                continue

            key = (base_info["date"], fid, market, "")
            if key in known:
                continue

            price = (market_odds or {}).get((fid, market))
            base_value = (result.get("base") or {}).get(market)
            if base_value is None:
                base_value = (result.get("base_extra") or {}).get(market, probability)
            rows.append({
                **base_info,
                "odds_market": f"{price:.2f}" if price else "",
                "tickets": ";".join(sorted(tags.get((fid, market), ()))),
                "market": market,
                "player": "",
                "probability": f"{probability:.4f}",
                "prob_base": f"{base_value:.4f}",
                "published": (
                    1 if (fid, market) in published
                    else 2 if (fid, market) in ticket_keys
                    else 0
                ),
            })
            known.add(key)

    write_predictions(rows)
    print(f"JOURNAL : {len(rows)} lignes au total.", flush=True)


# ---------------- EXECUTION ----------------

def main():
    global PARTIAL, NIGHT_RUN

    if not DRY_RUN and (not TOKEN or not CHAT_ID):
        raise RuntimeError("TELEGRAM_TOKEN ou CHAT_ID absent.")
    if not API_KEY:
        send_message("❌ Secret API_KEY absent.")
        return

    now = datetime.now(PARIS)
    today = now.date()
    is_manual = (
        os.getenv("FORCE_SEND", "").strip() == "1"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() == "workflow_dispatch"
    )
    NIGHT_RUN = is_night_run(now) and (NUIT_ACTIVE or is_manual)
    if NIGHT_RUN:
        print(f"MODE NUIT : matchs jusqu'à {NIGHT_WINDOW_END_HOUR:02d}:00", flush=True)

    # 6.6.0 : cache disque + budget du jour
    load_disk_cache(today)
    init_budget()
    did_send = False
    try:
        did_send = bool(run(now, today, is_manual=is_manual))
    finally:
        save_disk_cache()
        save_usage(count_run=did_send)
        print(f"APPELS API : {CALLS} (cache : {CACHE_HITS}) · quota {QUOTA}", flush=True)


def run(now, today, is_manual=False):
    global PARTIAL

    try:
        settle_previous_predictions(today)
    except Exception as exc:
        error(f"Verification du journal impossible : {type(exc).__name__} {exc}")

    slot = current_slot(now)
    if not NUIT_ACTIVE and not is_manual and (now.hour < RUN_START_HOUR or now.hour >= RUN_END_HOUR):
        print(
            f"HORS CRENEAU ({now.strftime('%H:%M')} Paris) : analyse et envoi Telegram ignores (journal verifie).",
            flush=True,
        )
        return False

    if not is_manual and int(USAGE.get("paris_runs", 0)) >= MAX_RUNS_PER_DAY:
        print(
            f"Plafond de {MAX_RUNS_PER_DAY} lancements/jour atteint (FORCE_SEND=1 pour forcer).",
            flush=True,
        )
        return False

    if already_sent(today, slot):
        print(f"Message deja envoye pour le creneau {slot} (FORCE_SEND=1 pour forcer).", flush=True)
        return False

    payload = api_get(f"fixtures/date/{today.isoformat()}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        send_message("⚠️ Analyse indisponible : matchs SportAPI illisibles.")
        return

    fixtures = payload["data"]
    eligible = []
    seen = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue

        kickoff = kickoff_of(fixture)
        if kickoff is None:
            continue

        if kickoff.date() != today or not kickoff_still_open(kickoff, now):
            continue
        # 6.8.1 : la nuit, seulement les matchs avant 08:00
        if is_night_run(now) and kickoff.hour >= NIGHT_WINDOW_END_HOUR:
            continue

        if str(fixture.get("status") or "").casefold() != "scheduled":
            continue

        if any(
            fixture.get(field) is None
            for field in ("home_id", "away_id", "league_id")
        ):
            continue

        if EXCLUDE_YOUTH and is_youth(fixture):
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

    eligible = prioritise(eligible)
    api_football_report = validate_api_football_fixtures(eligible, today)
    print(format_api_football_report(api_football_report), flush=True)

    results = []
    all_results = []
    analysed = 0

    for fixture in eligible:
        # 6.6.0 : on garde ENRICH_RESERVE appels pour les infos des picks
        if not budget_available(ENRICH_RESERVE):
            PARTIAL = True
            break

        if not kickoff_still_open(kickoff_of(fixture), datetime.now(PARIS)):
            continue

        try:
            result = analyse(fixture, today)
        except Exception as exc:
            error(
                f"Lecture historique impossible ({fixture.get('id')}) : "
                f"{type(exc).__name__} {exc}"
            )
            continue

        if result is None:
            REASONS["Historique insuffisant ou trop ancien"] += 1
            continue

        analysed += 1
        all_results.append(result)

        if result["picks"]:
            results.append(result)

    if BLOCKED:
        PARTIAL = True

    results.sort(
        key=lambda r: (-r["best"], kickoff_of(r["fixture"]))
    )

    publication_time = datetime.now(PARIS)
    selected = [
        r for r in results
        if r["picks"] and kickoff_still_open(kickoff_of(r["fixture"]), publication_time)
        # 6.8.0 : pronos aussi limités aux ligues autorisées (Stake)
        and (not PRONOS_LIGUES_AUTORISEES or ticket_league_ok(r["fixture"]))
    ][:MAX_MATCHES]

    # 6.7.0 : pronos "cote plus haute" ajoutés aux matchs déjà retenus, puis
    # quelques matchs supplémentaires qui n'ont que ce type de prono.
    for r in selected:
        r["picks"] = list(r["picks"]) + list(r.get("high_picks") or [])

    chosen = {str(r["fixture"].get("id")) for r in selected}
    only_high = [
        r for r in all_results
        if r.get("high_picks")
        and str(r["fixture"].get("id")) not in chosen
        and kickoff_still_open(kickoff_of(r["fixture"]), publication_time)
        and (not PRONOS_LIGUES_AUTORISEES or ticket_league_ok(r["fixture"]))
    ]
    only_high.sort(key=lambda r: -max(p for _, p in r["high_picks"]))
    for r in only_high[:HIGH_ODDS_EXTRA_MATCHES]:
        r["picks"] = list(r["high_picks"])
        r["best"] = max(p for _, p in r["high_picks"])
        selected.append(r)

    frozen = frozen_tickets_for_day(today)
    frozen_kinds = set(frozen.keys())

    if not TICKET_ACTIF:
        frozen.pop("jour", None)
        frozen_kinds.discard("jour")
        ticket = []
    elif "jour" in frozen_kinds:
        ticket = legs_from_frozen_rows(frozen["jour"])
        print("TICKET DU JOUR : déjà publié aujourd'hui (figé).", flush=True)
    else:
        ticket = []
        try:
            ticket = build_ticket(all_results, publication_time)
            for leg in ticket:
                try:
                    leg["standing"] = standing_text(leg["result"]["fixture"])
                except Exception:
                    leg["standing"] = ""
        except Exception as exc:
            ticket = []
            error(f"Ticket impossible : {type(exc).__name__} {exc}")

    # 6.9.5 : méga fun + fun journée (figés dès leur 1re publication du jour).
    # Ultra safe désactivé : pas de construction, pas d'appels absences.
    mega = dayfun = None
    if MEGA_ACTIF:
        if "mega" in frozen_kinds:
            mega = legs_from_frozen_rows(frozen["mega"])
            print("MÉGA FUN : déjà publié aujourd'hui (figé).", flush=True)
        else:
            try:
                mega = build_mega(
                    all_results, publication_time,
                    exclude_ids=ticket_fixture_ids(ticket),
                )
            except Exception as exc:
                mega = []
                error(f"Méga fun impossible : {type(exc).__name__} {exc}")
    if DAYFUN_ACTIF:
        if "dayfun" in frozen_kinds:
            dayfun = legs_from_frozen_rows(frozen["dayfun"])
            print("FUN JOURNÉE : déjà publié aujourd'hui (figé).", flush=True)
        else:
            try:
                dayfun = build_dayfun(
                    all_results, publication_time,
                    exclude_ids=ticket_fixture_ids(ticket) | ticket_fixture_ids(mega),
                )
            except Exception as exc:
                dayfun = []
                error(f"Fun journée impossible : {type(exc).__name__} {exc}")
    print(f"ABSENCES : {ABSENCE_STATE}", flush=True)

    # 6.6.0 : infos par pick publié (H2H, buteur, cartons, cotes)
    enrich = {}
    for result in selected:
        try:
            info = enrich_pick(result, today)
            prices = real_odds(result)
            if prices:
                info["odds"] = prices
            enrich[str(result["fixture"].get("id"))] = info
        except Exception as exc:
            error(f"Infos pick impossibles : {type(exc).__name__} {exc}")

    # 6.8.0 : les vraies cotes du ticket sont lues pendant sa construction
    ticket_odds = {}
    for leg in ticket or []:
        if leg.get("real"):
            ticket_odds[(str(leg["result"]["fixture"].get("id")), leg["market"])] = leg["odds"]
    enrich["__ticket_odds__"] = ticket_odds

    # 6.8.0 : vraies cotes connues -> journal (ROI réel dans le bilan)
    market_odds = dict(ticket_odds) if "jour" not in frozen_kinds else {}
    if "mega" not in frozen_kinds:
        for leg in (mega or []):
            if leg.get("real"):
                market_odds[(str(leg["result"]["fixture"].get("id")), leg["market"])] = leg["odds"]
    for fid, info in enrich.items():
        if fid.startswith("__") or not isinstance(info, dict):
            continue
        for market, price in (info.get("odds") or {}).items():
            market_odds[(fid, market)] = price

    try:
        extra_tickets = {}
        if mega is not None and "mega" not in frozen_kinds:
            extra_tickets["mega"] = mega
        if dayfun is not None and "dayfun" not in frozen_kinds:
            extra_tickets["dayfun"] = dayfun
        write_journal(
            all_results, selected, today,
            () if "jour" in frozen_kinds else ticket,
            market_odds, extra_tickets, frozen_kinds=frozen_kinds,
        )
    except Exception as exc:
        error(f"Ecriture du journal impossible : {type(exc).__name__} {exc}")

    summary = None
    try:
        summary = journal_summary(read_predictions_file(), today)
        print_summary(summary)
        write_summary_file(summary, today)
    except Exception as exc:
        error(f"Bilan du journal impossible : {type(exc).__name__} {exc}")

    print("\n===== BILAN FINAL =====", flush=True)
    print("MATCHS RECUS :", len(fixtures), flush=True)
    print("MATCHS A VENIR :", len(eligible), flush=True)
    print("MATCHS ANALYSABLES :", analysed, flush=True)
    print("MATCHS PUBLIES :", len(selected), flush=True)
    print("REQUETES :", CALLS, flush=True)
    print("ANALYSE PARTIELLE :", PARTIAL, flush=True)
    print("DIAGNOSTIC :", dict(REASONS), flush=True)
    print("ERREURS :", ERRORS, flush=True)

    fiche = write_fiches(
        selected, enrich, ticket, PARTIAL, analysed, len(eligible), summary, now,
        mega=mega, dayfun=dayfun, frozen_kinds=frozen_kinds,
    )
    if fiche:
        print("FICHE :", fiche, flush=True)

    send_message(build_light_message(
        selected, PARTIAL, ticket, summary, enrich, today,
        mega=mega, dayfun=dayfun, frozen_kinds=frozen_kinds,
    ))
    mark_sent(today, slot)
    return True


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERREUR FATALE :", type(exc).__name__, safe(exc), flush=True)
        raise SystemExit(1)
