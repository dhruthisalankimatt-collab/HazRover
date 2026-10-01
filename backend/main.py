"""
HazRover orchestration backend (FastAPI)

ESP32-S3 --HTTP JSON--> this server --signed tx--> ComplianceVault on MST L1

Rules enforced here (and again inside the contract):
  1. Slashing is deterministic: gas value > 1800 (hard math). AI is used ONLY to
     write the human-readable incident log AFTER the slash is mined.
  2. Sabotage (IMU kick/flip) and heartbeat timeout are also deterministic.
  3. Offline-cached records are accepted through /flush, oldest first.
  4. PING events (no transaction) only update the rover's live position for the dashboard.

STUB MODE: if CONTRACT_ADDRESS is empty/invalid, the server still starts and
returns fake tx hashes, so you can test ESP32 -> backend before deploying.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import List, Literal, Optional

import httpx
from dotenv import load_dotenv
from eth_account import Account
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from web3 import Web3

load_dotenv()
log = logging.getLogger("hazrover")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
MST_RPC_URL = os.getenv("MST_RPC_URL") or "https://testnetrpc.mstblockchain.com"
CHAIN_ID = int(os.getenv("MST_CHAIN_ID") or "91562037")  # verify with /health
EXPLORER_BASE = (os.getenv("EXPLORER_BASE") or "https://testnet.mstscan.com").rstrip("/")
OPERATOR_PRIVATE_KEY = (os.getenv("OPERATOR_PRIVATE_KEY") or "").strip()
CONTRACT_ADDRESS = (os.getenv("CONTRACT_ADDRESS") or "").strip()
DEVICE_API_KEY = os.getenv("DEVICE_API_KEY") or "change-me"
# Shared secret the ESP32 uses to HMAC-sign every reading. Put the same value in the sketch.
DEVICE_HMAC_SECRET = os.getenv("DEVICE_HMAC_SECRET") or ""
# true = reject any reading that is not signed by the device (turn on for the real demo)
REQUIRE_SIGNATURE = (os.getenv("REQUIRE_SIGNATURE") or "false").lower() in ("1", "true", "yes")
WATCHED_FACILITIES = [a.strip() for a in (os.getenv("FACILITIES") or "").split(",") if Web3.is_address(a.strip())]

GAS_THRESHOLD = 1800  # calibrated gas index for the demo (not real PPM)
WATCHDOG_INTERVAL_S = int(os.getenv("WATCHDOG_INTERVAL_S") or "60")

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "none").lower()  # "ollama" | "openai" | "none"
OLLAMA_URL = os.getenv("OLLAMA_URL") or "http://localhost:11434/api/generate"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "llama3.2"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

# --------------------------------------------------------------------------
# Contract ABI (matches ComplianceVault.sol)
# --------------------------------------------------------------------------
def _fn(name, inputs, outputs=None, mutability="nonpayable"):
    return {
        "type": "function", "name": name, "stateMutability": mutability,
        "inputs": [{"name": n, "type": t} for n, t in inputs],
        "outputs": [{"name": n, "type": t} for n, t in (outputs or [])],
    }


ABI = [
    _fn("slashEscrow", [("facility", "address"), ("gasPpm", "uint256"), ("evidenceHash", "bytes32")]),
    _fn("slashForSabotage", [("facility", "address"), ("proofHash", "bytes32")]),
    _fn("heartbeat", [("facility", "address")]),
    _fn("checkHeartbeat", [("facility", "address")]),
    _fn("isHeartbeatExpired", [("facility", "address")], [("", "bool")], "view"),
    _fn("evidenceUsed", [("", "bytes32")], [("", "bool")], "view"),
    _fn("facilities", [("", "address")],
        [("escrow", "uint256"), ("lastHeartbeatTimestamp", "uint256"),
         ("rover", "address"), ("registered", "bool"), ("totalSlashed", "uint256")], "view"),
]

# --------------------------------------------------------------------------
# Global state
# --------------------------------------------------------------------------
w3: Optional[Web3] = None
contract = None
operator = None
tx_lock = asyncio.Lock()          # one tx at a time -> no nonce collisions
seen_evidence: set = set()        # local dedupe on top of the on-chain replay guard
incidents: List[dict] = []        # in-memory feed for the React dashboard
last_heartbeat_seen: dict = {}    # facility -> unix ts
telemetry: dict = {}              # "facility|rover_id" -> deque of readings (hash-chained)
chain_head: dict = {}             # "facility|rover_id" -> latest chain hash
GENESIS = "0x" + "00" * 32
rover_status: dict = {}           # "facility|rover_id" -> latest position / message (for the map)


# --------------------------------------------------------------------------
# Chain helpers
# --------------------------------------------------------------------------
def _send_tx(fn) -> str:
    """Build, sign and broadcast a contract call. Blocking -> run in a thread."""
    tx = fn.build_transaction({
        "from": operator.address,
        "nonce": w3.eth.get_transaction_count(operator.address, "pending"),
        "chainId": CHAIN_ID,
        "gasPrice": w3.eth.gas_price,
    })
    signed = operator.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction  # web3 v6/v7
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    if receipt.status != 1:
        raise RuntimeError(f"tx reverted: {Web3.to_hex(tx_hash)}")
    return Web3.to_hex(tx_hash)


async def send_tx(fn) -> str:
    async with tx_lock:
        return await asyncio.to_thread(_send_tx, fn)


def explorer_url(tx_hash: str) -> str:
    return f"{EXPLORER_BASE}/tx/{tx_hash}"


def clean_revert_reason(exc: Exception) -> str:
    """Return only the human sentence of a revert ("no escrow"), never the ABI hex."""
    msg = str(exc)
    m = re.search(r"execution reverted:?\s*([A-Za-z][A-Za-z ]*[A-Za-z])", msg)
    if m:
        return m.group(1).strip()
    if "revert" in msg.lower():
        return "transaction reverted"
    msg = re.sub(r"0x[0-9a-fA-F]{16,}", "", msg)   # never leak hex blobs
    return msg[:200]


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class RoverEvent(BaseModel):
    facility: str = Field(..., description="Factory owner wallet address (escrow owner)")
    rover_id: str = "hazrover-01"
    event: Literal["HAZARD", "SABOTAGE", "HEARTBEAT", "PING"] = "HAZARD"
    gas_ppm: float = 0.0   # calibrated gas index sent by the ESP32
    flame: bool = False
    tilt_deg: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    dist_cm: float = -1.0       # ultrasonic distance in cm, -1 = no echo
    state: str = "RUNNING"      # RUNNING | GAS_HALT | OBSTACLE_STOP
    timestamp: int = Field(default_factory=lambda: int(time.time()))


class FlushBatch(BaseModel):
    records: List[RoverEvent]


def preimage(p: RoverEvent) -> str:
    """Canonical text of the immutable facts of a reading. Its keccak is what goes on-chain."""
    return json.dumps(
        {"f": p.facility.lower(), "r": p.rover_id, "e": p.event, "t": p.timestamp,
         "g": round(p.gas_ppm, 2), "d": round(p.dist_cm, 1), "st": p.state,
         "la": round(p.lat, 6), "lo": round(p.lon, 6)},
        sort_keys=True, separators=(",", ":"),
    )


def evidence_hash(p: RoverEvent) -> bytes:
    return Web3.keccak(text=preimage(p))


SIG_RE = re.compile(r',"sig":"([0-9a-fA-F]{64})"\}\s*$')


def split_and_verify(raw: bytes):
    """
    The ESP32 appends ,"sig":"<hmac-sha256 hex>" to its JSON. The HMAC covers the JSON
    WITHOUT the sig field. Returns (json_without_sig, signed_ok). Wrong signature -> 401.
    """
    text = raw.decode("utf-8", errors="replace")
    m = SIG_RE.search(text)
    if not m:
        if REQUIRE_SIGNATURE and DEVICE_HMAC_SECRET:
            raise HTTPException(status_code=401, detail="signature required")
        return text, False
    body = text[:m.start()] + "}"
    if not DEVICE_HMAC_SECRET:
        return body, False
    expect = hmac.new(DEVICE_HMAC_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, m.group(1).lower()):
        raise HTTPException(status_code=401, detail="bad signature: payload was altered or wrong secret")
    return body, True


def record_telemetry(p: RoverEvent, signed: bool):
    """Append the reading to a hash chain: hash = keccak(prev_hash + preimage). Editing or
    deleting any past reading breaks every hash after it (the dashboard re-checks this)."""
    key = f"{p.facility.lower()}|{p.rover_id}"
    pre = preimage(p)
    prev = chain_head.get(key, GENESIS)
    h = Web3.to_hex(Web3.keccak(text=prev + pre))
    chain_head[key] = h
    telemetry.setdefault(key, deque(maxlen=300)).append({
        "facility": p.facility, "rover_id": p.rover_id, "event": p.event,
        "gas": p.gas_ppm, "dist": p.dist_cm, "state": p.state, "lat": p.lat, "lon": p.lon,
        "ts": p.timestamp, "seen_at": int(time.time()), "signed": signed,
        "prev": prev, "hash": h, "preimage": pre,
    })


def check_key(x_api_key: Optional[str]):
    if not x_api_key or not hmac.compare_digest(x_api_key, DEVICE_API_KEY):
        raise HTTPException(status_code=401, detail="bad api key")


def update_status(p: RoverEvent, live: bool = True):
    """
    Remember the newest position for the dashboard map.
    seen_at uses SERVER time so a wrong ESP32 clock can't make the rover look stale.
    Records replayed from the offline cache (live=False) never overwrite a newer live position.
    """
    key = f"{p.facility.lower()}|{p.rover_id}"
    prev = rover_status.get(key)
    if prev and not live:
        return
    has_fix = not (p.lat == 0.0 and p.lon == 0.0)   # (0,0) = no GPS fix, keep the last good position
    rover_status[key] = {
        "facility": p.facility,
        "rover_id": p.rover_id,
        "event": p.event,
        "gas_ppm": p.gas_ppm,
        "lat": p.lat if has_fix else (prev["lat"] if prev else 0.0),
        "lon": p.lon if has_fix else (prev["lon"] if prev else 0.0),
        "seen_at": int(time.time()),
    }


# --------------------------------------------------------------------------
# AI incident log (runs AFTER the slash; never influences it)
# --------------------------------------------------------------------------
async def generate_incident_log(rec: dict):
    prompt = (
        "Write a 2-sentence factual industrial incident report. No speculation.\n"
        f"Facts: {json.dumps(rec['facts'])}. Reason: {rec['reason']}. "
        f"Penalty tx: {rec['tx_hash']}."
    )
    text = ""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            if LLM_PROVIDER == "ollama":
                r = await http.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False})
                text = r.json().get("response", "").strip()
            elif LLM_PROVIDER == "openai" and OPENAI_API_KEY:
                r = await http.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    json={"model": OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}]},
                )
                text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM log failed: %s", exc)
    rec["ai_log"] = text or (
        f"{rec['reason']} recorded for facility {rec['facts'].get('facility')}. "
        f"Escrow slashed on-chain (tx {rec['tx_hash']})."
    )


# --------------------------------------------------------------------------
# Core processing
# --------------------------------------------------------------------------
def _record(reason: str, tx: Optional[str], facts: dict, note: str = "") -> dict:
    rec = {"reason": reason, "tx_hash": tx, "explorer": explorer_url(tx) if tx else None,
           "facts": facts, "ai_log": note, "ts": int(time.time())}
    incidents.insert(0, rec)
    del incidents[200:]
    return rec


def _attach_evidence(rec: dict, p: RoverEvent):
    rec["preimage"] = preimage(p)
    rec["evidence_hash"] = Web3.to_hex(evidence_hash(p))


async def stub_event(p: RoverEvent, bg: BackgroundTasks) -> dict:
    """Used when no contract is deployed yet: same rules, fake tx hashes."""
    fake_tx = "0x" + os.urandom(32).hex()
    if p.event == "PING":
        return {"status": "ping_recorded"}
    if p.event == "HEARTBEAT":
        last_heartbeat_seen[p.facility] = p.timestamp
        return {"status": "heartbeat_recorded (stub)", "tx_hash": fake_tx}

    key = evidence_hash(p).hex()
    if key in seen_evidence:
        return {"status": "duplicate_ignored"}

    if p.event == "HAZARD" and p.gas_ppm <= GAS_THRESHOLD:
        _record("HAZARD_NO_SLASH", None, p.model_dump(), "Below slash threshold; logged only.")
        return {"status": "logged_no_slash", "gas_ppm": p.gas_ppm}

    seen_evidence.add(key)
    reason = "SABOTAGE (IMU kick/flip)" if p.event == "SABOTAGE" else f"GAS {p.gas_ppm:.0f} > {GAS_THRESHOLD}"
    rec = _record(reason, fake_tx, p.model_dump())
    _attach_evidence(rec, p)
    bg.add_task(generate_incident_log, rec)
    return {"status": "slashed (STUB)", "reason": reason, "tx_hash": fake_tx, "explorer": explorer_url(fake_tx)}


async def process_event(p: RoverEvent, bg: BackgroundTasks, live: bool = True, signed: bool = False) -> dict:
    if not Web3.is_address(p.facility):
        raise HTTPException(status_code=422, detail="facility is not a valid address")

    # Position/location is recorded first, so the map stays live even if the chain call fails.
    update_status(p, live)
    record_telemetry(p, signed)

    if p.event == "PING":                      # location only, no transaction
        return {"status": "ping_recorded"}

    if contract is None:
        return await stub_event(p, bg)

    facility = Web3.to_checksum_address(p.facility)

    if p.event == "HEARTBEAT":
        last_heartbeat_seen[facility] = p.timestamp
        try:
            tx = await send_tx(contract.functions.heartbeat(facility))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=clean_revert_reason(exc))
        return {"status": "heartbeat_recorded", "tx_hash": tx, "explorer": explorer_url(tx)}

    ev = evidence_hash(p)
    key = ev.hex()
    if key in seen_evidence:
        return {"status": "duplicate_ignored"}

    if p.event == "SABOTAGE":
        fn = contract.functions.slashForSabotage(facility, ev)
        reason = "SABOTAGE (IMU kick/flip)"
    else:  # HAZARD
        if p.gas_ppm <= GAS_THRESHOLD:
            # Flame-only or below-threshold: log it, do NOT slash (deterministic rule).
            _record("HAZARD_NO_SLASH", None, p.model_dump(), "Below slash threshold; logged only.")
            return {"status": "logged_no_slash", "gas_ppm": p.gas_ppm}
        fn = contract.functions.slashEscrow(facility, int(round(p.gas_ppm)), ev)
        reason = f"GAS {p.gas_ppm:.0f} > {GAS_THRESHOLD}"

    # Cheap read-only check first: instant, readable error instead of a chain revert.
    try:
        current = await asyncio.to_thread(contract.functions.facilities(facility).call)
        escrow_wei, _, _, registered, _ = current
        if not registered:
            raise HTTPException(status_code=409, detail="facility has never deposited escrow (not registered)")
        if escrow_wei == 0:
            raise HTTPException(status_code=409, detail="facility escrow is 0 (already slashed or never funded). Deposit tMSTC first.")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("pre-check read failed, continuing to attempt tx: %s", exc)

    try:
        tx = await send_tx(fn)
    except Exception as exc:  # noqa: BLE001
        short = clean_revert_reason(exc)
        log.error("slash failed: %s", short)
        if short == "no escrow":
            short = "no escrow: this facility has 0 tMSTC deposited (or it was already slashed to 0). Deposit escrow first."
        raise HTTPException(status_code=502, detail=short)

    seen_evidence.add(key)
    rec = _record(reason, tx, p.model_dump())
    _attach_evidence(rec, p)
    bg.add_task(generate_incident_log, rec)  # AI only AFTER the slash
    return {"status": "slashed", "reason": reason, "tx_hash": tx, "explorer": explorer_url(tx)}


# --------------------------------------------------------------------------
# Deadman's-switch watchdog
# --------------------------------------------------------------------------
async def watchdog():
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        if contract is None:
            continue
        targets = set(WATCHED_FACILITIES) | set(last_heartbeat_seen)
        for addr in targets:
            try:
                fac = Web3.to_checksum_address(addr)
                expired = await asyncio.to_thread(contract.functions.isHeartbeatExpired(fac).call)
                if expired:
                    tx = await send_tx(contract.functions.checkHeartbeat(fac))
                    rec = _record("HEARTBEAT_TIMEOUT", tx, {"facility": fac})
                    await generate_incident_log(rec)
            except Exception as exc:  # noqa: BLE001
                log.warning("watchdog error for %s: %s", addr, clean_revert_reason(exc))


# --------------------------------------------------------------------------
# App lifecycle
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    global w3, contract, operator

    w3 = Web3(Web3.HTTPProvider(MST_RPC_URL, request_kwargs={"timeout": 30}))

    try:
        operator = Account.from_key(OPERATOR_PRIVATE_KEY)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "OPERATOR_PRIVATE_KEY in .env is missing or invalid. It must be 0x followed by "
            "64 hex characters (no quotes, no placeholder text)."
        ) from exc

    if Web3.is_address(CONTRACT_ADDRESS):
        contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=ABI)
    else:
        contract = None
        log.warning("STUB MODE: no valid CONTRACT_ADDRESS, chain calls are faked")

    try:
        log.info("connected=%s chain_id=%s operator=%s", w3.is_connected(), w3.eth.chain_id, operator.address)
    except Exception as exc:  # noqa: BLE001
        log.warning("RPC not reachable right now: %s", exc)

    task = asyncio.create_task(watchdog())
    yield
    task.cancel()


app = FastAPI(title="HazRover Backend", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
async def health():
    info = {"ok": True, "stub_mode": contract is None, "operator": operator.address,
            "contract": CONTRACT_ADDRESS or None, "configured_chain_id": CHAIN_ID,
            "rpc": MST_RPC_URL, "llm_provider": LLM_PROVIDER}
    try:
        info["chain_id"] = w3.eth.chain_id
        info["operator_balance"] = str(w3.eth.get_balance(operator.address))
    except Exception as exc:  # noqa: BLE001
        info["ok"] = False
        info["rpc_error"] = str(exc)
    return info


@app.post("/ingest")
async def ingest(request: Request, bg: BackgroundTasks, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    body, signed = split_and_verify(await request.body())
    try:
        p = RoverEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:300])
    return await process_event(p, bg, signed=signed)


@app.get("/telemetry")
async def get_telemetry(limit: int = 60, facility: Optional[str] = None):
    """Recent readings (oldest first per rover) with their hash-chain fields."""
    out = []
    for key, dq in telemetry.items():
        if facility and not key.startswith(facility.lower() + "|"):
            continue
        out.extend(list(dq)[-limit:])
    return out


@app.post("/flush")
async def flush(batch: FlushBatch, bg: BackgroundTasks, x_api_key: Optional[str] = Header(None)):
    """ESP32 offline cache: process oldest first; keep going if one record fails."""
    check_key(x_api_key)
    results = []
    for rec in sorted(batch.records, key=lambda r: r.timestamp):
        try:
            results.append(await process_event(rec, bg, live=False))
        except HTTPException as exc:
            results.append({"status": "error", "detail": exc.detail})
    return {"processed": len(results), "results": results}


@app.get("/status")
async def get_status():
    """Latest position/message of every rover. The dashboard map reads this."""
    return list(rover_status.values())


@app.get("/incidents")
async def get_incidents(limit: int = 50):
    return incidents[:limit]