"""Temporary slot opt-in API for GOAT Sports.

Run:  pip install fastapi uvicorn
      ADMIN_KEY=yoursecret uvicorn main:app --host 0.0.0.0 --port 8000
Docs: http://localhost:8000/docs  (interactive, works from a phone browser)
"""
import os
import sqlite3
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me")
DB_PATH = os.getenv("DB_PATH", "academy.db")
MIN_FULL = int(os.getenv("MIN_FULL", 6))   # players needed for full court
MIN_HALF = int(os.getenv("MIN_HALF", 4))   # players needed for half court
SUB_DAYS = 30

Mode = Literal["full", "half", "solo"]
app = FastAPI(title="GOAT Sports Slots (temporary)")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.on_event("startup")
def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS subscribers(
            phone TEXT PRIMARY KEY, name TEXT, start_date TEXT, end_date TEXT);
        CREATE TABLE IF NOT EXISTS slots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, start TEXT, end TEXT,
            UNIQUE(day, start));
        CREATE TABLE IF NOT EXISTS optins(
            slot_id INTEGER, phone TEXT, mode TEXT,
            PRIMARY KEY(slot_id, phone));
        """)


def admin(key: Optional[str]):
    if key != ADMIN_KEY:
        raise HTTPException(401, "Admin key required (X-Admin-Key header)")


def norm(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-10:]


# ---------- models ----------
class SubscriberIn(BaseModel):
    phone: str
    name: str
    start_date: Optional[date] = None   # defaults to today; valid for 30 days


class SlotIn(BaseModel):
    day: date
    start: str  # "18:00"
    end: str    # "19:00"


class OptIn(BaseModel):
    phone: str
    mode: Mode


# ---------- admin: subscribers & slots ----------
@app.post("/subscribers")
def add_subscriber(s: SubscriberIn, x_admin_key: Optional[str] = Header(None)):
    """Add or renew a subscriber (paid 1500 -> 30 days)."""
    admin(x_admin_key)
    start = s.start_date or date.today()
    end = start + timedelta(days=SUB_DAYS)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO subscribers VALUES (?,?,?,?)",
                  (norm(s.phone), s.name, start.isoformat(), end.isoformat()))
    return {"phone": norm(s.phone), "valid_until": end}


@app.get("/subscribers")
def list_subscribers(x_admin_key: Optional[str] = Header(None)):
    admin(x_admin_key)
    with db() as c:
        rows = c.execute("SELECT * FROM subscribers ORDER BY end_date DESC").fetchall()
    today = date.today().isoformat()
    return [{**dict(r), "active": r["end_date"] >= today} for r in rows]


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "admin.html"))


@app.post("/slots")
def add_slots(slots: list[SlotIn], x_admin_key: Optional[str] = Header(None)):
    """Publish open slots (send a list)."""
    admin(x_admin_key)
    with db() as c:
        for s in slots:
            c.execute("INSERT OR IGNORE INTO slots(day,start,end) VALUES (?,?,?)",
                      (s.day.isoformat(), s.start, s.end))
    return {"added": len(slots)}


@app.delete("/slots/{slot_id}")
def delete_slot(slot_id: int, x_admin_key: Optional[str] = Header(None)):
    admin(x_admin_key)
    with db() as c:
        c.execute("DELETE FROM optins WHERE slot_id=?", (slot_id,))
        c.execute("DELETE FROM slots WHERE id=?", (slot_id,))
    return {"deleted": slot_id}


# ---------- players ----------
def summarize(c, slot) -> dict:
    rows = c.execute(
        "SELECT o.mode, s.name FROM optins o JOIN subscribers s ON s.phone=o.phone "
        "WHERE o.slot_id=?", (slot["id"],)).fetchall()
    counts = {"full": 0, "half": 0, "solo": 0}
    names = {"full": [], "half": [], "solo": []}
    for r in rows:
        counts[r["mode"]] += 1
        names[r["mode"]].append(r["name"])
    if counts["full"] >= MIN_FULL:
        status = "full court on"
    elif counts["full"] + counts["half"] >= MIN_HALF:
        status = "half court on"
    else:
        status = "needs more players"
    return {"slot_id": slot["id"], "day": slot["day"], "start": slot["start"],
            "end": slot["end"], "counts": counts, "players": names, "status": status}


@app.get("/slots")
def list_slots(day: Optional[date] = None):
    """Open slots with live counts. Defaults to today onwards."""
    with db() as c:
        if day:
            slots = c.execute("SELECT * FROM slots WHERE day=? ORDER BY start",
                              (day.isoformat(),)).fetchall()
        else:
            slots = c.execute("SELECT * FROM slots WHERE day>=? ORDER BY day,start",
                              (date.today().isoformat(),)).fetchall()
        return [summarize(c, s) for s in slots]


@app.post("/slots/{slot_id}/optin")
def opt_in(slot_id: int, body: OptIn):
    """Subscriber opts in (or changes mode). Needs an active subscription."""
    phone = norm(body.phone)
    with db() as c:
        slot = c.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise HTTPException(404, "Slot not found")
        sub = c.execute("SELECT * FROM subscribers WHERE phone=?", (phone,)).fetchone()
        if not sub:
            raise HTTPException(403, "Not a subscriber")
        if sub["end_date"] < slot["day"]:
            raise HTTPException(403, f"Subscription ends {sub['end_date']}; renew first")
        c.execute("INSERT OR REPLACE INTO optins VALUES (?,?,?)",
                  (slot_id, phone, body.mode))
        return summarize(c, slot)


@app.delete("/slots/{slot_id}/optin")
def opt_out(slot_id: int, phone: str):
    with db() as c:
        c.execute("DELETE FROM optins WHERE slot_id=? AND phone=?",
                  (slot_id, norm(phone)))
        slot = c.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise HTTPException(404, "Slot not found")
        return summarize(c, slot)


# ---------- player web form (index.html next to this file) ----------
@app.get("/", include_in_schema=False)
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


# ---------- WhatsApp helper ----------
@app.get("/whatsapp/{day}")
def whatsapp_text(day: date):
    """Ready-to-paste WhatsApp message for a day's slots."""
    with db() as c:
        slots = c.execute("SELECT * FROM slots WHERE day=? ORDER BY start",
                          (day.isoformat(),)).fetchall()
        lines = [f"*{day.strftime('%a %d %b')} - Slots*"]
        for s in slots:
            x = summarize(c, s)
            n = x["counts"]
            lines.append(f"\n{s['start']}-{s['end']}: {x['status']}\n"
                         f"Full {n['full']} | Half {n['half']} | Solo {n['solo']}")
    return {"text": "\n".join(lines)}
