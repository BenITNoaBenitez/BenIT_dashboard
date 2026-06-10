# Skill logging Hermes

## Objectif

Centraliser les evenements produits par Hermes Agent dans une base SQLite locale,
puis les afficher dans le dashboard local.

## Endpoint local

- `GET /api/health` verifie que le serveur est lance.
- `GET /api/logs` retourne les derniers logs.
- `POST /api/logs` ajoute un evenement.

Payload attendu pour `POST /api/logs`:

```json
{
  "level": "info",
  "message": "Action executee par Hermes"
}
```

## Niveaux recommandes

- `debug` pour le diagnostic technique.
- `info` pour les actions normales.
- `warning` pour les anomalies non bloquantes.
- `error` pour les erreurs qui bloquent une action.

## Prochaines instructions

Ce fichier servira de base aux consignes Hermes. Les regles definitives de
collecte, formatage, retention et affichage seront ajoutees ensuite.
