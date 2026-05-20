# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A home-server meal planning app for a Norwegian household. Two services run via Docker Compose:

- **Planner** (`planner/generate_plan.py`) — one-shot script, called weekly by cron. Loads `planner/preferences.json`, calls the Claude API to generate a 7-day Norwegian dinner plan + shopping list, saves it to SQLite, and emails it.
- **Web app** (`web/app.py`) — Flask app serving a companion UI to browse the plan, check off shopping items, and follow step-by-step recipes in "Kokkemodus" (cook mode).

## Running the services

```bash
# Start the web app (http://localhost:5000)
docker compose up web --build

# Run the planner once (generate plan + send email)
docker compose run --rm planner

# Run the web app directly without Docker
DB_PATH=data/meals.db flask --app web/app.py run --debug
```

There are no tests or linters configured. To run the planner locally outside Docker:

```bash
DB_PATH=data/meals.db PREFS_PATH=planner/preferences.json python planner/generate_plan.py
```

## Architecture

### Data flow

1. `generate_plan.py` calls Claude (`claude-sonnet-4-6`) with a Norwegian-language system prompt built from `preferences.json`. Claude returns a JSON object with `meals` (array) and `shopping_list` (object keyed by store section).
2. The plan is saved to SQLite in two tables: `weeks` (one row per week, stores the full `raw_json`) and `meals` (one row per day, stores structured fields).
3. The Flask app reads from SQLite and renders Jinja2 templates in `web/templates/`.

### Key design details

- `preferences.json` controls all household-specific config (people count, liked/disliked dishes, stores, day-specific notes). All keys are optional — `PREFS_DEFAULTS` in `generate_plan.py` fills in missing values.
- The shopping list lives only in `weeks.raw_json` (not normalised into its own table). `web/app.py:/shopping` parses it from there.
- Shopping list check-off state and cook-mode step progress are stored in `localStorage` on the client, keyed per week — nothing is written back to the server.
- The planner uses `week_start` (Monday's date, `YYYY-MM-DD`) as the week identifier. Re-running the planner for the same week replaces the existing plan.

### Environment variables

All from `.env` (see `.env.example`):

| Variable | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | planner |
| `EMAIL_ADDRESS` | planner |
| `EMAIL_APP_PASSWORD` | planner (Gmail App Password) |
| `EMAIL_TO` | planner (comma-separated) |
| `DB_PATH` | both (default: `/app/data/meals.db`) |
| `PREFS_PATH` | planner (default: `/app/planner/preferences.json`) |
