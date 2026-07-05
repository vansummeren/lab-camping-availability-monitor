# Bakkum Watcher

Überwacht https://www.campingbakkum.de/ubernachten/campen auf frei werdende
Stellplätze (Stornierungen) für die Woche 25.–31. Juli 2026 und benachrichtigt
per ntfy und/oder Home-Assistant-Webhook.

## Warum Playwright?

Die Seite rendert Preise/Verfügbarkeit clientseitig über ein Buchungs-API
(statisches HTML zeigt nur `...`). Der Watcher lädt die Seite headless,
fängt alle JSON-XHR-Antworten ab und wertet zusätzlich das gerenderte DOM aus
(Preis sichtbar + "Buchen" = verfügbar, "ausgebucht"/"nicht verfügbar" = belegt).

## Setup (Portainer / Docker)

1. Ordner als Stack/Repo nach Portainer bringen, `NTFY_URL` anpassen
   (zufälligen Topic-Namen wählen, ntfy-App aufs Handy, Topic abonnieren).
2. **Erster Lauf mit `DISCOVERY: "1"`**:
   ```
   docker compose run --rm -e DISCOVERY=1 -e CHECK_INTERVAL=0 bakkum-watcher
   docker compose cp bakkum-watcher:/data/discovery ./discovery  # oder Volume inspizieren
   ```
   In `/data/discovery/` liegen dann alle JSON-Payloads des Buchungs-APIs,
   `page.html` und ein Screenshot. Damit lässt sich prüfen, ob die
   Datumswahl geklappt hat und welcher Endpoint die Verfügbarkeit liefert —
   ggf. Parser darauf zuschneiden (dann reicht sogar `requests` statt Playwright).
3. Danach `DISCOVERY` entfernen und Stack normal laufen lassen:
   ```
   docker compose up -d --build
   docker logs -f bakkum-watcher
   ```

## Benachrichtigung

- **ntfy**: `NTFY_URL` auf Topic setzen — fertig. Klick auf die Notification
  öffnet direkt die Buchungsseite.
- **Home Assistant**: Webhook-Automation anlegen und `HA_WEBHOOK_URL` setzen:

  ```yaml
  automation:
    - alias: Bakkum Stellplatz frei
      trigger:
        - platform: webhook
          webhook_id: bakkum_watch
          allowed_methods: [POST]
          local_only: false
      action:
        - service: notify.mobile_app_dein_handy
          data:
            title: "{{ trigger.json.title }}"
            message: "{{ trigger.json.message }}"
            data:
              url: "{{ trigger.json.url }}"
              priority: high
              ttl: 0
  ```

## Verhalten

- Benachrichtigt nur beim Übergang **belegt → frei** pro Kategorie
  (State in `/data/state.json`), kein Spam bei jedem Lauf.
- `CHECK_INTERVAL=300` (5 min) ist ein guter Kompromiss — aggressiver
  bringt kaum Vorteil und belastet die Seite unnötig.
- `NOTIFY_ON_ERROR=1` meldet, wenn der Check selbst scheitert
  (Seitenumbau, Selektoren kaputt etc.).

## Repo-Struktur für ein Portainer-GitOps-Stack

Portainer kann einen Stack direkt aus einem Git-Repo ziehen und (bei Bedarf)
automatisch neu deployen, wenn du pushst — genau das GitOps-Muster, das du
schon für deine anderen Stacks nutzt. Ablage so:

```
bakkum-watcher/          <- Repo-Root (oder Unterordner in einem Mono-Repo)
├── Dockerfile
├── docker-compose.yml
├── watcher.py
└── README.md
```

Wichtig: `docker-compose.yml` referenziert `build: .` — das heißt, Portainer
braucht beim Deploy Zugriff auf **den ganzen Ordner**, nicht nur die Compose-
Datei. Das ist bei "Repository" als Stack-Quelle automatisch der Fall (siehe
unten), im Gegensatz zu "Web editor", wo du nur die Compose-Datei einfügst
und Portainer keinen Build-Context hat.

### 1. Repo anlegen und pushen

```bash
cd bakkum-watcher
git init
git add .
git commit -m "Bakkum availability watcher"
git branch -M main
git remote add origin git@github.com:<dein-user>/bakkum-watcher.git
git push -u origin main
```

Privates Repo reicht völlig — Portainer kann sich mit einem PAT/Deploy-Key
authentifizieren (siehe Schritt 2).

### 2. Stack in Portainer anlegen ("Repository"-Methode)

1. Portainer → **Stacks** → **Add stack**
2. **Build method: Repository**
3. Felder ausfüllen:
   - **Repository URL**: `https://github.com/<dein-user>/bakkum-watcher.git` (oder `git@...` für SSH)
   - **Repository reference**: `refs/heads/main` (oder leer lassen für den Default-Branch)
   - **Compose path**: `docker-compose.yml` — falls das Repo nicht die Wurzel ist,
     sondern z. B. `homelab-stacks/bakkum-watcher/docker-compose.yml`, dann diesen
     relativen Pfad angeben. Portainer klont trotzdem das **ganze Repo**, der
     Pfad sagt ihm nur, welche Compose-Datei es als Einstieg nimmt.
   - **Authentication**: bei privatem Repo anhaken und PAT (GitHub: Settings →
     Developer settings → Personal access tokens, Scope `repo`) oder Deploy-Key hinterlegen.
4. **Environment variables**: hier `NTFY_URL` (und ggf. `HA_WEBHOOK_URL`,
   `ARRIVAL`/`DEPARTURE` falls andere Woche) setzen — überschreibt/ergänzt die
   Defaults aus der `docker-compose.yml`. Alternativ direkt in der
   `docker-compose.yml` im Repo pflegen, wenn dir das lieber ist.
5. **GitOps updates** (optional, aber empfehlenswert): "Enable automatic
   updates" anhaken, Intervall z. B. 5 min, **oder** "Use webhook" für einen
   sofortigen Redeploy bei `git push` (Webhook-URL dann als GitHub-Repo-Webhook
   eintragen, Content-Type `application/json`).
6. **Deploy the stack**.

Was dabei technisch passiert: Portainer klont das Repo in einen internen
Arbeitsordner auf dem Docker-Host, führt darin `docker compose up -d --build`
aus. Der `Dockerfile`-`COPY watcher.py .`-Schritt hat also Zugriff auf alle
Dateien im geklonten Repo, nicht nur auf die Compose-Datei — du brauchst
nichts händisch hochzuladen.

### 3. Ohne Git — Alternativen

Falls du (noch) kein Repo willst:

- **"Web editor"**: Compose-Inhalt reinkopieren, aber dann fehlt der
  Build-Context für `watcher.py`. Workaround: Dockerfile so anpassen, dass es
  `watcher.py` per `curl`/`ADD <raw-url>` aus einem Gist/Raw-Link zieht, oder
  ein fertiges Image auf einer Registry (Docker Hub/GHCR) bauen und im Compose
  nur `image: ghcr.io/<du>/bakkum-watcher:latest` referenzieren — dann reicht
  "Web editor" mit reiner Laufzeit-Compose ohne `build:`.
- **"Upload"**: eine `.tar`-Datei mit allen vier Dateien hochladen — funktioniert
  genauso wie Repository, nur ohne GitOps-Automatik.

Für dein Setup (GitOps schon etabliert) ist die Repository-Methode der
konsistenteste Weg.

### 4. Nach dem Deploy prüfen

```bash
docker ps | grep bakkum
docker logs -f bakkum-watcher
```

Erwartete erste Zeilen: `checking https://... for 2026-07-25 → 2026-07-31`,
gefolgt von einer Liste der erkannten Kategorien mit ✅/❌. Falls stattdessen
`no availability signals parsed` erscheint → Discovery-Lauf (siehe oben) und
Parser nachschärfen.

`state.json` liegt im benannten Volume `bakkum_data` (siehe Compose), überlebt
also Neustarts/Redeploys. Zum Zurücksetzen (z. B. nach Parser-Änderungen):

```bash
docker volume rm bakkum-watcher_bakkum_data   # Name kann je nach Stack-Präfix variieren, mit `docker volume ls` prüfen
```

## Grenzen

- Die Interaktion mit dem Datums-Widget ist best-effort (Selektoren können
  sich ändern). Der Discovery-Dump zeigt, ob das Datum wirklich gesetzt wurde.
- Der Watcher bucht nicht automatisch — bei der Notification schnell selbst
  zuschlagen. (Präferenzgebühr & Kurtaxe kommen laut Seite noch obendrauf.)
