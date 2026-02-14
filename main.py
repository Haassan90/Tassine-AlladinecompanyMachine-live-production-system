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
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("production_system.log"),
        logging.StreamHandler()
    ]
)

# =====================================================
# FastAPI App
# =====================================================
app = FastAPI(title="Taco Group Live Production")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
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
        dead_connections = []
        for ws in self.active_connections:
            try:
                await asyncio.wait_for(ws.send_json(data), timeout=2)
            except Exception:
                dead_connections.append(ws)

        for ws in dead_connections:
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
    machine_id: str

class MachineRename(MachineAction):
    new_name: str

# =====================================================
async def update_machine_status(db: Session, m: Machine, new_status: str):
    m.status = new_status

    try:
        if new_status == "running":
            m.is_locked = True
            m.last_tick_time = datetime.now(timezone.utc)

            # Safe ERP update
            if (
                ERP_URL 
                and ERP_API_KEY 
                and ERP_API_SECRET 
                and m.erpnext_work_order_id
            ):
                update_work_order_status(
                    m.erpnext_work_order_id, 
                    "In Process"
                )

        elif new_status == "completed":
            m.is_locked = False

            # Safe ERP update
            if (
                ERP_URL 
                and ERP_API_KEY 
                and ERP_API_SECRET 
                and m.erpnext_work_order_id
            ):
                update_work_order_status(
                    m.erpnext_work_order_id, 
                    "Completed"
                )

    except Exception as e:
        logging.error(f"ERPNext status update failed: {e}")

    db.commit()

# =====================================================
# =====================================================
# Helper – Get Machine by location and ID
# =====================================================
def get_machine(db: Session, location: str, machine_id: str) -> Machine | None:
    """Fetch a machine by location and ID."""
    return db.query(Machine).filter(
        Machine.id == machine_id,
        Machine.location == location
    ).first()

# =====================================================
# API – Machine Controls (Updated)
# =====================================================
@app.post("/api/machine/start")
async def start_machine(data: MachineAction, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m or not m.work_order:
        return {"ok": False, "error": "Machine not found or no active work order"}
    await update_machine_status(db, m, "running")
    return {"ok": True, "machine": {"id": m.id, "status": m.status}}

@app.post("/api/machine/pause")
async def pause_machine(data: MachineAction, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m:
        return {"ok": False, "error": "Machine not found"}
    m.status = "paused"
    db.commit()
    return {"ok": True, "machine": {"id": m.id, "status": m.status}}

@app.post("/api/machine/stop")
async def stop_machine(data: MachineAction, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m:
        return {"ok": False, "error": "Machine not found"}
    await update_machine_status(db, m, "stopped")
    return {"ok": True, "machine": {"id": m.id, "status": m.status}}

@app.post("/api/machine/rename")
async def rename_machine(data: MachineRename, db: Session = Depends(get_db)):
    m = get_machine(db, data.location, data.machine_id)
    if not m:
        return {"ok": False, "error": "Machine not found"}
    old_name = m.name
    m.name = data.new_name
    db.commit()
    return {
        "ok": True,
        "machine": {"id": m.id, "old_name": old_name, "new_name": m.name}
    }
# =====================================================
# Automatic Meter Counter
# =====================================================
from datetime import datetime, timezone

async def automatic_meter_counter():
    while True:
        await asyncio.sleep(0.1)
        db = SessionLocal()
        try:
            machines = db.query(Machine).filter(Machine.status == "running").all()
            now = datetime.now(timezone.utc)  # always UTC-aware

            for m in machines:
                if not m.seconds_per_meter or not m.work_order:
                    continue

                # Ensure last_tick_time is set and timezone-aware
                if m.last_tick_time:
                    last_tick = m.last_tick_time
                    if last_tick.tzinfo is None:
                        last_tick = last_tick.replace(tzinfo=timezone.utc)
                else:
                    m.last_tick_time = now
                    continue

                # Calculate elapsed ticks
                diff = (now - last_tick).total_seconds()
                ticks = int(diff // m.seconds_per_meter)

                if ticks > 0 and m.produced_qty < m.target_qty:
                    increment = min(ticks, m.target_qty - m.produced_qty)
                    m.produced_qty += increment
                    m.last_tick_time = now  # always UTC-aware

                    # Safe values for ProductionLog
                    location_value = m.location or "Unknown"
                    remaining_qty = m.target_qty - m.produced_qty
                    status_value = "running"

                    # Insert production log
                    db.add(ProductionLog(
                        machine_id=m.id,
                        location=location_value,
                        work_order=m.work_order,
                        pipe_size=m.pipe_size,
                        produced_qty=increment,
                        remaining_qty=remaining_qty,
                        status=status_value,
                        timestamp=now
                    ))

                    # Update ERPNext metadata if exists
                    meta = db.query(ERPNextMetadata).filter(
                        ERPNextMetadata.work_order == m.work_order
                    ).first()
                    if meta:
                        meta.erp_status = "In Progress"
                        meta.last_synced = now

                    # Mark machine as completed if target reached
                    if m.produced_qty >= m.target_qty:
                        m.produced_qty = m.target_qty
                        await update_machine_status(db, m, "completed")

            db.commit()
        except Exception as e:
            logging.error(f"AUTO METER ERROR: {e}")
            db.rollback()
        finally:
            db.close()

        await asyncio.sleep(1)


# =====================================================
# Production Alerts
# =====================================================
alert_history = {}

async def production_alerts():
    while True:
        await asyncio.sleep(0.1)
        db = SessionLocal()
        try:
            machines = db.query(Machine).filter(Machine.target_qty > 0).all()
            for m in machines:
                if not m.work_order or m.status != "running":
                    continue
                percent = (m.produced_qty / m.target_qty) * 100 if m.target_qty else 0
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
        await asyncio.sleep(0.1)

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
        await asyncio.sleep(0.1)

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
# =====================================================
# Production Logs API (FIXED)
# =====================================================
@app.get("/api/production_logs")
def production_logs(db: Session = Depends(get_db)):
    logs = db.query(ProductionLog).order_by(
        ProductionLog.timestamp.desc()
    ).limit(200).all()

    return [
        {
            "id": log.id,
            "machine_id": log.machine_id,
            "work_order": log.work_order,
            "pipe_size": log.pipe_size,
            "produced_qty": log.produced_qty,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None
        }
        for log in logs
    ]
