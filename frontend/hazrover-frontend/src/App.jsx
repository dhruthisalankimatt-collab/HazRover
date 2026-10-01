import { useEffect, useMemo, useState } from "react";
import { BrowserProvider, Contract, Interface, JsonRpcProvider, formatEther, isAddress, keccak256, parseEther, toUtf8Bytes } from "ethers";
import { API, CHAIN, CONTRACT, ABI, EVENTS } from "./config.js";

const rpc = new JsonRpcProvider(CHAIN.rpc, CHAIN.id, { staticNetwork: true });
// Read through the public RPC; if the browser blocks it (CORS), fall back to the wallet's connection.
async function read(fn) {
  try { return await fn(rpc); }
  catch (e) {
    if (!window.ethereum) throw e;
    return await fn(new BrowserProvider(window.ethereum));
  }
}
const iface = new Interface(EVENTS);
// Used to read the evidence hash straight out of the slash transaction on the chain.
const slashIface = new Interface([
  "function slashEscrow(address facility, uint256 gasPpm, bytes32 evidenceHash)",
  "function slashForSabotage(address facility, bytes32 proofHash)",
]);

// Tiny gas chart. Dashed lines: red = contract slash threshold, orange = rover's own alert level.
function Spark({ values }) {
  if (values.length < 2) return <p className="empty">Waiting for readings…</p>;
  const W = 300, H = 80, max = Math.max(4095, ...values);
  const x = (i) => (i / (values.length - 1)) * W;
  const y = (v) => H - (v / max) * H;
  const pts = values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: 90 }}>
      <line x1="0" x2={W} y1={y(1800)} y2={y(1800)} stroke="#c5221f" strokeDasharray="4 3" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <line x1="0" x2={W} y1={y(3500)} y2={y(3500)} stroke="#e37400" strokeDasharray="4 3" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
const mapLink = (la, lo) => `https://www.google.com/maps?q=${la},${lo}`;

// The iframe is forced to a 2:1 box (see style below), so the bbox must be 2:1 in Web Mercator.
// One degree of latitude is 1/cos(lat) taller than one degree of longitude on screen,
// so dLat = dLon * cos(lat) / 2 keeps the marker exactly centred.
const MAP_ASPECT = 2;
function osmEmbed(la, lo) {
  const dLon = 0.005;
  const dLat = (dLon * Math.cos((la * Math.PI) / 180)) / MAP_ASPECT;
  const bbox = [lo - dLon, la - dLat, lo + dLon, la + dLat].map((n) => n.toFixed(6)).join("%2C");
  return `https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&layer=mapnik&marker=${la}%2C${lo}`;
}

const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "");
const eth = (v) => (v == null ? "–" : Number(formatEther(v)).toFixed(3));

// Strip ABI-encoded hex blobs / "execution reverted" noise so the banner stays readable.
function cleanNote(text) {
  if (!text) return text;
  let t = String(text);
  const hexIdx = t.search(/0x[0-9a-fA-F]{16,}/);
  if (hexIdx !== -1) t = t.slice(0, hexIdx);
  t = t
    .replace(/chain error:?/i, "")
    .replace(/execution reverted:?/i, "")
    .replace(/[('",\s:]+$/g, "")
    .replace(/^[('",\s:]+/g, "")
    .trim();
  if (!t) t = "Transaction reverted.";
  if (/no escrow/i.test(t)) t = "No escrow: this facility has 0 tMSTC deposited (or it was already slashed to 0). Deposit tMSTC first.";
  return t.length > 180 ? t.slice(0, 180) + "…" : t;
}

async function switchChain() {
  try {
    await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN.hex }] });
  } catch (e) {
    if (e.code !== 4902) throw e;
    await window.ethereum.request({
      method: "wallet_addEthereumChain",
      params: [{ chainId: CHAIN.hex, chainName: "MST Testnet", rpcUrls: [CHAIN.rpc], blockExplorerUrls: [CHAIN.scan],
        nativeCurrency: { name: "tMSTC", symbol: "tMSTC", decimals: 18 } }],
    });
  }
}

export default function App() {
  const [acct, setAcct] = useState("");
  const [fac, setFac] = useState("");
  const [data, setData] = useState(null);
  const [health, setHealth] = useState(null);
  const [inc, setInc] = useState([]);
  const [logs, setLogs] = useState([]);
  const [rovers, setRovers] = useState([]);
  const [tele, setTele] = useState([]);
  const [verified, setVerified] = useState({});
  const [amt, setAmt] = useState("2");
  const [rover, setRover] = useState("");
  const [key, setKey] = useState("");
  const [note, setNote] = useState("");
  const [noteKind, setNoteKind] = useState("info"); // "info" | "error"
  const [now, setNow] = useState(Date.now() / 1000);

  function say(text, kind = "info") {
    setNote(cleanNote(text));
    setNoteKind(kind);
  }

  async function connect() {
    if (!window.ethereum) return say("No wallet found. Open this page in the Chrome profile that has BridgeKey installed.", "error");
    try {
      const [a] = await window.ethereum.request({ method: "eth_requestAccounts" });
      await switchChain();
      setAcct(a);
      setFac((f) => f || a);
    } catch (e) { say(e.shortMessage || e.message, "error"); }
  }

  async function deposit() {
    try {
      const signer = await new BrowserProvider(window.ethereum).getSigner();
      const tx = await new Contract(CONTRACT, ABI, signer).depositEscrow({ value: parseEther(amt) });
      say("Deposit sent, waiting for confirmation…");
      await tx.wait();
      say("Escrow deposited.");
    } catch (e) { say(e.shortMessage || e.message, "error"); }
  }

  async function saveRover() {
    try {
      const signer = await new BrowserProvider(window.ethereum).getSigner();
      const tx = await new Contract(CONTRACT, ABI, signer).setRover(rover);
      say("Saving rover wallet…");
      await tx.wait();
      say("Rover wallet saved.");
    } catch (e) { say(e.shortMessage || e.message, "error"); }
  }

  // Slashing calls always revert with "no escrow" if the facility hasn't deposited
  // (or was drained to 0), so block those two buttons client-side.
  const hasEscrow = data?.registered && data?.escrow && data.escrow > 0n;

  async function simulate(kind) {
    if (!isAddress(fac)) return say("Enter a valid facility address first.", "error");
    if ((kind === "gas" || kind === "sabotage") && !hasEscrow) {
      return say(
        data?.registered
          ? "This facility's escrow is 0. Deposit tMSTC below before simulating a slash."
          : "This facility hasn't deposited escrow yet. Deposit tMSTC below first.",
        "error"
      );
    }
    // Simulator uses the rover's last real position if there is one, so it doesn't teleport the map.
    const cur = rovers.find((r) => r.facility.toLowerCase() === fac.toLowerCase());
    const body = {
      facility: fac, event: kind === "sabotage" ? "SABOTAGE" : kind === "heartbeat" ? "HEARTBEAT" : "HAZARD",
      gas_ppm: kind === "gas" ? 2000 : kind === "safe" ? 1200 : 0, tilt_deg: kind === "sabotage" ? 95 : 0,
      lat: cur?.lat || 12.9417, lon: cur?.lon || 77.5658,
    };
    try {
      const r = await fetch(API + "/ingest", { method: "POST", headers: { "Content-Type": "application/json", "X-API-Key": key }, body: JSON.stringify(body) });
      const j = await r.json();
      say(r.ok ? "Backend replied: " + j.status : "Backend error: " + (j.detail || r.status), r.ok ? "info" : "error");
    } catch { say("Backend unreachable. Is uvicorn running on " + API + "?", "error"); }
  }

  // Re-check an incident against the blockchain: hash the evidence text here in the browser and
  // compare it with the hash stored inside the slash transaction. No trust in the backend needed.
  async function verifyIncident(r) {
    const set = (v) => setVerified((o) => ({ ...o, [r.tx_hash]: v }));
    set("checking");
    try {
      const local = keccak256(toUtf8Bytes(r.preimage));
      const tx = await read((p) => p.getTransaction(r.tx_hash));
      if (!tx) return set("missing");
      if (tx.to?.toLowerCase() !== CONTRACT.toLowerCase()) return set("bad");
      const parsed = slashIface.parseTransaction({ data: tx.data });
      const onchain = String(parsed.args[parsed.args.length - 1]).toLowerCase();
      set(onchain === local.toLowerCase() ? "ok" : "bad");
    } catch { set("missing"); }
  }

  useEffect(() => {
    if (!isAddress(fac)) return;
    const run = async () => {
      try {
        setData(await read(async (p) => {
          const c = new Contract(CONTRACT, ABI, p);
          const [f, exp, t, g, sb, to, tr] = await Promise.all([
            c.facilities(fac), c.isHeartbeatExpired(fac), c.heartbeatTimeout(),
            c.gasPenalty(), c.sabotagePenalty(), c.timeoutPenalty(), c.treasury(),
          ]);
          const tb = await p.getBalance(tr);
          return { escrow: f.escrow, last: Number(f.lastHeartbeatTimestamp), registered: f.registered, slashed: f.totalSlashed, exp, t: Number(t), g, sb, to, tr, tb };
        }));
      } catch (e) { say("Chain read failed: " + (e.shortMessage || e.message), "error"); }
    };
    run();
    const i = setInterval(run, 5000);
    return () => clearInterval(i);
  }, [fac]);

  useEffect(() => {
    const run = async () => {
      try {
        setHealth(await (await fetch(API + "/health")).json());
        setInc(await (await fetch(API + "/incidents?limit=30")).json());
      } catch { setHealth(null); }
      try {
        const r = await fetch(API + "/status");
        if (r.ok) setRovers(await r.json());
      } catch {}
      try {
        const r = await fetch(API + "/telemetry?limit=60");
        if (r.ok) setTele(await r.json());
      } catch {}
    };
    run();
    const i = setInterval(run, 3000);
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => { clearInterval(i); clearInterval(t); };
  }, []);

  useEffect(() => {
    const run = async () => {
      try {
        const raw = await read(async (p) => {
          const b = await p.getBlockNumber();
          return p.getLogs({ address: CONTRACT, fromBlock: Math.max(0, b - 2000), toBlock: b });
        });
        setLogs(raw.slice(-12).reverse().map((l) => {
          let p = null;
          try { p = iface.parseLog(l); } catch {}
          let detail = "";
          if (p?.name === "Slashed") detail = `${eth(p.args.amount)} tMSTC, ${p.args.reason}`;
          if (p?.name === "EscrowDeposited") detail = `${eth(p.args.amount)} tMSTC`;
          return { tx: l.transactionHash, block: l.blockNumber, name: p ? p.name : "Contract event", detail };
        }));
      } catch {}
    };
    run();
    const i = setInterval(run, 8000);
    return () => clearInterval(i);
  }, []);

  const me = rovers.find((r) => r.facility.toLowerCase() === fac.toLowerCase()) || rovers[0];
  const age = me ? Math.max(0, Math.floor(now - me.seen_at)) : null;
  const hasPos = me && (me.lat !== 0 || me.lon !== 0);
  const left = data && data.last ? Math.max(0, data.last + data.t - now) : null;

  // Readings of the selected rover, oldest first.
  const samples = useMemo(
    () => tele.filter((t) => me && t.facility.toLowerCase() === me.facility.toLowerCase() && t.rover_id === me.rover_id),
    [tele, me?.facility, me?.rover_id]
  );
  // Re-hash the whole chain in the browser. If any stored reading was edited or removed, this fails.
  const chain = useMemo(() => {
    let bad = -1;
    for (let i = 0; i < samples.length; i++) {
      const sm = samples[i];
      if (keccak256(toUtf8Bytes(sm.prev + sm.preimage)) !== sm.hash) { bad = i; break; }
      if (i > 0 && sm.prev !== samples[i - 1].hash) { bad = i; break; }
    }
    return { ok: bad < 0, bad, n: samples.length };
  }, [samples]);
  const lastS = samples[samples.length - 1];
  const signedCount = samples.filter((sm) => sm.signed).length;

  return (
    <main>
      <header>
        <div>
          <h1>HazRover Compliance Console</h1>
          <p>Escrow, incidents and heartbeat for the autonomous inspection rover on MST Testnet.</p>
        </div>
        <button className="solid" onClick={connect}>{acct ? short(acct) : "Connect BridgeKey"}</button>
      </header>

      <div className="status">
        <span className={health && !health.stub_mode ? "ok" : "warn"}>
          {health ? (health.stub_mode ? "Backend in stub mode" : "Backend live") : "Backend offline"}
        </span>
        <span>Contract <a href={`${CHAIN.scan}/address/${CONTRACT}`} target="_blank" rel="noreferrer">{short(CONTRACT)}</a></span>
        {health?.operator && <span>Operator {short(health.operator)}</span>}
        {health?.operator_balance && <span>Operator gas {eth(BigInt(health.operator_balance))} tMSTC</span>}
      </div>
      {note && (
        <div className={"note " + (noteKind === "error" ? "note-error" : "")} role="status" onClick={() => setNote("")}>
          {note}
        </div>
      )}

      <div className="grid">
        <section className="loc">
          <div>
            <h2>Rover location</h2>
            {me ? (
              <>
                <p className={"here " + (age < 30 ? "live" : "stale")}>{age < 30 ? "I AM HERE" : `Last seen ${age}s ago`}</p>
                <dl>
                  <dt>Rover</dt><dd>{me.rover_id}</dd>
                  <dt>Position</dt><dd>{hasPos ? `${me.lat.toFixed(5)}, ${me.lon.toFixed(5)}` : "No GPS fix yet"}</dd>
                  <dt>Last message</dt><dd>{me.event}, {age}s ago</dd>
                  <dt>Gas reading</dt><dd>{Math.round(me.gas_ppm)}</dd>
                </dl>
                {hasPos && <a href={mapLink(me.lat, me.lon)} target="_blank" rel="noreferrer">Open in Google Maps</a>}
              </>
            ) : (
              <p className="empty">No rover message yet. The first ping or heartbeat will place it on the map.</p>
            )}
          </div>
          {hasPos && (
            <iframe
              key={`${me.lat.toFixed(4)},${me.lon.toFixed(4)}`}
              title="Rover location map"
              src={osmEmbed(me.lat, me.lon)}
              loading="lazy"
              style={{ width: "100%", height: "auto", aspectRatio: `${MAP_ASPECT} / 1`, minHeight: 0, border: 0, borderRadius: 6 }}
            />
          )}
        </section>

        <section style={{ gridColumn: "1 / -1" }}>
          <h2>Live sensor data</h2>
          {lastS ? (
            <>
              <dl>
                <dt>Gas (MQ135 raw)</dt>
                <dd className="big">{Math.round(lastS.gas)} <small>/ 4095 · slash above 1800 · rover alert above 3500</small></dd>
                <dt>Obstacle distance</dt><dd>{lastS.dist < 0 ? "No echo (path clear)" : `${lastS.dist.toFixed(1)} cm`}</dd>
                <dt>Rover state</dt>
                <dd className={lastS.state !== "RUNNING" ? "bad" : ""}>
                  {lastS.state === "RUNNING" ? "Running" : lastS.state === "GAS_HALT" ? "HALTED: toxic gas" : "STOPPED: obstacle"}
                </dd>
                <dt>Authenticity</dt>
                <dd>{signedCount}/{samples.length} readings carry a valid device signature (HMAC-SHA256)</dd>
                <dt>Hash chain</dt>
                <dd style={{ color: chain.ok ? "#137333" : "#c5221f", fontWeight: 600 }}>
                  {chain.ok
                    ? `Intact ✓ ${chain.n} readings re-hashed in your browser, head ${short(lastS.hash)}`
                    : `BROKEN at reading #${chain.bad + 1}: stored data was modified`}
                </dd>
              </dl>
              <Spark values={samples.map((sm) => sm.gas)} />
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ textAlign: "left" }}>
                      <th>Time</th><th>Event</th><th>Gas</th><th>Distance</th><th>State</th><th>Signature</th><th>Reading hash</th>
                    </tr>
                  </thead>
                  <tbody>
                    {samples.slice(-8).reverse().map((sm, i) => (
                      <tr key={i}>
                        <td>{new Date(sm.seen_at * 1000).toLocaleTimeString()}</td>
                        <td>{sm.event}</td>
                        <td>{Math.round(sm.gas)}</td>
                        <td>{sm.dist < 0 ? "–" : sm.dist.toFixed(1) + " cm"}</td>
                        <td>{sm.state}</td>
                        <td>{sm.signed ? "✓ signed by rover" : "unsigned (simulator)"}</td>
                        <td title={sm.hash}>{short(sm.hash)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p className="empty">No readings yet. They appear here as soon as the rover pings.</p>
          )}
        </section>

        <section>
          <h2>Facility escrow</h2>
          <label>Facility wallet
            <input value={fac} onChange={(e) => setFac(e.target.value.trim())} placeholder="0x… factory owner" />
          </label>
          {data && (
            <dl>
              <dt>Escrow held</dt><dd className="big">{eth(data.escrow)} <small>tMSTC</small></dd>
              <dt>Total slashed</dt><dd>{eth(data.slashed)} tMSTC</dd>
              <dt>Registered</dt><dd>{data.registered ? "Yes" : "No. Deposit from this wallet to register."}</dd>
              <dt>Penalties</dt><dd>gas {eth(data.g)}, sabotage {eth(data.sb)}, timeout {eth(data.to)} tMSTC</dd>
              <dt>Treasury</dt><dd>{short(data.tr)} holds {eth(data.tb)} tMSTC</dd>
              <dt>Heartbeat</dt>
              <dd className={data.exp ? "bad" : ""}>
                {data.exp ? "Expired: anyone can call checkHeartbeat" : left == null ? "None received yet" : `Next due in ${Math.floor(left / 60)}m ${Math.floor(left % 60)}s`}
              </dd>
            </dl>
          )}
          {data && !hasEscrow && (
            <p className="warn-inline">
              {data.registered ? "Escrow is 0. Deposit below before testing a gas or sabotage slash." : "Not registered yet. Deposit below to register this facility."}
            </p>
          )}
          <div className="row">
            <input className="amt" value={amt} onChange={(e) => setAmt(e.target.value)} aria-label="Deposit amount in tMSTC" />
            <button className="solid" onClick={deposit} disabled={!acct}>Deposit escrow</button>
          </div>
          {data?.registered && acct.toLowerCase() === fac.toLowerCase() && (
            <div className="row" style={{ marginTop: 12 }}>
              <input value={rover} onChange={(e) => setRover(e.target.value.trim())} placeholder="Rover wallet (optional)" aria-label="Rover wallet" style={{ margin: 0 }} />
              <button onClick={saveRover} disabled={!isAddress(rover)}>Save rover</button>
            </div>
          )}
        </section>

        <section className="feed">
          <h2>Live incidents</h2>
          {inc.length === 0 && <p className="empty">No incidents yet. Send a hazard reading from the rover or the simulator below.</p>}
          {inc.map((r, i) => (
            <article key={i} className={r.tx_hash ? "slash" : "logged"}>
              <div className="top">
                <strong>{r.reason}</strong>
                <time>{new Date(r.ts * 1000).toLocaleTimeString()}</time>
              </div>
              <p>{r.ai_log || "Writing incident report…"}</p>
              {r.facts?.lat ? (
                <p className="where">Location:{" "}
                  <a href={mapLink(r.facts.lat, r.facts.lon)} target="_blank" rel="noreferrer">
                    {Number(r.facts.lat).toFixed(5)}, {Number(r.facts.lon).toFixed(5)}
                  </a>
                </p>
              ) : null}
              {r.explorer ? <a href={r.explorer} target="_blank" rel="noreferrer">View on mstscan</a> : <em>Logged, no penalty</em>}
              {r.preimage && r.tx_hash && (
                <div style={{ marginTop: 6 }}>
                  <small title={r.evidence_hash}>Evidence hash {short(r.evidence_hash)}</small>{" "}
                  <button onClick={() => verifyIncident(r)}>Verify against chain</button>{" "}
                  {verified[r.tx_hash] === "checking" && <small>checking…</small>}
                  {verified[r.tx_hash] === "ok" && <strong style={{ color: "#137333" }}>✓ Hash stored on-chain matches this data</strong>}
                  {verified[r.tx_hash] === "bad" && <strong style={{ color: "#c5221f" }}>✗ MISMATCH: data differs from what was recorded on-chain</strong>}
                  {verified[r.tx_hash] === "missing" && <small>Transaction not found on chain (stub mode?)</small>}
                </div>
              )}
            </article>
          ))}
        </section>

        <section>
          <h2>On-chain events</h2>
          {logs.length === 0 && <p className="empty">No contract events in the last 2,000 blocks.</p>}
          {logs.map((l, i) => (
            <div className="log" key={i}>
              <span>{l.name}{l.detail ? `: ${l.detail}` : ""}</span>
              <a href={`${CHAIN.scan}/tx/${l.tx}`} target="_blank" rel="noreferrer">block {l.block}</a>
            </div>
          ))}
        </section>

        <section>
          <h2>Demo simulator</h2>
          <p className="empty">Sends readings to the backend as if the rover had. The key stays in this tab only.</p>
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Device API key" aria-label="Device API key" />
          <div className="row wrap">
            <button onClick={() => simulate("heartbeat")}>Heartbeat</button>
            <button onClick={() => simulate("safe")}>Gas 1200 (no slash)</button>
            <button className="danger" onClick={() => simulate("gas")} disabled={!hasEscrow} title={!hasEscrow ? "Deposit escrow first" : ""}>Gas 2000</button>
            <button className="danger" onClick={() => simulate("sabotage")} disabled={!hasEscrow} title={!hasEscrow ? "Deposit escrow first" : ""}>Sabotage</button>
          </div>
        </section>
      </div>
    </main>
  );
}