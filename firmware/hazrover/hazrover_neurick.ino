/*
  HazRover - Newrick rover firmware, merged with the HazRover backend

  Kept from the original sketch: Newrick motor control, MQ135 on GPIO10, ultrasonic
  avoidance on GPIO12/13, stop-forever below 10 cm, NTP time, LittleFS offline queue.

  Changed so it works with backend/main.py:
    - Sends plain HTTP to http://<laptop-ip>:8000/ingest with the X-API-Key header
    - JSON matches the backend: facility, rover_id, event (HAZARD/HEARTBEAT), gas_ppm, timestamp (unix)
    - Heartbeat every 5 minutes (not every loop: each one is a blockchain transaction)
    - Gas hazard needs 3 bad readings in a row, halts the motors, then waits 30 s between reports
    - All networking runs in its own task, so a slow server never freezes the motors

  Libraries: Newrick (your kit library). LittleFS, WiFi, HTTPClient come with the ESP32 core.
*/
#include <Newrick.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <time.h>

Newrick neurick;

// ================= EDIT THESE =================
const char* WIFI_SSID   = "YOUR_HOTSPOT_NAME";
const char* WIFI_PASSWORD = "YOUR_HOTSPOT_PASSWORD";
const char* SERVER_URL  = "http://192.168.1.10:8000";   // laptop IP from ipconfig, port 8000, plain http
const char* API_KEY     = "SAME_AS_DEVICE_API_KEY_IN_BACKEND_ENV";
const char* FACILITY    = "0xBd07b59b273FB3266d4D2626BC87E15a29B4BE6A";  // wallet that deposited escrow
const char* ROVER_ID    = "hazrover-01";

// ================= PINS (unchanged) =================
#define MQ135_PIN 10
#define TRIG_PIN  12
#define ECHO_PIN  13

// ================= RULES =================
#define GAS_ALERT_LEVEL        3000       // above the contract's 1800 limit, so a confirmed hazard always slashes
#define BAD_LOOPS_NEEDED       3          // consecutive bad readings before we act
#define OK_LOOPS_TO_RESUME     6          // clean readings before the motors run again
#define HAZARD_COOLDOWN_MS     30000UL    // gap between hazard reports (protects the escrow)
#define HEARTBEAT_MS           300000UL   // 5 minutes
#define DEMO_LATITUDE   12.941500
#define DEMO_LONGITUDE  77.565900

const long GMT_OFFSET_SEC = 19800;        // only affects local-time printing; the epoch sent is UTC
const int  DAYLIGHT_OFFSET_SEC = 0;
const char* QUEUE_FILE = "/event_queue.txt";
const char* QUEUE_TMP  = "/event_queue.tmp";

// ================= STATE =================
struct Msg { char json[360]; };
QueueHandle_t eventQ;

bool permanentlyStopped = false;
bool gasHalt = false;
int  badLoops = 0, okLoops = 0;
long badSum = 0;
unsigned long lastHazard = 0, lastBeat = 0;

// ================= MOTORS / SENSORS =================
void stopRover() { neurick.motor(1, 0); neurick.motor(2, 0); }

float readDistanceCM() {
  digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duration == 0) return -1;
  return duration * 0.0343 / 2.0;
}

// ================= EVENTS =================
String makeJson(const char* ev, int gas) {
  String j = "{\"facility\":\"";
  j += FACILITY;
  j += "\",\"rover_id\":\"";
  j += ROVER_ID;
  j += "\",\"event\":\"";
  j += ev;                                   // HAZARD | HEARTBEAT
  j += "\",\"gas_ppm\":";
  j += gas;
  j += ",\"flame\":false,\"tilt_deg\":0,\"lat\":";
  j += String(DEMO_LATITUDE, 6);
  j += ",\"lon\":";
  j += String(DEMO_LONGITUDE, 6);
  j += ",\"timestamp\":";
  j += (unsigned long)time(nullptr);         // unix seconds (UTC)
  j += "}";
  return j;
}

// Called from loop(): never blocks. The network task does the sending.
void enqueue(const String& json) {
  if (json.length() >= sizeof(Msg::json)) return;
  Msg m;
  strcpy(m.json, json.c_str());
  if (xQueueSend(eventQ, &m, 0) != pdTRUE) Serial.println("event queue full, dropped");
}

// ================= NETWORK TASK (runs on the other core) =================
// Returns the HTTP status, or -1 when there is no Wi-Fi / no reply.
int postEvent(const String& json) {
  if (WiFi.status() != WL_CONNECTED) return -1;
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(20000);                    // the backend waits for the blockchain receipt
  if (!http.begin(client, String(SERVER_URL) + "/ingest")) return -1;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  int code = http.POST(json);
  if (code > 0) Serial.printf("POST /ingest -> %d %s\n", code, http.getString().c_str());
  else Serial.printf("POST failed: %s\n", http.errorToString(code).c_str());
  http.end();
  return code;
}

void saveToQueue(const String& json) {
  File f = LittleFS.open(QUEUE_FILE, FILE_APPEND);
  if (!f) return;
  if (f.size() > 20000) { f.close(); Serial.println("offline queue full"); return; }
  f.println(json);
  f.close();
  Serial.println("stored in offline queue");
}

// Oldest first. Any HTTP reply means the backend got it (it ignores repeats), so only
// "no reply" keeps an event in the queue.
void drainQueue() {
  if (WiFi.status() != WL_CONNECTED || !LittleFS.exists(QUEUE_FILE)) return;
  File in = LittleFS.open(QUEUE_FILE, FILE_READ);
  if (!in) return;
  File out = LittleFS.open(QUEUE_TMP, FILE_WRITE);
  if (!out) { in.close(); return; }
  bool failed = false;
  while (in.available()) {
    String line = in.readStringUntil('\n');
    line.trim();
    if (!line.length()) continue;
    if (!failed && postEvent(line) > 0) continue;
    failed = true;
    out.println(line);
  }
  in.close(); out.close();
  LittleFS.remove(QUEUE_FILE);
  if (failed) LittleFS.rename(QUEUE_TMP, QUEUE_FILE); else LittleFS.remove(QUEUE_TMP);
}

void deliver(const String& json) {
  drainQueue();                                        // older events first
  int code = postEvent(json);
  if (code <= 0 && json.indexOf("\"HEARTBEAT\"") < 0)  // stale heartbeats are not worth keeping
    saveToQueue(json);
}

void syncTime() {
  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, "pool.ntp.org", "time.nist.gov");
  struct tm t;
  for (int i = 0; i < 30; i++) if (getLocalTime(&t, 500)) { Serial.println("time synchronized"); return; }
  Serial.println("NTP failed (will retry)");
}

void netTask(void*) {
  Msg m;
  unsigned long lastWifi = 0, lastDrain = 0, lastNtp = 0;
  for (;;) {
    if (xQueueReceive(eventQ, &m, pdMS_TO_TICKS(1000)) == pdTRUE) deliver(String(m.json));
    unsigned long now = millis();
    if (WiFi.status() != WL_CONNECTED) {
      if (now - lastWifi > 10000) { lastWifi = now; WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASSWORD); }
    } else {
      if (time(nullptr) < 1700000000 && now - lastNtp > 30000) { lastNtp = now; syncTime(); }
      if (now - lastDrain > 10000) { lastDrain = now; drainQueue(); }
    }
  }
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  delay(1000);

  neurick.begin();
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  stopRover();

  if (!LittleFS.begin(true)) Serial.println("LittleFS failed (offline queue disabled)");

  Serial.printf("Connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 30 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWi-Fi ok, IP %s\n", WiFi.localIP().toString().c_str());
    syncTime();
  } else {
    Serial.println("\nWi-Fi not connected: events will be queued and sent later");
  }

  eventQ = xQueueCreate(10, sizeof(Msg));
  xTaskCreatePinnedToCore(netTask, "net", 12288, NULL, 1, NULL, 0);   // loop() runs on the other core
  Serial.println("HAZROVER READY");
}

// ================= MAIN LOOP =================
void loop() {
  unsigned long now = millis();
  int gas = analogRead(MQ135_PIN);
  Serial.printf("MQ135 = %d\n", gas);

  // ---- heartbeat: first one at boot, then every 5 minutes (also while stopped) ----
  if (lastBeat == 0 || now - lastBeat >= HEARTBEAT_MS) {
    lastBeat = now;
    enqueue(makeJson("HEARTBEAT", gas));
  }

  // ---- stopped for good (obstacle < 10 cm) ----
  if (permanentlyStopped) {
    stopRover();
    delay(500);
    return;
  }

  // ---- gas hazard: 3 bad readings in a row ----
  if (gas > GAS_ALERT_LEVEL) { badLoops++; okLoops = 0; badSum += gas; }
  else {
    badLoops = 0; badSum = 0;
    if (gasHalt && ++okLoops >= OK_LOOPS_TO_RESUME) { gasHalt = false; Serial.println("gas clear, resuming"); }
  }
  if (badLoops >= BAD_LOOPS_NEEDED) {
    gasHalt = true;
    if (lastHazard == 0 || now - lastHazard > HAZARD_COOLDOWN_MS) {
      lastHazard = now;
      Serial.println("TOXIC GAS ALERT: motors halted, reporting");
      enqueue(makeJson("HAZARD", badSum / badLoops));
    }
  }
  if (gasHalt) { stopRover(); delay(200); return; }

  // ---- obstacle avoidance (unchanged) ----
  float distance = readDistanceCM();

  if (distance < 0) {
    Serial.println("NO ECHO -> GO FORWARD");
    neurick.motor(1, 350);
    neurick.motor(2, 350);
    delay(200);
    return;
  }

  Serial.printf("Distance = %.1f cm\n", distance);

  if (distance < 10.0) {
    Serial.println("OBSTACLE < 10 CM -> PERMANENT STOP");
    stopRover();
    permanentlyStopped = true;
    // The backend has no "stopped" event, so this is logged as a hazard with no gas (no penalty).
    enqueue(makeJson("HAZARD", 0));
    return;
  } else if (distance < 20.0) {
    neurick.motor(1, 300);
    neurick.motor(2, 100);
  } else if (distance < 25.0) {
    neurick.motor(1, 100);
    neurick.motor(2, 300);
  } else {
    neurick.motor(1, 350);
    neurick.motor(2, 350);
  }

  delay(200);
}
