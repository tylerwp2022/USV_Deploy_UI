#!/usr/bin/env python3
"""
MCTF Manual-Control Bridge Server (boat-qualified)
==================================================

WHAT THIS IS
  A tiny local web server that sits between a control UI (a browser now, a TAK /
  CoT feed later) and the MOOSDB. The browser never touches MOOS directly; it
  speaks HTTP to this server, and this server is the single component that owns
  the MOOSDB write. Each boat's policy process (solution.py) is subscribed to its
  own boat-qualified command variables, so anything this server pokes drives the
  matching boat.

      browser  --HTTP-->  THIS SERVER  --pymoos.notify-->  MOOSDB  -->  policy
      (TAK later --CoT-->  THIS SERVER  ... same path)

WHY BOAT-QUALIFIED VARIABLES
  Blue and red boats run as separate policy processes, and inside each the agents
  are agent_0..agent_5 -- BOTH teams use that same range. So a variable keyed on
  agent id alone cannot distinguish blue's agent_0 from red's agent_0. We key on
  the globally-unique boat id (blue_one, red_two, ...) instead, so commands are
  team-agnostic and never collide. This matches the entry's MCTF_BOAT_ID scheme.

VARIABLE SCHEMA (must match solution_sai.py in MCTF_BOAT_ID mode)
  MCTF_POSTURE_<boat_id>  -> auto | attack | balance | defend
  MCTF_ROLE_<boat_id>     -> auto | attack | defend | chase | escort | patrol
  ("auto" hands that lever back to the policy's automatic logic.)

RUN
  pip install flask
  python mctf_control_server.py            # serves on http://127.0.0.1:5005
  # open that URL in a browser on the same machine.

CONTROL FILE (fallback when MOOS is down)
  Set MCTF_CONTROL_FILE to a stable path OUTSIDE the staged entry dir (which the
  harness wipes on each launch). The policy must read the SAME path. The file
  keys are the full boat-qualified variable names.
"""

import json
import os
import signal
import sys
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

# --- MOOS connection (optional; absent == file fallback) ---------------------
try:
    import pymoos
    HAS_PYMOOS = True
except ImportError:
    HAS_PYMOOS = False


# =========================================================================
# Config -- keep these in sync with the entry + wp_config.json
# =========================================================================
POSTURE_PREFIX = "MCTF_POSTURE_"     # full var = MCTF_POSTURE_blue_one
ROLE_PREFIX = "MCTF_ROLE_"           # full var = MCTF_ROLE_blue_one
ACTIVE_PREFIX = "MCTF_ACTIVE_"       # full var = MCTF_ACTIVE_blue_one (boat -> UI)
AUTO_POSTURE_PREFIX = "MCTF_AUTO_POSTURE_"  # team-level: MCTF_AUTO_POSTURE_blue

# The roster of boats this UI can command. These MUST match wp_config.json's
# boat_id strings and the MCTF_BOAT_ID the launcher exports per boat. Override
# with MCTF_BOAT_IDS="blue_one,blue_two,..." if your roster differs.
_DEFAULT_BOATS = ["blue_one", "blue_two", "blue_three",
                  "red_one", "red_two", "red_three"]
BOAT_IDS = [b.strip() for b in
            os.environ.get("MCTF_BOAT_IDS", ",".join(_DEFAULT_BOATS)).split(",")
            if b.strip()]

VALID_POSTURES = ["auto", "attack", "balance", "defend"]
VALID_ROLES = ["auto", "attack", "defend", "chase", "escort", "patrol"]

# --- Per-boat MOOSDB ports ---------------------------------------------------
# In this mission each boat runs its OWN surveyor MOOSDB on its own port, and
# those DBs do NOT share variables with each other or with shoreside (pShare is
# input-only here -- verified). So to read each boat's MCTF_ACTIVE_* and write
# its MCTF_ROLE_*/MCTF_POSTURE_*, the bridge must connect to that boat's own DB.
# We read the ports from wp_config.json (the single source of truth that the
# harness also uses), so the roster and ports always match the real mission and
# can't drift from a second hardcoded copy.
WP_CONFIG_PATH = os.environ.get(
    "MCTF_WP_CONFIG",
    # Default: ../wp_config.json relative to this server (control/ lives next to
    # app.py, which sits beside wp_config.json).
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wp_config.json"),
)


def load_boat_ports():
    """Return {boat_id: db_port} from wp_config.json. Empty dict on any failure
    (bridge then runs file-only, no MOOS). The config groups boats under
    teams -> [{boat_id, port, ...}], same shape app.py reads."""
    try:
        with open(WP_CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[mctf-control] could not read {WP_CONFIG_PATH}: {e}")
        return {}
    ports = {}
    for team in ("blue", "red"):
        for boat in cfg.get("teams", {}).get(team, []):
            bid = boat.get("boat_id")
            port = boat.get("port")
            if bid and port:
                try:
                    ports[bid] = int(port)
                except (TypeError, ValueError):
                    pass
    return ports


# JSON fallback file. IMPORTANT: must be the SAME path the policy reads
# (MCTF_CONTROL_FILE in the entry). Defaults next to this server, which is only
# correct if you run from that directory.
CONTROL_FILE = os.environ.get(
    "MCTF_CONTROL_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mctf_control.json"),
)


def posture_var(boat_id):
    return POSTURE_PREFIX + boat_id


def role_var(boat_id):
    return ROLE_PREFIX + boat_id


# =========================================================================
# The bridge: holds desired command state and pushes it to MOOS (or file)
# =========================================================================
class _BoatLink:
    """One MOOS connection to a single boat's surveyor DB.

    Holds the pymoos client for that boat, subscribes to that boat's
    MCTF_ACTIVE_<id>, and caches the latest status string. Writes (role/posture)
    for this boat go out on this client. All failures are swallowed: a boat that
    isn't running yet just has a dead link, and the UI shows it as 'no report'.
    """

    def __init__(self, boat_id, port):
        self.boat_id = boat_id
        self.port = port
        self.connected = False
        self.active = None        # latest "behavior:source" from this boat
        self.auto_posture = None  # latest team auto-posture this boat reported
        self._comms = None
        self._active_var = ACTIVE_PREFIX + boat_id
        # The team this boat belongs to, for the auto-posture var it publishes.
        self._team = "red" if boat_id.startswith("red") else "blue"
        self._auto_posture_var = AUTO_POSTURE_PREFIX + self._team
        if HAS_PYMOOS:
            self._connect()

    def _connect(self):
        try:
            self._comms = pymoos.comms()

            def _on_connect():
                self.connected = True
                # Subscribe to THIS boat's status report + its team auto-posture.
                self._comms.register(self._active_var, 0)
                self._comms.register(self._auto_posture_var, 0)
                return True

            def _on_mail():
                for msg in self._comms.fetch():
                    try:
                        if msg.key() == self._active_var:
                            self.active = msg.string().strip()
                        elif msg.key() == self._auto_posture_var:
                            self.auto_posture = msg.string().strip()
                    except Exception:
                        pass
                return True

            self._comms.set_on_connect_callback(_on_connect)
            self._comms.set_on_mail_callback(_on_mail)
            # Unique client name per boat so the DBs don't bounce duplicates.
            self._comms.run("localhost", self.port, f"uMCTFCtrl_{self.boat_id}")
        except Exception:
            self._comms = None
            self.connected = False

    def ensure_connected(self):
        """Watchdog hook: if this link is not connected, (re)start its client.

        WHY this exists: pymoos.comms().run() is asynchronous, and a boat may not
        be up when the bridge starts (launch order) or may restart mid-session.
        Rather than rely on a single startup attempt, the bridge's watchdog calls
        this periodically. If pymoos's own internal retry already reconnected,
        self.connected is True and we do nothing. If the client object is gone or
        still down, we rebuild it with a fresh run(), so a boat that comes up
        later still gets linked without restarting the bridge."""
        if self.connected:
            return
        # Tear down any half-dead client and try a fresh connection.
        try:
            if self._comms is not None:
                self._comms.close(True)
        except Exception:
            pass
        self._comms = None
        if HAS_PYMOOS:
            self._connect()

    def notify(self, var, value):
        """Write a var to THIS boat's DB. Returns True on success."""
        if not (self._comms and self.connected):
            return False
        try:
            self._comms.notify(var, value, pymoos.time())
            return True
        except Exception:
            return False


class ControlBridge:
    """
    Owns the commanded state and one MOOS connection per boat.

    State is keyed by full variable name (MCTF_POSTURE_blue_one, ...). We hold
    our own copy because the UI needs to show what IS commanded without reading
    back. Writes are routed to the owning boat's connection (the boat_id is
    parsed out of the variable name); status reads come from each boat's link.
    """

    def __init__(self):
        self._state = {}
        for b in BOAT_IDS:
            self._state[posture_var(b)] = "auto"
            self._state[role_var(b)] = "auto"

        self._lock = threading.Lock()

        # One link per boat, keyed by boat_id, using ports from wp_config.json.
        # Boats with no known port (missing from config) get no link and are
        # simply uncommandable over MOOS (file fallback still mirrors them).
        self._ports = load_boat_ports()
        self._links = {}
        for b in BOAT_IDS:
            port = self._ports.get(b)
            if port is not None:
                self._links[b] = _BoatLink(b, port)

        # Watchdog: periodically (re)connect any dead link. This removes the
        # launch-order dependency -- the bridge can start before the boats and
        # still link to each one as it comes up, and re-link a boat that restarts
        # mid-session. Daemon so it dies with the process.
        if HAS_PYMOOS and self._links:
            self._watchdog = threading.Thread(target=self._watchdog_loop,
                                               daemon=True)
            self._watchdog.start()

    def _watchdog_loop(self):
        while True:
            time.sleep(2.0)
            for link in self._links.values():
                try:
                    link.ensure_connected()
                except Exception:
                    pass

    # ----- routing helpers --------------------------------------------------

    @staticmethod
    def _boat_of_var(var):
        """Extract the boat_id from a full var name (MCTF_ROLE_blue_one ->
        blue_one). Returns None if it doesn't match a known prefix."""
        for pre in (POSTURE_PREFIX, ROLE_PREFIX):
            if var.startswith(pre):
                return var[len(pre):]
        return None

    def _notify_moos(self, var, value):
        """Route a write to the owning boat's connection."""
        boat = self._boat_of_var(var)
        link = self._links.get(boat) if boat else None
        if link is None:
            return False
        return link.notify(var, value)

    # ----- file fallback ----------------------------------------------------

    def _write_file(self):
        """Mirror the whole commanded state to the JSON file (atomic rename)."""
        try:
            tmp = CONTROL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, CONTROL_FILE)
            return True
        except Exception:
            return False

    # ----- public API -------------------------------------------------------

    def set_value(self, var, value):
        with self._lock:
            self._state[var] = value
            moos_ok = self._notify_moos(var, value)
            file_ok = self._write_file()
        return {"var": var, "value": value, "moos": moos_ok, "file": file_ok}

    def snapshot(self):
        with self._lock:
            # active: {boat_id: "behavior:source"} from each boat's link.
            active = {}
            any_connected = False
            # auto_posture: {team: "defend"/"balance"/"adaptive"} -- the stance
            # the autonomy runs team-wide. Any connected boat on a team reports
            # it (all agree), so take the first non-empty per team.
            auto_posture = {}
            for b in BOAT_IDS:
                link = self._links.get(b)
                if link is not None:
                    if link.connected:
                        any_connected = True
                    if link.active:
                        active[b] = link.active
                    if getattr(link, "auto_posture", None):
                        team = "red" if b.startswith("red") else "blue"
                        auto_posture.setdefault(team, link.auto_posture)
            return {
                "state": dict(self._state),
                "active": active,
                "auto_posture": auto_posture,
                "moos_connected": any_connected,
                "has_pymoos": HAS_PYMOOS,
                "control_file": CONTROL_FILE,
                # Per-boat link status, so the UI can show which boats are
                # actually reachable over MOOS vs file-only.
                "links": {b: {"port": self._ports.get(b),
                              "connected": bool(self._links.get(b)
                                                and self._links[b].connected)}
                          for b in BOAT_IDS},
            }

    def reset_all_auto(self):
        results = []
        for b in BOAT_IDS:
            results.append(self.set_value(posture_var(b), "auto"))
            results.append(self.set_value(role_var(b), "auto"))
        return results


bridge = ControlBridge()


# =========================================================================
# Flask app
# =========================================================================
app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)),
                               "index.html")


@app.route("/api/state")
def api_state():
    snap = bridge.snapshot()
    snap["boat_ids"] = BOAT_IDS
    snap["valid_postures"] = VALID_POSTURES
    snap["valid_roles"] = VALID_ROLES
    snap["posture_prefix"] = POSTURE_PREFIX
    snap["role_prefix"] = ROLE_PREFIX
    return jsonify(snap)


@app.route("/api/posture", methods=["POST"])
def api_posture():
    """Set one boat's posture. Body: {"boat_id": "blue_one", "value": "defend"}."""
    body = request.json or {}
    boat_id = body.get("boat_id", "").strip()
    value = body.get("value", "").strip().lower()
    if boat_id not in BOAT_IDS:
        return jsonify({"error": f"unknown boat '{boat_id}'", "valid": BOAT_IDS}), 400
    if value not in VALID_POSTURES:
        return jsonify({"error": f"invalid posture '{value}'",
                        "valid": VALID_POSTURES}), 400
    return jsonify(bridge.set_value(posture_var(boat_id), value))


@app.route("/api/role", methods=["POST"])
def api_role():
    """Set one boat's role. Body: {"boat_id": "blue_one", "value": "chase"}."""
    body = request.json or {}
    boat_id = body.get("boat_id", "").strip()
    value = body.get("value", "").strip().lower()
    if boat_id not in BOAT_IDS:
        return jsonify({"error": f"unknown boat '{boat_id}'", "valid": BOAT_IDS}), 400
    if value not in VALID_ROLES:
        return jsonify({"error": f"invalid role '{value}'", "valid": VALID_ROLES}), 400
    return jsonify(bridge.set_value(role_var(boat_id), value))


@app.route("/api/team_posture", methods=["POST"])
def api_team_posture():
    """Set posture for a WHOLE team at once. Body: {"team":"blue","value":"defend"}.

    Option A semantics: posture is the coarse, team-wide lever. This writes the
    posture var for every boat on that team in one request, so one click swings
    the whole team. Per-boat role (set via /api/role) still overrides an
    individual boat on top of this.
    """
    body = request.json or {}
    team = body.get("team", "").strip().lower()
    value = body.get("value", "").strip().lower()
    if team not in ("blue", "red"):
        return jsonify({"error": f"unknown team '{team}'",
                        "valid": ["blue", "red"]}), 400
    if value not in VALID_POSTURES:
        return jsonify({"error": f"invalid posture '{value}'",
                        "valid": VALID_POSTURES}), 400
    # Boats whose id starts with the team name (blue_one, ...). Matches the
    # entry's own team test (boat_id.startswith(team)).
    team_boats = [b for b in BOAT_IDS if b.startswith(team)]
    results = [bridge.set_value(posture_var(b), value) for b in team_boats]
    return jsonify({"team": team, "value": value, "results": results})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    return jsonify({"results": bridge.reset_all_auto()})


if __name__ == "__main__":
    # Exit cleanly on SIGTERM/SIGINT. WHY: app.py stops this bridge by sending
    # SIGTERM to its process group. Flask's dev server does not reliably die on
    # SIGTERM on its own, which is what let the bridge orphan and hold port 5005.
    # Handling the signal explicitly (exit immediately) makes the stop reliable,
    # so app.py's escalation rarely has to fall through to SIGKILL.
    def _shutdown(signum, frame):
        # os._exit skips Flask's own teardown but guarantees the process ends
        # right now, releasing port 5005. There's no unsaved state to flush --
        # commands are already written to MOOS/file at the moment they're set.
        os._exit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _shutdown)
        except Exception:
            pass

    print(f"[mctf-control] pymoos available: {HAS_PYMOOS}")
    print(f"[mctf-control] boats: {', '.join(BOAT_IDS)}")
    print(f"[mctf-control] wp_config: {WP_CONFIG_PATH}")
    # pymoos.run() is async -- connections handshake a moment after construction.
    # Wait briefly so the banner reflects reality instead of reading too early
    # (which made every boat look like "no link" even when it was up). This is
    # only cosmetic for the banner; the live UI polls continuously and the
    # watchdog keeps links current regardless.
    if HAS_PYMOOS:
        time.sleep(2.5)
    # Show the per-boat ports + whether each link connected, so a missing boat
    # DB (boat not running yet) is visible at startup rather than a silent gap.
    snap = bridge.snapshot()
    for b in BOAT_IDS:
        link = snap["links"].get(b, {})
        port = link.get("port")
        state = "connected" if link.get("connected") else "no link yet (watchdog will retry)"
        print(f"[mctf-control]   {b}: port {port} -- {state}")
    print(f"[mctf-control] control file: {CONTROL_FILE}")
    print(f"[mctf-control] open http://127.0.0.1:5005")
    app.run(host="127.0.0.1", port=5005, threaded=True)
