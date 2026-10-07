import json
import math
import os
import random
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.1:8b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
FOUND_RADIUS_M = 100  # how close you must be to claim a quest
NUM_QUESTS = 3

db_url = os.getenv("DATABASE_URL") or "sqlite:///./wanderquest.db"
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


class Hunt(Base):
    __tablename__ = "hunts"
    id = Column(Integer, primary_key=True)
    lat = Column(Float)
    lon = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)


class Quest(Base):
    __tablename__ = "quests"
    id = Column(Integer, primary_key=True)
    hunt_id = Column(Integer, ForeignKey("hunts.id"))
    name = Column(String(255))
    kind = Column(String(120))
    lat = Column(Float)
    lon = Column(Float)
    riddle = Column(Text)
    found = Column(Boolean, default=False)
    found_at = Column(DateTime, nullable=True)


Base.metadata.create_all(engine)
app = FastAPI(title="WanderQuest")


class HuntRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius: int = Field(default=1000, ge=200, le=3000)


class FoundRequest(BaseModel):
    lat: float
    lon: float


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def compass(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    names = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
    return names[int((deg + 22.5) // 45) % 8]


def describe(tags):
    if tags.get("historic"):
        return "historic " + tags["historic"].replace("_", " ")
    if tags.get("amenity") == "fountain":
        return "fountain"
    if tags.get("natural") == "tree":
        return "tree" + (f" ({tags['species']})" if tags.get("species") else "")
    if tags.get("tourism"):
        return tags["tourism"].replace("_", " ")
    return "landmark"


async def fetch_pois(lat, lon, radius):
    q = f"""[out:json][timeout:20];
(
  nwr(around:{radius},{lat},{lon})[historic][name];
  nwr(around:{radius},{lat},{lon})[tourism~"artwork|attraction|viewpoint"][name];
  nwr(around:{radius},{lat},{lon})[amenity=fountain];
  nwr(around:{radius},{lat},{lon})[natural=tree][species];
);
out center 40;"""
    headers = {"User-Agent": "WanderQuest/1.0 (DEV Hacktoberfest challenge project)", "Accept": "*/*"}
    elements = None
    errors = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                 headers=headers, follow_redirects=True) as c:
        for url in OVERPASS_URLS:
            try:
                r = await c.post(url, data={"data": q})
                r.raise_for_status()
                elements = r.json().get("elements", [])
                break
            except Exception as e:
                msg = f"{url.split('/')[2]}: {type(e).__name__} {e}"
                print("Overpass error ->", msg)
                errors.append(msg)
    if elements is None:
        raise HTTPException(502, "OpenStreetMap lookup failed: " + " | ".join(errors))

    pois = []
    for el in elements:
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plon = el.get("lon") or (el.get("center") or {}).get("lon")
        if plat is None or plon is None:
            continue
        tags = el.get("tags", {})
        kind = describe(tags)
        name = tags.get("name") or tags.get("species") or kind
        d = haversine(lat, lon, plat, plon)
        if d < 60:  # too close to be a fun walk
            continue
        pois.append({"name": name, "kind": kind, "lat": plat, "lon": plon,
                     "distance_m": round(d), "direction": compass(lat, lon, plat, plon)})
    return pois


def pick_targets(pois):
    random.shuffle(pois)
    chosen, seen = [], set()
    for p in pois:
        key = (round(p["lat"], 4), round(p["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        chosen.append(p)
        if len(chosen) == NUM_QUESTS:
            break
    return sorted(chosen, key=lambda p: p["distance_m"])


SYSTEM = (
    "You are an adventurous, playful guide who writes scavenger-hunt riddles. "
    "For each target write a cryptic 4-line riddle in two rhyming couplets (AABB) that hints at the place WITHOUT "
    "ever saying its name or any word from its name. Mention the compass direction and rough "
    "distance, and end by reminding the walker to look up from their screen. "
    'Reply ONLY with JSON: {"riddles":[{"id":0,"riddle":"line1\\nline2\\nline3\\nline4"}]}'
)


def parse_json(text):
    text = text.strip().replace("```json", "").replace("```", "")
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


async def make_riddles(targets):
    items = [{"id": i, "secret_name": t["name"], "kind": t["kind"],
              "direction": t["direction"], "distance_m": t["distance_m"]} for i, t in enumerate(targets)]
    fallback = {i: f"Walk about {t['distance_m']} m to the {t['direction']}.\n"
                   f"A {t['kind']} is waiting there,\nso pocket your phone and look up with care,\n"
                   "the world is your map out there!" for i, t in enumerate(targets)}
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "temperature": 0.9,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": json.dumps(items)},
                    ],
                },
            )
            r.raise_for_status()
            data = parse_json(r.json()["choices"][0]["message"]["content"])
        out = {int(x["id"]): str(x["riddle"]) for x in data["riddles"]}
        return {i: out.get(i, fallback[i]) for i in range(len(targets))}
    except Exception as e:
        print("LLM error, using fallback riddles:", e)
        return fallback


@app.post("/api/hunt")
async def create_hunt(req: HuntRequest):
    pois = await fetch_pois(req.lat, req.lon, req.radius)
    if len(pois) < 1:
        raise HTTPException(404, "No interesting places found nearby. Try a bigger area or a park/town centre.")
    targets = pick_targets(pois)
    riddles = await make_riddles(targets)
    with SessionLocal() as db:
        hunt = Hunt(lat=req.lat, lon=req.lon)
        db.add(hunt)
        db.flush()
        quests = []
        for i, t in enumerate(targets):
            q = Quest(hunt_id=hunt.id, name=t["name"], kind=t["kind"],
                      lat=t["lat"], lon=t["lon"], riddle=riddles[i])
            db.add(q)
            quests.append((q, t))
        db.commit()
        return {
            "hunt_id": hunt.id,
            "found_radius_m": FOUND_RADIUS_M,
            "quests": [{"id": q.id, "riddle": q.riddle, "kind": q.kind, "lat": q.lat, "lon": q.lon,
                        "distance_m": t["distance_m"], "direction": t["direction"]} for q, t in quests],
        }


@app.post("/api/quest/{quest_id}/found")
async def quest_found(quest_id: int, req: FoundRequest):
    with SessionLocal() as db:
        q = db.get(Quest, quest_id)
        if not q:
            raise HTTPException(404, "Quest not found")
        d = round(haversine(req.lat, req.lon, q.lat, q.lon))
        if d > FOUND_RADIUS_M:
            raise HTTPException(403, f"You're still {d} m away. Get within {FOUND_RADIUS_M} m!")
        q.found, q.found_at = True, datetime.utcnow()
        db.commit()
        return {"name": q.name, "kind": q.kind}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
