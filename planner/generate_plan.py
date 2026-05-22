"""
generate_plan.py
-------------
A one-shot script that:
  1. Loads meal preferences from preferences.json
  2. Calls the Claude API (twice) to generate a 7-day Norwegian dinner plan + shopping list
  3. Saves the plan to a SQLite database
  4. Sends a formatted email with the plan

Scheduling is handled externally by cron.
Run directly to test: python generate_plan.py
"""

import os
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
PREFS_DEFAULTS = {
    "language": "norwegian",
    "location": "Norway",
    "stores": ["Rema 1000", "Kiwi", "Meny"],
    "people": 2,
    "liked_dishes": [],
    "disliked_dishes": [],
    "dietary_notes": "",
    "cuisine_mix": "A mix of traditional Norwegian dishes and international weeknight meals"
}

def load_preferences() -> dict:
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            user_prefs = json.load(f)
        prefs = {**PREFS_DEFAULTS, **user_prefs}
        log.info("Loaded preferences from %s", PREFS_PATH)
    except FileNotFoundError:
        prefs = PREFS_DEFAULTS
        log.warning("preferences.json not found at %s — using defaults", PREFS_PATH)
    return prefs


# ─── Tool schemas ─────────────────────────────────────────────────────────────
# Two separate tools for two separate calls:
#   1. MEALS_TOOL   — creative call, generates 7 meals with per-meal ingredients
#   2. SHOPPING_TOOL — consolidation call, given the meals as context

MEALS_TOOL = {
    "name": "save_meals",
    "description": "Lagre de 7 genererte middagene.",
    "input_schema": {
        "type": "object",
        "properties": {
            "meals": {
                "type": "array",
                "description": "Liste over 7 middager, én per ukedag",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {
                            "type": "string",
                            "description": "Ukedag på norsk, f.eks. 'Mandag'"
                        },
                        "name":        {"type": "string"},
                        "description": {"type": "string", "description": "Én setning beskrivelse"},
                        "ingredients": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "amount": {
                                        "type": ["number", "null"],
                                        "description": "null for ingredienser uten fast mengde (salt, pepper)"
                                    },
                                    "unit": {
                                        "type": ["string", "null"],
                                        "description": "null for heltalls-enheter (løk, egg, fedd hvitløk)"
                                    },
                                    "name": {"type": "string"}
                                },
                                "required": ["amount", "unit", "name"]
                            }
                        },
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "nutrition": {
                            "type": "object",
                            "properties": {
                                "calories":  {"type": "number"},
                                "protein_g": {"type": "number"},
                                "carbs_g":   {"type": "number"},
                                "fat_g":     {"type": "number"}
                            },
                            "required": ["calories", "protein_g", "carbs_g", "fat_g"]
                        }
                    },
                    "required": ["day", "name", "description", "ingredients", "steps", "nutrition"]
                }
            }
        },
        "required": ["meals"]
    }
}

SHOPPING_TOOL = {
    "name": "save_shopping_list",
    "description": "Lagre den konsoliderte handlelisten gruppert etter butikkavdeling.",
    "input_schema": {
        "type": "object",
        "properties": {
            "shopping_list": {
                "type": "object",
                "description": "Handleliste gruppert etter butikkavdeling (norsk navn)",
                "additionalProperties": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "unit": {"type": ["string", "null"]},
                            "days": {
                                "type": "object",
                                "description": "Mengde per dag. Kun dagene som bruker ingrediensen. null for ingredienser uten fast mengde.",
                                "additionalProperties": {"type": ["number", "null"]}
                            }
                        },
                        "required": ["name", "unit", "days"]
                    }
                }
            }
        },
        "required": ["shopping_list"]
    }
}


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


def get_recent_meals(n_weeks: int = 4, exclude_week_start: str = None) -> dict:
    """Return {week_start: [meal_name, ...]} for the last n_weeks, most recent first."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    query = "SELECT w.week_start, m.name FROM meals m JOIN weeks w ON m.week_id = w.id "
    params = []
    if exclude_week_start:
        query += "WHERE w.week_start != ? "
        params.append(exclude_week_start)
    query += "ORDER BY w.id DESC LIMIT ?"
    params.append(n_weeks * 7)
    rows = con.execute(query, params).fetchall()
    con.close()

    weeks: dict = {}
    for row in rows:
        ws = row["week_start"]
        if ws not in weeks:
            weeks[ws] = []
        weeks[ws].append(row["name"])
    return dict(list(weeks.items())[:n_weeks])


# ─── Claude API ───────────────────────────────────────────────────────────────

def build_meals_prompt(prefs: dict, recent_meals: dict = None) -> str:
    liked    = ", ".join(prefs["liked_dishes"])    if prefs["liked_dishes"]    else "ikke spesifisert"
    disliked = ", ".join(prefs["disliked_dishes"]) if prefs["disliked_dishes"] else "ingen"
    dietary  = prefs["dietary_notes"] or "ingen"

    recent_section = ""
    if recent_meals:
        lines = [f"- Uke {ws}: {', '.join(names)}" for ws, names in recent_meals.items()]
        recent_section = "\nSiste ukers middager (ikke gjenta disse):\n" + "\n".join(lines) + "\n"

    return f"""Du er en matplanlegger for en husstand i {prefs['location']}.
Bruk save_meals-verktøyet for å returnere planen.

Kontekst:
- Antall personer: {prefs['people']}
- Butikker tilgjengelig: {", ".join(prefs['stores'])}
- Retter de liker som kan brukes som inspirasjon: {liked}
- Retter som skal unngås: {disliked}
- Kostholdskommentarer: {dietary}
{recent_section}
Regler:
- Planlegg 7 middager (Mandag–Søndag) på norsk
- Bruk ingredienser som er lett tilgjengelig i norske dagligvarebutikker
- Foretrekk sesongbaserte norske råvarer der det er naturlig
- Maks 45 minutter tilberedningstid på hverdager
- Tilpass porsjoner for {prefs['people']} personer
- Bruk norske produktnavn og mål (g, kg, dl, ss, ts)
- Ingredienser: amount er null for ingredienser uten fast mengde (salt, pepper, vann); unit er null for heltalls-enheter (løk, egg, fedd hvitløk)
- Velg retter som deler ingredienser for å unngå matsvinn, men hver rett må fungere selvstendig — ingen rett skal kreve rester fra en annen dag
- Velg sunne og rimelige middagsretter
- Estimer næringsinnhold per porsjon (per person): kalorier (kcal), protein (g), karbohydrater (g) og fett (g)"""


BUILD_SHOPPING_PROMPT = """Du er en matplanlegger. Du får en ferdig middagsplan og skal lage en konsolidert handleliste.
Bruk save_shopping_list-verktøyet for å returnere handlelisten.

Regler:
- Grupper ingredienser etter butikkavdeling på norsk (f.eks. "Frukt og grønt", "Kjøtt og fisk", "Meieri", "Tørrvarer")
- Konsolider like ingredienser på tvers av dager under ett navn og én enhet
- For hver vare: bruk "days"-objekt med nøyaktig mengde per dag — hentet direkte fra middagsplanen
- Ta kun med dagene som faktisk bruker ingrediensen
- Bruk null som mengde for ingredienser uten fast mengde (salt, pepper, olje, vann)
- Behold original enhet fra oppskriften (g, kg, dl, ss, ts, eller null for heltalls-enheter)"""


def generate_meals(client: anthropic.Anthropic, prefs: dict, recent_meals: dict = None) -> list:
    """Call 1: generate 7 meals with per-meal ingredients. Pure creativity, no aggregation."""
    log.info("Call 1: generating meals...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        tools=[MEALS_TOOL],
        tool_choice={"type": "tool", "name": "save_meals"},
        system=build_meals_prompt(prefs, recent_meals),
        messages=[{"role": "user", "content": "Lag en variert middagsplan for uken."}]
    )
    tool_block = next(b for b in message.content if b.type == "tool_use")
    meals = tool_block.input["meals"]
    log.info("Call 1 done: %d meals generated", len(meals))
    return meals


def generate_shopping_list(client: anthropic.Anthropic, meals: list) -> dict:
    """Call 2: consolidate meals into a shopping list. Meals are provided as concrete facts."""
    log.info("Call 2: consolidating shopping list...")
    meals_json = json.dumps({"meals": meals}, ensure_ascii=False, indent=2)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=[SHOPPING_TOOL],
        tool_choice={"type": "tool", "name": "save_shopping_list"},
        system=BUILD_SHOPPING_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Her er middagsplanen:\n\n{meals_json}\n\nLag handlelisten."
        }]
    )
    tool_block = next(b for b in message.content if b.type == "tool_use")
    shopping_list = tool_block.input["shopping_list"]
    log.info("Call 2 done: %d sections", len(shopping_list))
    return shopping_list


def generate_meal_plan(prefs: dict, recent_meals: dict = None) -> dict:
    """Orchestrate both API calls and return the combined plan dict."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    meals         = generate_meals(client, prefs, recent_meals)
    shopping_list = generate_shopping_list(client, meals)
    return {"meals": meals, "shopping_list": shopping_list}


# ─── Email ────────────────────────────────────────────────────────────────────

def send_failure_email(week_start: str, error: str):
    """Send a plain-text alert when plan generation fails."""
    if not all([EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_TO]):
        log.warning("Email credentials not configured — skipping failure notification")
        return
    try:
        msg = MIMEText(f"Matplanleggeren feilet for uke {week_start}:\n\n{error}")
        msg["Subject"] = f"Matplanlegger feil — uke fra {week_start}"
        msg["From"]    = EMAIL_ADDRESS
        recipients     = [addr.strip() for addr in EMAIL_TO.split(",")]
        msg["To"]      = ", ".join(recipients)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, recipients, msg.as_string())
        log.info("Failure notification sent")
    except Exception:
        log.exception("Failed to send failure notification email")


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
            Middagsplan — uke fra {week_start}
        </h1>
        <table style="width:100%;border-collapse:collapse;border:1px solid #eee">
            {meals_html}
        </table>
        <div style="text-align:center;margin-top:32px">
            <a href="http://localhost:5000/shopping"
               style="display:inline-block;background:#2d6a4f;color:#fff;text-decoration:none;
                      padding:14px 28px;border-radius:6px;font-size:16px;font-weight:bold">
                Se handleliste
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
    msg["Subject"] = f"Middagsplan — uke fra {week_start}"
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
        recent_meals = get_recent_meals(n_weeks=4, exclude_week_start=week_start)
        if recent_meals:
            log.info("Passing %d weeks of history to prompt", len(recent_meals))
        plan = generate_meal_plan(prefs, recent_meals)
        plan["base_people"] = prefs["people"]
        save_plan(week_start, plan)
        send_email(plan, week_start)
        log.info("Weekly job completed successfully")
    except smtplib.SMTPException as e:
        log.error("Email failed: %s", e)
    except Exception as e:
        log.error("Unexpected error: %s", e)
        send_failure_email(week_start, str(e))
        raise


if __name__ == "__main__":
    init_db()
    run_weekly_job()
