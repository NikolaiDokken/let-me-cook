from flask import Flask, render_template
from dotenv import load_dotenv
import os
import json
import sqlite3
from datetime import datetime

load_dotenv()

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "/app/data/meals.db")

_MONTHS_NO = ['januar','februar','mars','april','mai','juni',
              'juli','august','september','oktober','november','desember']

def fmt_date(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.day}. {_MONTHS_NO[d.month - 1]} {d.year}"

def fmt_ing(ing):
    if ing.get('amount') is None:
        return ing['name']
    amt = ing['amount']
    amt_str = str(int(amt)) if amt == int(amt) else f"{float(amt):.1f}"
    if ing.get('unit'):
        return f"{amt_str}{ing['unit']} {ing['name']}"
    return f"{amt_str} {ing['name']}"

def normalize_ingredients(lst):
    result = []
    for ing in lst:
        if isinstance(ing, str):
            result.append({"amount": None, "unit": None, "name": ing})
        else:
            result.append(ing)
    return result

def normalize_shopping_items(lst):
    result = []
    for item in lst:
        if isinstance(item, str):
            result.append({"name": item, "unit": None})
        else:
            result.append(item)
    return result

def fmt_shopping_ing(ing):
    if 'days' not in ing:
        return fmt_ing(ing)
    vals = [v for v in ing['days'].values() if v is not None]
    if not vals:
        return ing['name']
    total = sum(vals)
    amt_str = str(int(total)) if total == int(total) else f"{float(total):.1f}"
    if ing.get('unit'):
        return f"{amt_str}{ing['unit']} {ing['name']}"
    return f"{amt_str} {ing['name']}"

app.jinja_env.filters['fmt_date'] = fmt_date
app.jinja_env.filters['fmt_ing'] = fmt_ing
app.jinja_env.filters['fmt_shopping_ing'] = fmt_shopping_ing


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def migrate_db():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("ALTER TABLE meals ADD COLUMN nutrition TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass
    con.close()

migrate_db()


def get_current_week():
    con = get_db()
    week = con.execute(
        "SELECT * FROM weeks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not week:
        con.close()
        return None, []
    meals = con.execute(
        "SELECT * FROM meals WHERE week_id = ? ORDER BY id",
        (week["id"],)
    ).fetchall()
    con.close()
    return week, meals


@app.route("/")
def index():
    week, meals = get_current_week()
    if not week:
        return render_template("index.html", week=None, meals=[])
    meals_list = [
        {
            "day": m["day"],
            "name": m["name"],
            "description": m["description"],
        }
        for m in meals
    ]
    return render_template("index.html", week=week["week_start"], meals=meals_list)


@app.route("/shopping")
def shopping():
    con = get_db()
    week = con.execute(
        "SELECT id, raw_json, week_start FROM weeks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not week:
        con.close()
        return render_template("shopping.html", week=None, sections={}, base_people=None, days=[])
    meals = con.execute(
        "SELECT day FROM meals WHERE week_id = ? ORDER BY id",
        (week["id"],)
    ).fetchall()
    con.close()
    plan = json.loads(week["raw_json"])
    sections = {k: normalize_shopping_items(v) for k, v in plan.get("shopping_list", {}).items()}
    base_people = plan.get("base_people")
    days = [m["day"] for m in meals]
    return render_template("shopping.html", week=week["week_start"], sections=sections, base_people=base_people, days=days)


@app.route("/cook/<day>")
def cook(day):
    week, meals = get_current_week()
    if not week:
        return render_template("cook.html", meal=None, week=None, back_url="/", base_people=None)
    meal = next((m for m in meals if m["day"].lower() == day.lower()), None)
    if not meal:
        return render_template("cook.html", meal=None, week=None, back_url="/", base_people=None)
    plan = json.loads(week["raw_json"])
    meal_data = {
        "day": meal["day"],
        "name": meal["name"],
        "description": meal["description"],
        "ingredients": normalize_ingredients(json.loads(meal["ingredients"])),
        "steps": json.loads(meal["steps"]),
        "nutrition": json.loads(meal["nutrition"]) if meal["nutrition"] else None,
    }
    return render_template("cook.html", meal=meal_data, week=week["week_start"], back_url="/", base_people=plan.get("base_people"))


@app.route("/history/<week_start>/cook/<day>")
def history_cook(week_start, day):
    con = get_db()
    week = con.execute(
        "SELECT * FROM weeks WHERE week_start = ?", (week_start,)
    ).fetchone()
    if not week:
        con.close()
        return render_template("cook.html", meal=None, week=None, back_url=f"/history/{week_start}", base_people=None)
    meal = con.execute(
        "SELECT * FROM meals WHERE week_id = ? AND lower(day) = lower(?)",
        (week["id"], day)
    ).fetchone()
    con.close()
    if not meal:
        return render_template("cook.html", meal=None, week=None, back_url=f"/history/{week_start}", base_people=None)
    plan = json.loads(week["raw_json"])
    meal_data = {
        "day": meal["day"],
        "name": meal["name"],
        "description": meal["description"],
        "ingredients": normalize_ingredients(json.loads(meal["ingredients"])),
        "steps": json.loads(meal["steps"]),
        "nutrition": json.loads(meal["nutrition"]) if meal["nutrition"] else None,
    }
    return render_template("cook.html", meal=meal_data, week=week_start, back_url=f"/history/{week_start}", base_people=plan.get("base_people"))


@app.route("/history")
def history():
    con = get_db()
    rows = con.execute(
        "SELECT w.week_start, GROUP_CONCAT(m.name, '||') as names "
        "FROM weeks w JOIN meals m ON m.week_id = w.id "
        "GROUP BY w.id ORDER BY w.id DESC"
    ).fetchall()
    con.close()
    weeks = [
        {
            "week_start": r["week_start"],
            "meals": r["names"].split("||") if r["names"] else [],
        }
        for r in rows
    ]
    return render_template("history.html", weeks=weeks)


@app.route("/history/<week_start>")
def history_week(week_start):
    con = get_db()
    week = con.execute(
        "SELECT * FROM weeks WHERE week_start = ?", (week_start,)
    ).fetchone()
    if not week:
        con.close()
        return render_template("history_week.html", week=None, meals=[])
    meals = con.execute(
        "SELECT day, name, description FROM meals WHERE week_id = ? ORDER BY id",
        (week["id"],)
    ).fetchall()
    con.close()
    return render_template(
        "history_week.html",
        week=week_start,
        meals=[dict(m) for m in meals],
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
