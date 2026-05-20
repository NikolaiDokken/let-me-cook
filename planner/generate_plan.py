"""
weekly_job.py
-------------
A one-shot script that:
  1. Loads meal preferences from preferences.json
  2. Calls the Claude API to generate a 7-day meal plan + shopping list
  3. Saves the plan to a SQLite database
  4. Sends a formatted email with the plan

Scheduling is handled externally by cron.
Run directly to test: python weekly_job.py
"""

import os
import re
import json
import sqlite3
import smtplib
import logging
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
EMAIL_ADDRESS     = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD    = os.getenv("EMAIL_APP_PASSWORD")
EMAIL_TO          = os.getenv("EMAIL_TO")
DB_PATH           = os.getenv("DB_PATH", "/app/data/meals.db")
PREFS_PATH        = os.getenv("PREFS_PATH", "/app/planner/preferences.json")


# ─── Preferences ──────────────────────────────────────────────────────────────
# Loaded from preferences.json so household members can tweak without touching code.
# Any key missing from the file falls back to the defaults below.

PREFS_DEFAULTS = {
    "language": "norwegian",
    "location": "Norway",
    "stores": ["Rema 1000", "Kiwi", "Meny"],
    "people": 2,
    "liked_dishes": [],
    "disliked_dishes": [],
    "dietary_notes": "",
    "cuisine_mix": "A mix of traditional Norwegian dishes and international weeknight meals",
    "day_notes": {
        "Fredag": "Noe enkelt eller en liten fredagsgodbid — gjerne fredagstaco"
    }
}

def load_preferences() -> dict:
    """
    Load preferences.json and merge with defaults.
    Missing keys fall back to defaults, so the file can be as minimal as you like.
    """
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            user_prefs = json.load(f)
        prefs = {**PREFS_DEFAULTS, **user_prefs}
        log.info("Loaded preferences from %s", PREFS_PATH)
    except FileNotFoundError:
        prefs = PREFS_DEFAULTS
        log.warning("preferences.json not found at %s — using defaults", PREFS_PATH)
    return prefs


# ─── Database ─────────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist. Safe to call on every run."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS weeks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start  TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            raw_json    TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            week_id      INTEGER NOT NULL REFERENCES weeks(id),
            day          TEXT NOT NULL,
            name         TEXT NOT NULL,
            description  TEXT,
            ingredients  TEXT,   -- JSON list
            steps        TEXT,   -- JSON list
            nutrition    TEXT    -- JSON object
        )
    """)
    try:
        con.execute("ALTER TABLE meals ADD COLUMN nutrition TEXT")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()
    log.info("Database ready at %s", DB_PATH)


def save_plan(week_start: str, plan: dict) -> int:
    """Save the week plan and individual meals, replacing any existing plan for the same week."""
    con = sqlite3.connect(DB_PATH)
    existing = con.execute("SELECT id FROM weeks WHERE week_start = ?", (week_start,)).fetchone()
    if existing:
        con.execute("DELETE FROM meals WHERE week_id = ?", (existing[0],))
        con.execute("DELETE FROM weeks WHERE id = ?", (existing[0],))
        log.info("Replaced existing plan for week starting %s", week_start)
    cur = con.execute(
        "INSERT INTO weeks (week_start, raw_json) VALUES (?, ?)",
        (week_start, json.dumps(plan))
    )
    week_id = cur.lastrowid
    for meal in plan["meals"]:
        con.execute(
            """INSERT INTO meals (week_id, day, name, description, ingredients, steps, nutrition)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                week_id,
                meal["day"],
                meal["name"],
                meal.get("description", ""),
                json.dumps(meal.get("ingredients", [])),
                json.dumps(meal.get("steps", [])),
                json.dumps(meal["nutrition"]) if meal.get("nutrition") else None
            )
        )
    con.commit()
    con.close()
    log.info("Saved plan for week starting %s (week_id=%d)", week_start, week_id)
    return week_id


# ─── Claude API ───────────────────────────────────────────────────────────────

def build_system_prompt(prefs: dict) -> str:
    """
    Build the system prompt dynamically from preferences.
    The more detail in preferences.json, the better the output.
    """
    liked    = ", ".join(prefs["liked_dishes"])    if prefs["liked_dishes"]    else "ikke spesifisert"
    disliked = ", ".join(prefs["disliked_dishes"]) if prefs["disliked_dishes"] else "ingen"
    dietary  = prefs["dietary_notes"] or "ingen"


    return f"""Du er en matplanlegger for en husstand i {prefs['location']}.
Svar KUN med et gyldig JSON-objekt — ingen markdown, ingen forklaring, ingen kodeblokker.

Kontekst:
- Antall personer: {prefs['people']}
- Butikker tilgjengelig: {", ".join(prefs['stores'])}
- Retter de liker som kan brukes som inspirasjon: {liked}
- Retter som skal unngås: {disliked}
- Kostholdskommentarer: {dietary}

Regler:
- Planlegg 7 middager (Mandag–Søndag) på {prefs['language']}
- Bruk ingredienser som er lett tilgjengelig i norske dagligvarebutikker
- Foretrekk sesongbaserte norske råvarer der det er naturlig
- Bruk likte retter som inspirasjon, men ikke gjenta samme rett to uker på rad
- Maks 45 minutter tilberedningstid på hverdager
- Tilpass porsjoner for {prefs['people']} personer
- Bruk norske produktnavn og mål (g, kg, dl, ss, ts)
- Ingredienser bruker strukturert format: {{"amount": tall|null, "unit": enhet|null, "name": navn}}
- amount er null for ingredienser uten fast mengde (salt, pepper, vann etter smak); unit er null for ingredienser som telles i hele enheter (løk, egg, fedd hvitløk)
- Konsolider like ingredienser i handlelisten
- Grupper handlelisten etter butikkavdeling på norsk
- Velg retter som deler ingredienser for å unngå matsvinn, men hver rett må fungere selvstendig — ingen rett skal kreve rester eller forhåndstilberedt mat fra en annen dag, slik at rekkefølgen fritt kan endres
- Velg sunne og rimelige middagsretter
- Estimer næringsinnhold per porsjon (per person): kalorier (kcal), protein (g), karbohydrater (g) og fett (g)

JSON-strukturen må følge dette nøyaktig:
{{
  "meals": [
    {{
      "day": "Mandag",
      "name": "Navn på rett",
      "description": "Én setning beskrivelse",
      "ingredients": [
        {{"amount": 400, "unit": "g", "name": "kyllingfilet"}},
        {{"amount": 1, "unit": null, "name": "løk"}},
        {{"amount": null, "unit": null, "name": "salt og pepper"}}
      ],
      "steps": ["Steg 1 tekst", "Steg 2 tekst", "..."],
      "nutrition": {{"calories": 650, "protein_g": 35, "carbs_g": 75, "fat_g": 22}}
    }}
  ],
  "shopping_list": {{
    "Frukt og grønt": [
      {{"amount": 1, "unit": null, "name": "løk"}},
      {{"amount": 2, "unit": null, "name": "fedd hvitløk"}}
    ],
    "Kjøtt og fisk": [
      {{"amount": 400, "unit": "g", "name": "kyllingfilet"}}
    ],
    "Meieri": [
      {{"amount": 1, "unit": "dl", "name": "fløte"}}
    ],
    "Tørrvarer": [
      {{"amount": 1, "unit": "boks", "name": "hakkede tomater"}},
      {{"amount": null, "unit": null, "name": "olivenolje"}}
    ]
  }}
}}"""


def generate_meal_plan(prefs: dict) -> dict:
    """Call Claude with the preferences-aware prompt and return the parsed plan."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    log.info("Calling Claude API...")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=build_system_prompt(prefs),
        messages=[
            {
                "role": "user",
                "content": "Lag en variert middagsplan for uken."
            }
        ]
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)
        raw = raw.strip()

    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        log.error("Claude returned invalid JSON. Raw response:\n%s", raw)
        raise

    log.info("Meal plan generated with %d meals", len(plan["meals"]))
    return plan


# ─── Email ────────────────────────────────────────────────────────────────────

def build_email_html(plan: dict, week_start: str) -> str:
    meals_html = ""
    for meal in plan["meals"]:
        meals_html += f"""
        <tr>
            <td style="padding:12px;font-weight:bold;vertical-align:top;width:110px;
                       border-bottom:1px solid #eee">
                {meal['day']}
            </td>
            <td style="padding:12px;vertical-align:top;border-bottom:1px solid #eee">
                <strong>{meal['name']}</strong><br>
                <span style="color:#666;font-size:14px">{meal['description']}</span>
            </td>
        </tr>"""

    return f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:auto;color:#333;padding:20px">
        <h1 style="color:#2d6a4f;border-bottom:2px solid #2d6a4f;padding-bottom:8px">
            🥗 Middagsplan — uke fra {week_start}
        </h1>
        <table style="width:100%;border-collapse:collapse;border:1px solid #eee">
            {meals_html}
        </table>
        <div style="text-align:center;margin-top:32px">
            <a href="http://localhost:5000/shopping"
               style="display:inline-block;background:#2d6a4f;color:#fff;text-decoration:none;
                      padding:14px 28px;border-radius:6px;font-size:16px;font-weight:bold">
                🛒 Se handleliste
            </a>
        </div>
        <p style="color:#aaa;font-size:12px;margin-top:40px;border-top:1px solid #eee;padding-top:12px">
            Generert av din matplanlegger
        </p>
    </body></html>"""


def build_email_text(plan: dict, week_start: str) -> str:
    lines = [f"MIDDAGSPLAN — uke fra {week_start}", "=" * 40]
    for meal in plan["meals"]:
        lines.append(f"{meal['day']}: {meal['name']}")
        lines.append(f"  {meal['description']}\n")
    lines += ["", "Se handleliste: http://localhost:5000/shopping"]
    return "\n".join(lines)


def send_email(plan: dict, week_start: str):
    """Send the meal plan email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🥗 Middagsplan — uke fra {week_start}"
    msg["From"]    = EMAIL_ADDRESS
    recipients = [addr.strip() for addr in EMAIL_TO.split(",")]
    msg["To"]      = ", ".join(recipients)

    msg.attach(MIMEText(build_email_text(plan, week_start), "plain"))
    msg.attach(MIMEText(build_email_html(plan, week_start), "html"))

    log.info("Sending email to %s...", msg["To"])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, recipients, msg.as_string())
    log.info("Email sent successfully")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_weekly_job():
    log.info("Starting weekly meal plan job...")

    today      = date.today()
    monday     = today - timedelta(days=today.weekday())
    week_start = monday.strftime("%Y-%m-%d")

    prefs = load_preferences()

    try:
        plan = generate_meal_plan(prefs)
        plan["base_people"] = prefs["people"]
        save_plan(week_start, plan)
        send_email(plan, week_start)
        log.info("Weekly job completed successfully")
    except json.JSONDecodeError as e:
        log.error("Claude returned invalid JSON: %s", e)
    except smtplib.SMTPException as e:
        log.error("Email failed: %s", e)
    except Exception as e:
        log.error("Unexpected error: %s", e)
        raise


if __name__ == "__main__":
    init_db()
    run_weekly_job()