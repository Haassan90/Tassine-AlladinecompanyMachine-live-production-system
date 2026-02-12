# =====================================================
# 🔒 main.py – Taco Group Live Production Dashboard (FINAL COMPLETE)
# Updates: ERPNext safe sync + Scheduler + Auto Meter + Alerts + Admin ERP Orders
# =====================================================

import os
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy.orm import Session
from pydantic import BaseModel
from dotenv import load_dotenv

# =====================================================
# Load environment variables
# =====================================================
load_dotenv()
ERP_URL = os.getenv("ERP_URL")
ERP_API_KEY = os.getenv("ERP_API_KEY")
ERP_API_SECRET = os.getenv("ERP_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

# =====================================================
# Import project modules
# =====================================================
from database import engine, SessionLocal, init_db
from models import Machine, ProductionLog, ERPNextMetadata
from erpnext_sync import (
    update_work_order_status, 
    get_work_orders, 
    auto_assign_work_orders, 
    get_admin_work_orders
)
from report import router as report_router
from scheduler import start_scheduler  # Scheduler with WebSocket manager

# =====================================================
# Logging
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =====================================================
# FastAPI App
# =====================================================
app = FastAPI(title="Taco Group Live Production")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(report_router)

# =====================================================
# Database setup
# =====================================================
init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =====================================================
# Frontend folder
# =====================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "Frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/")
async def get_dashboard():
    html_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Dashboard HTML not found!</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

# =====================================================
# WebSocket Manager
# =====================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, data: dict):
        for ws in list(self.active_connections):
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(ws)

manager = ConnectionManager()

@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)

# =====================================================
# Dashboard Helpers
# =====================================================
def get_dashboard_data(db: Session):
    response = []
    machines = db.query(Machine).all()
    metadata_map = {m.work_order: m for m in db.query(ERPNextMetadata).all()}
    locations = {}
    next_jobs = {}

    for m in machines:
        remaining_qty = (m.target_qty - m.produced_qty) if m.target_qty else 0
        remaining_time = remaining_qty * m.seconds_per_meter if m.seconds_per_meter else None
        progress_percent = (m.produced_qty / m.target_qty) * 100 if m.target_qty else 0
        erp_meta = metadata_map.get(m.work_order)

        if m.status in ["free", "stopped"] and m.work_order:
            if m.location not in next_jobs:
                next_jobs[m.location] = {
                    "machine_id": m.id,
                    "work_order": m.work_order,
                    "pipe_size": m.pipe_size,
                    "total_qty": m.target_qty,
                    "produced_qty": m.produced_qty,
                    "remaining_time": remaining_time
                }

        locations.setdefault(m.location, []).append({
            "id": m.id,
            "name": m.name,
            "status": m.status,
            "job": {
                "work_order": m.work_order,
                "size": m.pipe_size,
                "total_qty": m.target_qty,
                "completed_qty": m.produced_qty,
                "remaining_qty": remaining_qty,
                "remaining_time": remaining_time,
                "progress_percent": progress_percent,
                "erp_status": erp_meta.erp_status if erp_meta else None,
                "erp_comments": erp_meta.erp_comments if erp_meta else None
            } if m.work_order else None,
            "next_job": next_jobs.get(m.location)
        })

    for loc, machines_list in locations.items():
        response.append({"name": loc, "machines": machines_list})

    return response

# =====================================================
# API Endpoints
# =====================================================
@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return {"locations": get_dashboard_data(db)}

@app.get("/api/job_queue")
def job_queue():
    try:
        work_orders = get_work_orders()
    except Exception:
        work_orders = []
    queue = [({
        "id": wo.get("name"),
        "pipe_size": wo.get("custom_pipe_size"),
        "qty": wo.get("qty"),
        "produced_qty": wo.get("produced_qty", 0),
        "location": wo.get("custom_location"),
        "machine_id": wo.get("custom_machine_id")
    }) for wo in work_orders if wo.get("status") != "Completed"]
    return {"queue": queue}

# =====================================================
# Admin-Only ERP Orders Endpoint
# =====================================================
@app.get("/api/admin/work_orders")
def admin_work_orders():
    try:
        work_orders = get_admin_work_orders()
    except Exception:
        work_orders = []
    return {"work_orders": [{
        "id": wo.get("name"),
        "status": wo.get("status"),
        "pipe_size": wo.get("custom_pipe_size"),
        "qty": wo.get("qty"),
        "produced_qty": wo.get("produced_qty", 0),
        "location": wo.get("custom_location"),
        "machine_id": wo.get("custom_machine_id")
    } for wo in work_orders]}

# =====================================================
# Pydantic Models
# =====================================================
class MachineAction(BaseModel):
    location: str
    machine_id: int

class MachineRename(MachineAction):
    new_name: str

# =====================================================
# Machine Helpers
# =====================================================
def get_machine(db: Session, location: str, machine_id: int):
    return db.query(Machine).filter(Machine.id == machine_id, Machine.location == location).first()

async def update_machine_status(db: Session, m: Machine, new_status: str):
    m.status = new_status
    try:
        if new_status == "running":
            m.is_locked = True
            m.last_tick_time = datetime.now(timezone.utc)
            if ERP_URL and ERP_API_KEY and ERP_API_SECRET:
                update_work_order_status(m.erpnext_work_order_id, "In Process")
        elif new_status == "completed":
            m.is_locked = False
            if ERP_URL and ERP_API_KEY and ERP_API_SECRET:
                update_work_order_status(m.erpnext_work_order_id, "Completed")
    except Exception as e:
        logging.error(f"ERPNext status update failed: {e}")
    db.commit()

# =====================================================
# API – Machine Controls
# =====================================================
@app.post("/api/machine/start")
async def start_machine(data: MachineAction, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m or not m.work_order:
        return {"ok": False}
    await update_machine_status(db, m, "running")
    return {"ok": True}

@app.post("/api/machine/pause")
async def pause_machine(data: MachineAction, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m:
        return {"ok": False}
    m.status = "paused"
    db.commit()
    return {"ok": True}

@app.post("/api/machine/stop")
async def stop_machine(data: MachineAction, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m:
        return {"ok": False}
    await update_machine_status(db, m, "stopped")
    return {"ok": True}

@app.post("/api/machine/rename")
async def rename_machine(data: MachineRename, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m:
        return {"ok": False}
    m.name = data.new_name
    db.commit()
    return {"ok": True}

# =====================================================
# Automatic Meter Counter
# =====================================================
async def automatic_meter_counter():
    while True:
        db = SessionLocal()
        try:
            machines = db.query(Machine).filter(Machine.status == "running").all()
            now = datetime.now(timezone.utc)
            for m in machines:
                if not m.seconds_per_meter or not m.work_order:
                    continue
                if not m.last_tick_time:
                    m.last_tick_time = now
                    continue
                diff = (now - m.last_tick_time).total_seconds()
                ticks = int(diff // m.seconds_per_meter)
                if ticks > 0 and m.produced_qty < m.target_qty:
                    increment = min(ticks, m.target_qty - m.produced_qty)
                    m.produced_qty += increment
                    m.last_tick_time = now
                    db.add(ProductionLog(
                        machine_id=m.id,
                        work_order=m.work_order,
                        pipe_size=m.pipe_size,
                        produced_qty=increment,
                        timestamp=now
                    ))
                    meta = db.query(ERPNextMetadata).filter(ERPNextMetadata.work_order == m.work_order).first()
                    if meta:
                        meta.erp_status = "In Progress"
                        meta.last_synced = now
                    if m.produced_qty >= m.target_qty:
                        m.produced_qty = m.target_qty
                        await update_machine_status(db, m, "completed")
            db.commit()
        except Exception as e:
            logging.error(f"AUTO METER ERROR: {e}")
        finally:
            db.close()
        await asyncio.sleep(1)

# =====================================================
# Production Alerts
# =====================================================
alert_history = {}

async def production_alerts():
    while True:
        db = SessionLocal()
        try:
            machines = db.query(Machine).filter(Machine.target_qty > 0).all()
            for m in machines:
                if not m.work_order or m.status != "running":
                    continue
                percent = (m.produced_qty / m.target_qty) * 100
                last_level = alert_history.get(m.id, 0)
                alert_level = 0
                message = None
                if percent >= 100:
                    alert_level = 3
                    message = f"✅ Machine {m.name} COMPLETED"
                elif percent >= 90:
                    alert_level = 2
                    message = f"⚠ {m.name} CRITICAL {percent:.1f}%"
                elif percent >= 75:
                    alert_level = 1
                    message = f"⚠ {m.name} Warning {percent:.1f}%"
                if alert_level > 0 and alert_level != last_level:
                    alert_history[m.id] = alert_level
                    await manager.broadcast({"alert": message, "machine_id": m.id, "level": alert_level})
                elif percent < 75:
                    alert_history[m.id] = 0
        except Exception as e:
            logging.error(f"ALERT LOOP ERROR: {e}")
        finally:
            db.close()
        await asyncio.sleep(5)

# =====================================================
# ERPNext Sync Loop (Safe)
# =====================================================
async def erpnext_sync_loop(interval: int = 10):
    logging.info("🚀 ERPNext Sync Loop started")
    while True:
        try:
            # Safe call: ERP offline will not break loop
            await asyncio.to_thread(auto_assign_work_orders)
        except Exception as e:
            logging.error(f"ERP Sync Loop error: {e}")
        await asyncio.sleep(interval)

# =====================================================
# Broadcast Dashboard + ERP Queue (Safe)
# =====================================================
async def broadcast_dashboard_and_erpnext():
    while True:
        db = SessionLocal()
        try:
            locations = get_dashboard_data(db)
            try:
                work_orders = get_work_orders()
            except Exception:
                work_orders = []
            erp_queue = [{
                "id": wo.get("name"),
                "status": wo.get("status"),
                "pipe_size": wo.get("custom_pipe_size"),
                "qty": wo.get("qty"),
                "produced_qty": wo.get("produced_qty", 0),
                "location": wo.get("custom_location"),
                "machine_id": wo.get("custom_machine_id")
            } for wo in work_orders]

            await manager.broadcast({
                "locations": locations,
                "work_orders": erp_queue
            })
        except Exception as e:
            logging.error(f"BROADCAST ERROR: {e}")
        finally:
            db.close()
        await asyncio.sleep(5)

# =====================================================
# Startup Event
# =====================================================
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(automatic_meter_counter(), name="AutomaticMeterCounter")
    asyncio.create_task(production_alerts(), name="ProductionAlerts")
    asyncio.create_task(erpnext_sync_loop(), name="ERPNextSyncLoop")
    asyncio.create_task(broadcast_dashboard_and_erpnext(), name="BroadcastDashboard")
    
    # Start scheduler with WebSocket manager
    start_scheduler(manager)
