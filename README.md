# Garmin Running Coach MCP

Personal Garmin Connect MCP server for running coaching: coaching briefs, athlete profile, workouts, and week review tools.

## Quick start (Docker)

```bash
cp .env.example .env
# fill GARMIN_EMAIL / GARMIN_PASSWORD (and optional home lat/lon for weather)
docker compose up --build -d
```

MCP endpoint: `http://localhost:8000/mcp`  
Health: `http://localhost:8000/health`

## Local run

```bash
uv sync --group dev
cp .env.example .env   # fill credentials
cd src && uv run python server.py
```

## Tools

See [docs/garmin-mcp-tools.md](docs/garmin-mcp-tools.md).

## Odysseus / Cursor skills

Skill packs for week review and profile live under [docs/skills/](docs/skills/).

## Tests

```bash
uv sync --group dev
uv run pytest
```

## Env

| Variable | Purpose |
|----------|---------|
| `GARMIN_EMAIL` | Garmin Connect account |
| `GARMIN_PASSWORD` | Garmin Connect password |
| `GARMIN_HOME_LAT` / `GARMIN_HOME_LON` | Optional home coords for weather context |
| `MCP_TRANSPORT` | Default `streamable-http` |
| `MCP_HOST` / `MCP_PORT` | Bind address (default `0.0.0.0:8000`) |
