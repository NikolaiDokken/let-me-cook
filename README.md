# Meal Planner

A Raspberry Pi–hosted meal planning app. Every week a planner calls the Claude API to generate a 7-day Norwegian dinner plan and emails it. A companion web app lets you browse the plan, check off shopping items, and follow step-by-step recipes.

## Stack

- **Planner** — Python + Anthropic SDK, generates the weekly plan and sends it by email
- **Web app** — Python + Flask, serves the companion UI
- **Database** — SQLite, stores all weekly plans and meals
- **Infrastructure** — Docker Compose, runs both services on the Pi

## Setup

**1. Clone and configure**

```bash
cp .env.example .env
```

Fill in your values in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
EMAIL_ADDRESS=you@gmail.com
EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_TO=you@gmail.com
```

`EMAIL_APP_PASSWORD` is a Gmail App Password, generated at Google Account → Security → App passwords.

**2. Customise preferences**

```bash
cp planner/preferences.example.json planner/preferences.json
```

Edit `preferences.json` to set the number of people, liked/disliked dishes, dietary notes, and available stores. All keys are optional — missing ones fall back to defaults.

**3. Run**

```bash
# Start the web app
docker compose up web --build

# Generate this week's plan and email it
docker compose run --rm planner
```

The web app is available at `http://<pi-ip>:5000` on your local network.

## Web app

| Route | Description |
|---|---|
| `/` | This week's meal overview |
| `/shopping` | Shopping list with tap-to-check items |
| `/cook/<day>` | Step-by-step recipe for a day |
| `/history` | All previous weeks |
| `/history/<date>/cook/<day>` | Recipe from a previous week |

**Cook mode** — on any recipe page, tap *Kokkemodus* to enter a fullscreen step-by-step view. Tap the right half of the screen to advance, left half to go back.

Shopping list and step progress are saved in the browser (`localStorage`), keyed per week, so state persists across page reloads.

## Scheduling on the Pi

To generate the plan automatically every Monday morning, add a cron job on the Pi:

```bash
crontab -e
```

```
0 7 * * 1 docker compose -f /home/<user>/meal-planner/docker-compose.yml run --rm planner
```

