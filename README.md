# bot-btts ⚽

Bot de pronostics football qui analyse les matchs du jour et publie ses
sélections sur Telegram. Données via [SportAPI.ai](https://sportapi.ai),
cotes réelles optionnelles via [The Odds API](https://the-odds-api.com).

## Fonctionnalités

- **Analyse statistique** : modèle de Poisson sur les buts attendus
  (domicile/extérieur), pondéré sur les 10 derniers matchs (décroissance).
- **Filtre de forme** strict sur les 5 derniers matchs (séries de défaites,
  matchs sans marquer, seuils marqués/encaissés).
- **Marchés** : Domicile +0,5 but, Extérieur +0,5 but, Plus de 1,5 but,
  Plus de 2,5 buts, Victoire domicile/extérieur.
- **Tickets multiples** :
  - 🎟️ **Ticket du jour** — 4 matchs, cote visée ~10
  - 🎉 **Méga Fun** — cote ≥ 50, sélections solides (forme ≥ 60 %)
  - 🎯 **Fun Journée** — jusqu'à 10 jambes (solides + boosts)
- **Cotes réelles** (optionnel) via The Odds API : cote bookmaker affichée
  si disponible, sinon cote minimale estimée (préfixe `~`).
- **Journal & bilan** : `data/predictions.csv` (pronostics + résultats),
  `data/bilan.csv` (ROI, calibration, score de Brier sur 30 jours).
- **Gestion du quota API** : cache disque, lecture des en-têtes
  `X-RateLimit`, arrêt propre avant épuisement.
- **Ajustements** : fatigue (match récent, coupe d'Europe), enjeu de
  classement (vrais championnats uniquement).

## Prérequis

- Python 3.11+
- Une clé **SportAPI.ai**
- Un **bot Telegram** (token + chat_id du canal/chat cible)
- *(Optionnel)* Une clé **The Odds API** pour les cotes réelles

## Installation

```bash
git clone https://github.com/Petskllm/bot-btts.git
cd bot-btts
pip install requests
