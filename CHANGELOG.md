# Changelog

## [1.0.0] - 2026-08-21

### Added
- Bot Telegram de surveillance des logements CROUS (trouverunlogement.lescrous.fr).
- Choix de la zone de recherche par nom de ville/département (`/ville`, géocodage
  via geo.api.gouv.fr) ou par sélection manuelle sur la carte du site (`/carte`).
- Boucle de surveillance avec intervalle configurable, détection des nouveaux
  logements uniquement (diff sur les identifiants d'annonces).
- Détection des blocages HTTP 429/403 et des pages CAPTCHA/surcharge, avec
  pause automatique et reprise.
- Alertes Telegram non silencieuses + notification push optionnelle via ntfy.sh.
- Commandes `/start`, `/help`, `/ville`, `/carte`, `/zone`, `/monitor`, `/stop`,
  `/check`, `/status`, `/test`, `/reset`.
- Restriction d'accès au `TELEGRAM_CHAT_ID` configuré.
- Menu de commandes natif Telegram (`setMyCommands`).