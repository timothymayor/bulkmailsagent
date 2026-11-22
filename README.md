# bulkmailsagent

Personalised bulk email over your own SMTP account, with a local web UI, resumable
campaigns, batch sending and PDF/Word reports.

## Run locally (Python)

```
pip install -r requirements.txt
copy .env.example .env      # then fill in SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ...
python server.py            # web UI at http://127.0.0.1:8025
```

Command line:

```
python bulkmail.py recipients.csv templates/hr_compliance.txt              # dry run
python bulkmail.py recipients.csv templates/hr_compliance.txt --send --limit 180 --rate 3
```

Re-running the same campaign skips everyone already emailed (tracked in `sent_log.csv`).

## Run with Docker

Needs Docker Desktop (Windows/macOS) or Docker Engine (Linux).

```
docker build -t bulkmailsagent . # build (or rebuild after code changes)
docker compose up -d             # web UI at http://127.0.0.1:8025
docker compose logs -f           # watch output
docker compose down              # stop
```

If `docker compose build` stops with `additional privileges requested: pass
"--allow=fs.read=Dockerfile"`, use `docker build` as above, or set
`COMPOSE_BAKE=false` first (PowerShell: `$env:COMPOSE_BAKE="false"`).

The project folder is mounted into the container at `/data`, so the container uses
the same `.env`, `templates/`, `sent_log.csv`, `unsubscribed.txt` and `reports/` as a
local run, and nothing is lost when the image is rebuilt. Settings saved in the web UI
are written back to `.env`.

Command-line sends inside the container (put the recipients CSV in the project folder
first; paths are relative to it):

```
docker compose run --rm bulkmail python /app/bulkmail.py Members_Mailing_List.csv templates/hr_compliance.txt --send --limit 180 --rate 3
```

Notes:

- The web UI has no login, so the port is published on `127.0.0.1` only. Don't change
  it to `8025:8025` or anyone on your network could send email as you.
- Run either the Docker container or the local `python server.py`, not both: they use
  the same port, and the "one send per campaign" lock doesn't work between the
  container and Windows.
- With `restart: unless-stopped`, the container starts again whenever Docker Desktop
  starts, until you run `docker compose down`.
