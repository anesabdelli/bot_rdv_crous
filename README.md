# crous-watch-bot

Bot Telegram qui surveille [trouverunlogement.lescrous.fr](https://trouverunlogement.lescrous.fr/)
et vous alerte dès qu'un **nouveau logement CROUS** apparaît dans la zone de
votre choix.

Construit sur le même principe que le bot de surveillance RDV préfecture :
polling régulier, détection de blocage/CAPTCHA avec back-off automatique,
alerte Telegram + notification ntfy.sh en cas de disponibilité.

## Fonctionnalités

- **Deux façons de choisir sa zone de recherche :**
  - `/ville` — tapez le nom d'une ville ou d'un département, le bot géolocalise
    automatiquement (via l'API officielle [geo.api.gouv.fr](https://geo.api.gouv.fr))
    et construit lui-même l'URL de recherche.
  - `/carte` — le bot vous envoie un lien vers la carte interactive du site.
    Vous choisissez votre zone à la main, copiez l'URL obtenue (elle contient
    `bounds=...`) et vous la collez au bot.
- Surveillance en continu (intervalle configurable, 2s par défaut) avec
  détection des **nouveaux logements uniquement** (pas de spam sur les
  logements déjà vus).
- Détection des blocages (HTTP 429 / 403 / CAPTCHA / page de surcharge) avec
  pause automatique de 5 minutes puis reprise.
- Alerte "alarme incendie" (message Telegram non silencieux + notification
  push ntfy.sh optionnelle) dès qu'un logement neuf est trouvé.
- Liste complète de commandes / menu Telegram natif.
- Bot restreint à votre `TELEGRAM_CHAT_ID` (les autres utilisateurs sont ignorés).

## Commandes

| Commande   | Description |
|------------|-------------|
| `/start`   | Démarre le bot et affiche votre chat ID |
| `/help`    | Affiche la liste des commandes |
| `/ville`   | Choisir une zone en tapant une ville ou un département |
| `/carte`   | Choisir une zone sur la carte interactive du site |
| `/zone`    | Affiche la zone actuellement configurée |
| `/monitor` | Démarre la surveillance |
| `/stop`    | Arrête la surveillance |
| `/check`   | Effectue une vérification immédiate |
| `/status`  | Affiche l'état du bot (zone, nombre de checks, etc.) |
| `/test`    | Déclenche une fausse alerte pour tester les notifications |
| `/reset`   | Efface la zone choisie |

## Installation

### 1. Prérequis

- Python 3.10+
- Un bot Telegram créé via [@BotFather](https://t.me/BotFather) (récupérez le token)

### 2. Cloner et installer

```bash
git clone <url-de-votre-repo> crous-watch-bot
cd crous-watch-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configurer

```bash
cp .env.example .env
```

Éditez `.env` et renseignez au minimum `TELEGRAM_BOT_TOKEN`. Lancez le bot une
première fois, envoyez `/start` en privé à votre bot sur Telegram, il vous
donnera votre `TELEGRAM_CHAT_ID` à copier dans `.env`. Relancez ensuite le bot.

### 4. Lancer

```bash
python bot.py
```

### 5. Utiliser

Sur Telegram :

1. `/ville Bordeaux` *(ou juste `/ville`, puis tapez le nom au message suivant)*
   **ou** `/carte` puis collez l'URL obtenue sur le site.
2. `/monitor` pour démarrer la surveillance.
3. Vous recevez une alerte dès qu'un logement neuf apparaît dans la zone.
4. `/stop` pour arrêter.

## Notes importantes

- **L'id d'outil CROUS change chaque année universitaire** (ex :
  `/tools/47/search` pour 2026‑2027). Si `/ville` ou `/carte` ne renvoient
  plus rien, vérifiez l'URL actuelle sur le site et mettez à jour
  `CROUS_SEARCH_BASE_URL` dans `.env`.
- Le bot n'interroge que la **première page** de résultats de la zone
  choisie : gardez des zones raisonnablement ciblées (une ville, pas la
  France entière) pour ne rien manquer.
- Un intervalle de vérification très court (2s) peut entraîner un blocage
  temporaire par le site ; le bot gère cela automatiquement (pause de 5 min)
  mais vous pouvez augmenter `CHECK_INTERVAL` dans `.env` si les blocages
  sont fréquents.
- Ce bot interroge uniquement des pages publiques et l'API officielle
  `geo.api.gouv.fr` ; respectez les conditions d'utilisation du site CROUS.

## Déploiement continu (optionnel)

Pour le faire tourner en permanence, utilisez par exemple `systemd`, `tmux`,
ou Docker. Un exemple de service `systemd` :

```ini
[Unit]
Description=CROUS Watch Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/crous-watch-bot
ExecStart=/opt/crous-watch-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Licence

Usage personnel. Aucune garantie fournie.
This bot is for personal use only
