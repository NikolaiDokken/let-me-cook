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

app.jinja_env.filters['fmt_date'] = fmt_date


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


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
        "SELECT raw_json, week_start FROM weeks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    if not week:
        return render_template("shopping.html", week=None, sections={})
    plan = json.loads(week["raw_json"])
    sections = plan.get("shopping_list", {})
    return render_template("shopping.html", week=week["week_start"], sections=sections)


@app.route("/cook/<day>")
def cook(day):
    week, meals = get_current_week()
    if not week:
        return render_template("cook.html", meal=None, week=None, back_url="/")
    meal = next((m for m in meals if m["day"].lower() == day.lower()), None)
    if not meal:
        return render_template("cook.html", meal=None, week=None, back_url="/")
    meal_data = {
        "day": meal["day"],
        "name": meal["name"],
        "description": meal["description"],
        "ingredients": json.loads(meal["ingredients"]),
        "steps": json.loads(meal["steps"]),
    }
    return render_template("cook.html", meal=meal_data, week=week["week_start"], back_url="/")


@app.route("/history/<week_start>/cook/<day>")
def history_cook(week_start, day):
    con = get_db()
    week = con.execute(
        "SELECT * FROM weeks WHERE week_start = ?", (week_start,)
    ).fetchone()
    if not week:
        con.close()
        return render_template("cook.html", meal=None, week=None, back_url=f"/history/{week_start}")
    meal = con.execute(
        "SELECT * FROM meals WHERE week_id = ? AND lower(day) = lower(?)",
        (week["id"], day)
    ).fetchone()
    con.close()
    if not meal:
        return render_template("cook.html", meal=None, week=None, back_url=f"/history/{week_start}")
    meal_data = {
        "day": meal["day"],
        "name": meal["name"],
        "description": meal["description"],
        "ingredients": json.loads(meal["ingredients"]),
        "steps": json.loads(meal["steps"]),
    }
    return render_template("cook.html", meal=meal_data, week=week_start, back_url=f"/history/{week_start}")


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
