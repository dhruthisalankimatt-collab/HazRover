"""
Run once from the backend folder:   python patch_backend.py
Adds to main.py: a PING event, a live rover status (location) and the "I AM HERE" terminal line.
Your original is saved as main.py.bak
"""
import shutil, sys

SRC = "main.py"
text = open(SRC, encoding="utf-8").read()
if "def note_seen" in text:
    print("main.py is already patched. Nothing to do.")
    sys.exit(0)

NOTE_SEEN = '''def note_seen(p: RoverEvent):
    """Remember the rover's latest position and print it in the terminal."""
    rover_status[p.facility.lower()] = {
        "facility": p.facility, "rover_id": p.rover_id, "event": p.event,
        "gas_ppm": p.gas_ppm, "lat": p.lat, "lon": p.lon,
        "device_ts": p.timestamp, "seen_at": int(time.time()),
    }
    where = f"{p.lat:.5f}, {p.lon:.5f}"
    stamp = time.strftime("%H:%M:%S")
    if p.event in ("PING", "HEARTBEAT"):
        print(f"[{stamp}] I AM HERE  |  {p.rover_id}  |  location {where}  |  gas {p.gas_ppm:.0f}  |  ({p.event})", flush=True)
    else:
        print(f"[{stamp}] !! {p.event}  |  {p.rover_id}  |  location {where}  |  gas {p.gas_ppm:.0f}", flush=True)


'''

edits = [
    ('event: Literal["HAZARD", "SABOTAGE", "HEARTBEAT"] = "HAZARD"',
     'event: Literal["HAZARD", "SABOTAGE", "HEARTBEAT", "PING"] = "HAZARD"'),
    ("last_heartbeat_seen: dict = {}",
     "rover_status: dict = {}           # facility -> latest message, shown on the dashboard\nlast_heartbeat_seen: dict = {}"),
    ("async def stub_event(p: RoverEvent, bg: BackgroundTasks) -> dict:",
     NOTE_SEEN + "async def stub_event(p: RoverEvent, bg: BackgroundTasks) -> dict:"),
    ('        raise HTTPException(status_code=422, detail="facility is not a valid address")',
     '        raise HTTPException(status_code=422, detail="facility is not a valid address")\n'
     '    note_seen(p)\n'
     '    if p.event == "PING":  # presence only: no blockchain transaction\n'
     '        return {"status": "i_am_here"}'),
]
for old, new in edits:
    if text.count(old) != 1:
        print("Could not patch automatically, this line was not found exactly once:\n  " + old)
        print("Your main.py was NOT changed. Send this message to Claude.")
        sys.exit(1)
    text = text.replace(old, new, 1)

text = text.rstrip() + '''


@app.get("/status")
async def get_status():
    return list(rover_status.values())
'''
shutil.copy(SRC, SRC + ".bak")
open(SRC, "w", encoding="utf-8").write(text)
print("Patched. Backup saved as main.py.bak. Restart uvicorn if it did not reload by itself.")