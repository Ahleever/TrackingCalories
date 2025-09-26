from __future__ import annotations
import os
import datetime as dt
from functools import wraps
from dataclasses import dataclass
from typing import Optional

from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# -----------------------------------------------------------------------------
# App config
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///caltracker.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    is_admin = db.Column(db.Boolean, default=False)

    profile = db.relationship("Profile", backref="user", uselist=False, cascade="all,delete")
    entries = db.relationship("Entry", backref="user", cascade="all,delete")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Profile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    height_in = db.Column(db.Float)
    weight_lb = db.Column(db.Float)

    age = db.Column(db.Integer)
    sex = db.Column(db.String(1))
    activity = db.Column(db.String(20), default="moderately_active")
    goal = db.Column(db.String(20), default="maintain")  


class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date = db.Column(db.Date, default=dt.date.today, index=True)
    calories_in = db.Column(db.Integer, default=0)
    calories_out = db.Column(db.Integer, default=0)
    weight_lb = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)


@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))

# -----------------------------------------------------------------------------
# Helpers & calculations
# -----------------------------------------------------------------------------
ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "lightly_active": 1.375,
    "moderately_active": 1.55,
    "very_active": 1.725,
    "extra_active": 1.9,
}

def cm_to_m(cm: float) -> float:
    return cm / 100.0

def kg_to_lbs(kg: float) -> float:
    return kg * 2.20462

def lbs_to_kg(lbs: float) -> float:
    return lbs / 2.20462

def inches_to_cm(inches: float) -> float:
    return inches * 2.54

def ft_in_to_cm(ft: float, inch: float) -> float:
    return inches_to_cm(ft * 12 + inch)

@dataclass
class Metrics:
    bmi: Optional[float]
    bmr: Optional[int]
    tdee: Optional[int]
    target_calories: Optional[int]

def calculate_metrics(profile: Optional[Profile]) -> Metrics:
    if not profile or not all([profile.height_in, profile.weight_lb, profile.age, profile.sex]):
        return Metrics(None, None, None, None)

    h = profile.height_in
    w = profile.weight_lb
    a = profile.age
    s = (profile.sex or "").upper()

    # BMI
    bmi = round(w / (cm_to_m(h) ** 2), 1)

    # BMR 
    bmr = 10 * w + 6.25 * h - 5 * a + (5 if s == "M" else -161)

    # TDEE
    mult = ACTIVITY_MULTIPLIERS.get(profile.activity or "moderately_active", 1.55)
    tdee = bmr * mult

    # Target
    if (profile.goal or "maintain").lower() == "lose":
        target = max(int(round(tdee - 500)), int(bmr))  # guardrail: don't suggest below BMR
    else:
        target = int(round(tdee))

    return Metrics(bmi=bmi, bmr=int(round(bmr)), tdee=int(round(tdee)), target_calories=target)

def exercise_recommendations(profile: Optional[Profile]):
    if not profile:
        return []
    goal = (profile.goal or "maintain").lower()
    base = [
        {"name": "Brisk Walking", "intensity": "Moderate", "duration_min": 30, "type": "cardio"},
        {"name": "Jogging", "intensity": "Heavy", "duration_min": 20, "type": "cardio"},
        {"name": "Cycling (leisure)", "intensity": "Low", "duration_min": 30, "type": "cardio"},
        {"name": "Bodyweight Circuit", "intensity": "Moderate", "duration_min": 25, "type": "strength"},
        {"name": "Swimming (moderate)", "intensity": "Moderate", "duration_min": 25, "type": "cardio"},
        {"name": "Pilates/core", "intensity": "Low", "duration_min": 30, "type": "mobility"},
    ]
    if goal == "lose":
        out = []
        for x in base:
            if x["type"] in ("cardio", "strength"):
                y = x.copy()
                y["duration_min"] += 10
                y["note"] = "Fat-loss focus: try intervals."
                out.append(y)
        out.append({"name": "Walk after meals", "met": 3.3, "duration_min": 10, "type": "habit",
                    "note": "Light post-meal walk 2–3x/day"})
        return out
    return base + [{"name": "Full-body strength 2x/week", "met": 5.0, "duration_min": 40, "type": "strength"}]

# -----------------------------------------------------------------------------
# Admin
# -----------------------------------------------------------------------------
def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return view_func(*args, **kwargs)
    return wrapper

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password")
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("Email is already registered.", "error")
            return redirect(url_for("register"))

        u = User(email=email)
        u.set_password(password)

        # Auto-admin if email appears in ADMIN_EMAILS
        admin_emails = [x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()]
        if email in admin_emails:
            u.is_admin = True

        db.session.add(u)
        db.session.commit()

        # Bootstrap empty profile
        db.session.add(Profile(user_id=u.id))
        db.session.commit()

        login_user(u)
        flash("Welcome! Let’s set up your profile.", "success")
        return redirect(url_for("profile"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password")
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Invalid credentials.", "error")
            return redirect(url_for("login"))
        login_user(user)
        flash("Logged in successfully.", "success")
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You are logged out.", "success")
    return redirect(url_for("index"))

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    p = current_user.profile
    if request.method == "POST":
        ft = float(request.form.get("height_ft") or 0)
        inch = float(request.form.get("height_in") or 0)
        lb = float(request.form.get("weight_lb") or 0)

        p.height_in = ft_in_to_cm(ft, inch) if (ft or inch) else None
        p.weight_lb = lbs_to_kg(lb) if lb else None
        p.age = int(request.form.get("age") or 0) or None
        p.sex = (request.form.get("sex") or "").upper() or None
        p.activity = request.form.get("activity") or "moderately_active"
        p.goal = request.form.get("goal") or "maintain"

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("dashboard"))

    metrics = calculate_metrics(p)

    inches = (p.height_in / 2.54) if p and p.height_in else None
    ft = int(inches // 12) if inches else ""
    inch = round(inches % 12, 1) if inches else ""
    lb = round(kg_to_lbs(p.weight_lb), 1) if p and p.weight_lb else ""

    return render_template("profile.html", p=p, metrics=metrics, ft=ft, inch=inch, lb=lb)

@app.route("/dashboard")
@login_required
def dashboard():
    p = current_user.profile
    metrics = calculate_metrics(p)

    start = dt.date.today() - dt.timedelta(days=14)
    recent = (
        Entry.query.filter(Entry.user_id == current_user.id, Entry.date >= start)
        .order_by(Entry.date.desc())
        .all()
    )

    week_start = dt.date.today() - dt.timedelta(days=6)
    week_entries = [e for e in recent if e.date >= week_start]
    week_in = sum(e.calories_in or 0 for e in week_entries)
    week_out = sum(e.calories_out or 0 for e in week_entries)

    recs = exercise_recommendations(p)
    return render_template(
        "dashboard.html",
        recent=recent,
        week_in=week_in,
        week_out=week_out,
        metrics=metrics,
        recs=recs,
    )

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_entry():
    if request.method == "POST":
        date_str = request.form.get("date") or dt.date.today().isoformat()
        date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()

        weight_lb = float(request.form.get("weight_lb") or 0)
        weight_lb = lbs_to_kg(weight_lb) if weight_lb else None

        e = Entry(
            user_id=current_user.id,
            date=date,
            calories_in=int(request.form.get("calories_in") or 0),
            calories_out=int(request.form.get("calories_out") or 0),
            weight_lb=weight_lb,
            notes=request.form.get("notes") or None,
        )
        db.session.add(e)
        db.session.commit()
        flash("Entry added.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add.html", today=dt.date.today().isoformat())

@app.route("/admin")
@admin_required
def admin_panel():
    users = User.query.order_by(User.created_at.desc()).all()
    total_entries = Entry.query.count()
    return render_template("admin.html", users=users, total_entries=total_entries)

# -----------------------------------------------------------------------------
# CLI helper 
# -----------------------------------------------------------------------------
@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database initialized.")

with app.app_context():
    db.create_all()

    app.run(debug=True)
