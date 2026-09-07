# Politique de confidentialité — clipping-bot

_Dernière mise à jour : 7 septembre 2026_

**clipping-bot** est un outil personnel et automatisé de création de clips vidéo.
Il n'a pas d'utilisateurs tiers : il fonctionne uniquement pour le compte de son
propriétaire.

## Données traitées

- **Google Drive** : l'application utilise le périmètre OAuth `drive.file`, qui
  lui donne accès **uniquement aux fichiers qu'elle crée elle-même** (clips
  vidéo générés, fichier d'état `state.json`, journaux). Elle **n'accède pas**
  au reste de votre Google Drive.
- **APIs publiques** (YouTube Data API, Twitch, Google Gemini, Upload-Post) :
  utilisées pour détecter des vidéos publiques, générer des sous-titres et
  publier des clips. Aucune donnée personnelle d'utilisateurs tiers n'est
  collectée.

## Stockage et partage

- Les fichiers générés sont stockés sur le Google Drive du propriétaire.
- Aucune donnée n'est vendue, louée ou partagée avec des tiers.
- Les clés d'API et jetons d'accès sont stockés de façon sécurisée
  (secrets GitHub Actions / fichier local non versionné).

## Suppression des données

Le propriétaire peut à tout moment révoquer l'accès de l'application depuis
https://myaccount.google.com/permissions et supprimer les fichiers créés sur
son Google Drive.

## Contact

Pour toute question : ce.boutajar@gmail.com
