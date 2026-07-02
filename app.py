# =============================================================================
# app.py  --  USV_Deploy_UI Flask console (no-terminal redesign)
# -----------------------------------------------------------------------------
# PURPOSE
#   Web console for launching a MOOS-IvP MCTF mission: shoreside + a roster of
#   USVs, each running an ML/heuristic policy via the pyquaticus bridge.
#
#   This version replaces the old "one gnome-terminal per boat" approach with a
#   managed process model:
#     - Each boat / shoreside is a backgrounded subprocess in its OWN process
#       group, tracked in a registry (PROCS).
#     - A reader thread drains each process's combined stdout/stderr into an
#       in-memory ring buffer (for live streaming to the page) AND appends it to
#       a per-process log file under ./logs (durable debugging).
#     - The browser polls /status to show which processes are up and to display
#       captured output in collapsible per-process panels.
#
#   Controls exposed:
#     /launch_boats     all boats from wp_config.json (assumes entries staged)
#     /launch_shoreside shoreside only (warp 4)
#     /launch_all       shoreside first, brief wait, then all boats
#     /launch_boat      one boat (per-boat button; kept from old UI)
#     /stop_all         kill every tracked process group
#     /stop_proc/<name> kill one tracked process group
#     /status           JSON: liveness + recent output for every process
#     /submit           stage a chosen entry zip for a team (unchanged)
#
# PROCESS GROUPS (important correctness detail)
#   Each boat's pyquaticus_moos_launcher.py itself spawns children
#   (launch_surveyor.sh -> pAntler -> the MOOS apps). If we killed only the
#   Python parent PID, those MOOS communities would orphan and hold their ports.
#   So every process is started with start_new_session=True (its own process
#   group) and stopped with os.killpg(), which tears down the whole tree. The
#   old gnome-terminal approach got this for free by closing the window; we have
#   to do it explicitly.
#
# SECURITY NOTE
#   On hardware (shore_ip != localhost) the launch commands SSH to the boats and
#   interpolate operator-supplied values; _safe_ip()/_safe_port() validate the
#   free-form ones. LAN-only convenience, not production-hardened. See README.
# =============================================================================

from flask import Flask, render_template, request, redirect, url_for, jsonify
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import socket
from collections import deque


# Shoreside MOOSDB port (the launch script uses 9000). We check whether this
# port is listening to decide if shoreside is actually up, because the launch
# script itself exits immediately under --auto (it backgrounds pAntler), so its
# PID is not a valid liveness signal the way a boat's blocking process is.
SHORESIDE_MOOS_PORT = 9000


def _port_listening(port, host='127.0.0.1'):
    """True if something is accepting connections on host:port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()

app = Flask(__name__)

# Paths resolved relative to this file so the app runs regardless of CWD.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'wp_config.json')
SUBMISSIONS_PATH = os.path.join(BASE_DIR, 'submissions')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# Mission path (for shoreside, which lives in the mission tree). Same env var
# the launcher uses; falls back to the thesis mission.
MISSION_PATH = os.environ.get(
    'MCTF_MISSION_PATH', '/home/tyler/moos-ivp-mctf/missions/tyler_thesis')

# Simulation time warp. Boats and shoreside MUST share this or pHelmIvP throws
# clock-skew errors (see README). Old per-boat code hardcoded 4; centralized
# here and made configurable via MCTF_TIME_WARP (set in run.sh) so you can drop
# to 1x for human-in-the-loop manual control -- 4x changes the game state faster
# than an operator can react to. Default stays 4 for fast unattended runs.
# Applies to SIM only; on real hardware boats run at 1x regardless (see
# boat_command). Both shoreside and boats read this one value, so they always
# agree on the warp.
try:
    SIM_TIMEWARP = float(os.environ.get('MCTF_TIME_WARP', '4'))
    # Use an int when it's whole (warp 4, not 4.0) -- the launch scripts and
    # MOOS expect a clean integer for typical warps; fractional is allowed too.
    if SIM_TIMEWARP == int(SIM_TIMEWARP):
        SIM_TIMEWARP = int(SIM_TIMEWARP)
except (TypeError, ValueError):
    SIM_TIMEWARP = 4

# SSH credentials for hardware mode (LAN-only convenience; Pi defaults).
USERNAME = 'pi'
PASSWORD = 'raspberry'

# How many recent output lines to keep in memory per process for live view.
BUFFER_LINES = 500


# Seconds to wait between boat launches. This must be long enough for each
# boat's bridge to finish running get_field.sh (which regenerates the SHARED
# field.txt / flags.txt in the mission dir) before the next boat starts and
# reads those files. Too short and two boats collide -- one reads field.txt
# while another is mid-rewrite, yielding an empty zone and an IndexError crash
# in the bridge (blue_zone[0]). ~3s comfortably covers get_field.sh's runtime.
BOAT_LAUNCH_STAGGER = 3.0

# Seconds to wait after launching shoreside before launching boats, so the
# shoreside MOOSDB is up for the boats to connect to.
SHORESIDE_SETTLE = 3.0


# -----------------------------------------------------------------------------
# Process registry
# -----------------------------------------------------------------------------
# PROCS maps a logical name ('shoreside', 'blue_one', ...) to a record:
#   {'popen': Popen, 'buffer': deque[str], 'logfile': path, 'thread': Thread,
#    'started': float, 'cmd': str}
# A lock guards structural changes (add/remove). The reader threads only append
# to their own deque, which is thread-safe for append/iteration in CPython.
PROCS = {}
PROCS_LOCK = threading.Lock()


def _reader_thread(name, proc, buffer, logfile_path):
    """Drain a process's combined stdout/stderr line-by-line into the in-memory
    ring buffer AND append to its log file. Runs until the pipe closes (process
    exit). One of these per launched process."""
    try:
        with open(logfile_path, 'a', buffering=1) as logf:
            header = f"\n===== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
            logf.write(header)
            buffer.append(header.strip())
            # proc.stdout is the merged stream (stderr redirected into it).
            for raw in iter(proc.stdout.readline, b''):
                line = raw.decode('utf-8', errors='replace').rstrip('\n')
                buffer.append(line)      # live view (capped deque)
                logf.write(line + '\n')  # durable record
    except Exception as e:
        buffer.append(f"[reader thread error: {e}]")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


def _spawn(name, cmd, cwd, env=None):
    """Start `cmd` (a list) as a backgrounded process in its own process group,
    register it, and attach a reader thread. If a process with this name is
    already alive, refuse (caller should stop it first). Returns (ok, message).

    `env` (optional): a full environment dict for the child. When None the child
    inherits this app's environment (the default and what boats use). Callers
    that need an extra var pass dict(os.environ, KEY=VAL)."""
    with PROCS_LOCK:
        existing = PROCS.get(name)
        if existing and existing['popen'].poll() is None:
            return False, f"{name} is already running"

        logfile_path = os.path.join(LOG_DIR, f"{name}.log")
        # Merge stderr into stdout so the reader sees one ordered stream.
        # start_new_session=True puts the child in its own process group so we
        # can kill the whole tree later with os.killpg.
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,                      # None -> inherit (Popen's default)
        )
        buffer = deque(maxlen=BUFFER_LINES)
        t = threading.Thread(
            target=_reader_thread,
            args=(name, proc, buffer, logfile_path),
            daemon=True,
        )
        t.start()
        PROCS[name] = {
            'popen': proc, 'buffer': buffer, 'logfile': logfile_path,
            'thread': t, 'started': time.time(), 'cmd': ' '.join(cmd),
        }
    return True, f"{name} launched (pid {proc.pid})"


def _stop(name):
    """Kill the process group for `name` (tears down the launcher AND the MOOS
    community it spawned). Returns (ok, message).

    Escalates: SIGTERM first (clean shutdown, which the MOOS apps honor), then
    -- if the process is still alive after a short grace period -- SIGKILL. WHY
    the escalation: not every child dies on SIGTERM. The Flask control bridge in
    particular can ignore SIGTERM (the dev server doesn't always wire it up), so
    a single SIGTERM would leave it running and holding port 5005, and the old
    code returned "stopped" without checking. The SIGKILL backstop guarantees
    the process is actually gone before we report success.

    NOTE on locking: we take PROCS_LOCK only to look up the record, then RELEASE
    it before the kill-and-wait. Holding it across the up-to-~3s wait would block
    /status polling and other launches/stops. The Popen object is safe to use
    after releasing the lock (we're not mutating PROCS here)."""
    with PROCS_LOCK:
        rec = PROCS.get(name)
    if not rec:
        return False, f"{name}: not found (never launched this session)"
    proc = rec['popen']
    if proc.poll() is not None:
        return True, f"{name}: already exited"
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True, f"{name}: already gone"

    # 1) Polite SIGTERM to the whole group.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"{name}: already gone"
    except Exception as e:
        return False, f"{name}: failed to stop ({e})"

    # 2) Wait up to ~2s for it to actually exit, polling the parent PID.
    died = False
    for _ in range(20):
        if proc.poll() is not None:
            died = True
            break
        time.sleep(0.1)

    # 3) Still alive? Escalate to SIGKILL (can't be ignored).
    if not died:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            died = True
        except Exception as e:
            return False, f"{name}: SIGTERM ignored, SIGKILL failed ({e})"
        for _ in range(10):
            if proc.poll() is not None:
                died = True
                break
            time.sleep(0.1)
        return (True, f"{name}: stopped via SIGKILL (was pid {pid})" if died
                else f"{name}: may still be running (pid {pid})")

    return True, f"{name}: stopped (was pid {pid})"


def _run_killmoos():
    """Sweep ALL MOOS mission processes via the user's killmoos script
    (/home/tyler/moos-ivp/bin/killmoos). This catches anything not tracked by
    this app -- orphans from crashed runs, externally-launched missions, etc.
    killmoos kills by the targ_/uMAC process patterns, so it's independent of
    which specific MOOS apps a mission runs.

    Uses --force (immediate SIGKILL): by the time Stop All is pressed, the goal
    is a clean slate now, not a graceful wind-down. Falls back to the bare
    command name if the absolute path isn't present (PATH lookup)."""
    killmoos_path = '/home/tyler/moos-ivp/bin/killmoos'
    cmd = [killmoos_path if os.path.exists(killmoos_path) else 'killmoos', '--force']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        # killmoos prints a summary line; surface its last non-empty line.
        out = (r.stdout or '').strip().splitlines()
        tail = out[-1] if out else f"exit {r.returncode}"
        return (r.returncode == 0), f"killmoos: {tail}"
    except FileNotFoundError:
        return False, "killmoos: not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "killmoos: timed out"
    except Exception as e:
        return False, f"killmoos: error ({e})"


# -----------------------------------------------------------------------------
# Input validation (free-form values that reach a shell on hardware)
# -----------------------------------------------------------------------------
_IP_RE = re.compile(r'^(localhost|(\d{1,3}\.){3}\d{1,3})$')


def _safe_ip(value):
    if not value or not _IP_RE.match(value):
        return False
    if value == 'localhost':
        return True
    return all(0 <= int(o) <= 255 for o in value.split('.'))


def _safe_port(value):
    try:
        return 1 <= int(value) <= 65535
    except (TypeError, ValueError):
        return False


# -----------------------------------------------------------------------------
# Config / submissions
# -----------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {"shore_ip": "localhost", "teams": {"blue": [], "red": []}}


def list_zip_files():
    try:
        return sorted(f for f in os.listdir(SUBMISSIONS_PATH) if f.endswith('.zip'))
    except Exception as e:
        print(f"Error listing submissions: {e}")
        return []


def all_boats(config):
    """Flatten the config roster into (team, boat_dict) pairs in launch order."""
    pairs = []
    for team in ('red', 'blue'):           # red first is conventional
        for boat in config['teams'].get(team, []):
            pairs.append((team, boat))
    return pairs


def vnames_string(config):
    """Build the colon-separated vehicle list shoreside expects, e.g.
    red_one:red_two:...:blue_one:..."""
    names = [b['boat_id'] for _, b in all_boats(config)]
    return ':'.join(names)


# -----------------------------------------------------------------------------
# Command builders
# -----------------------------------------------------------------------------
def boat_command(config, team, boat):
    """Build the submission_runner.py argv for one boat. Uses sys.executable so
    the child inherits this app's interpreter / conda env."""
    boat_id = boat['boat_id']
    boat_name = boat['boat_name']
    boat_ip = boat['ip']
    boat_port = boat['port']
    entry = f"./{team}_entry/test.zip"
    shore_ip = config['shore_ip']
    sim = shore_ip == 'localhost'

    cmd = [sys.executable, '-u', 'submission_runner.py',
           f'--entry_name={entry}', f'--color={team}',
           f'--boat_id={boat_id}', f'--boat_name={boat_name}',
           f'--timewarp={SIM_TIMEWARP if sim else 1}',
           f'--shore_ip={shore_ip}', f'--boat_ip={boat_ip}',
           f'--boat_port={boat_port}']
    if sim:
        cmd.insert(3, '--sim')
    return cmd


def shoreside_command(config):
    """Build the launch_shoreside.sh argv. Runs in the mission's shoreside dir,
    at the same warp as the boats, with the roster as --vnames."""
    return ['./launch_shoreside.sh', '--auto', str(SIM_TIMEWARP),
            f'--vnames={vnames_string(config)}']


# -----------------------------------------------------------------------------
# Page
# -----------------------------------------------------------------------------
@app.route('/', methods=['GET'])
def index():
    config = load_config()
    return render_template('index.html',
                           teams=config['teams'],
                           zip_files=list_zip_files())


# -----------------------------------------------------------------------------
# Entry staging (unchanged behavior, no terminal)
# -----------------------------------------------------------------------------
@app.route('/submit', methods=['POST'])
def submit():
    team = request.form.get('team')
    zip_file = request.form.get('zip_file')
    if team and zip_file:
        handle_submission(team, zip_file)
    return redirect(url_for('index'))


def handle_submission(team, zip_file):
    """Stage the chosen entry zip as ./<team>_entry/test.zip (sim) or rsync to
    the boats (hardware). Runs synchronously; no terminal."""
    config = load_config()
    if config['shore_ip'] == 'localhost':
        dest = os.path.join(BASE_DIR, f'{team}_entry', 'test.zip')
        subprocess.run(['rm', '-rf', f'./{team}_entry'], cwd=BASE_DIR)
        subprocess.run(['mkdir', '-p', f'./{team}_entry'], cwd=BASE_DIR)
        subprocess.run(['cp', f'./submissions/{zip_file}', dest], cwd=BASE_DIR)
    else:
        for bot in config['teams'][team]:
            ip = bot['ip']
            if not _safe_ip(ip):
                continue
            subprocess.run(
                ['sshpass', '-p', PASSWORD, 'rsync', '-av', '--progress',
                 f'./submissions/{zip_file}', f'{USERNAME}@{ip}:~/entries/'],
                cwd=BASE_DIR)


# -----------------------------------------------------------------------------
# Launch routes
# -----------------------------------------------------------------------------
@app.route('/launch_shoreside', methods=['POST'])
def launch_shoreside():
    config = load_config()
    shoreside_dir = os.path.join(MISSION_PATH, 'shoreside')
    ok, msg = _spawn('shoreside', shoreside_command(config), cwd=shoreside_dir)
    return jsonify({'ok': ok, 'message': msg})


@app.route('/launch_boats', methods=['POST'])
def launch_boats():
    config = load_config()
    results = []
    for team, boat in all_boats(config):
        if not (_safe_ip(boat['ip']) and _safe_port(boat['port'])):
            results.append({'name': boat['boat_id'], 'ok': False,
                            'message': 'invalid ip/port'})
            continue
        ok, msg = _spawn(boat['boat_id'], boat_command(config, team, boat),
                         cwd=BASE_DIR)
        results.append({'name': boat['boat_id'], 'ok': ok, 'message': msg})
        time.sleep(BOAT_LAUNCH_STAGGER)   # see constant: avoids field.txt write race
    return jsonify({'results': results})


@app.route('/launch_all', methods=['POST'])
def launch_all():
    """Shoreside first (it's the hub the boats connect to), brief wait, then all
    boats staggered."""
    config = load_config()
    results = []

    shoreside_dir = os.path.join(MISSION_PATH, 'shoreside')
    ok, msg = _spawn('shoreside', shoreside_command(config), cwd=shoreside_dir)
    results.append({'name': 'shoreside', 'ok': ok, 'message': msg})

    time.sleep(SHORESIDE_SETTLE)   # let shoreside MOOSDB come up before boats connect

    for team, boat in all_boats(config):
        if not (_safe_ip(boat['ip']) and _safe_port(boat['port'])):
            results.append({'name': boat['boat_id'], 'ok': False,
                            'message': 'invalid ip/port'})
            continue
        ok, msg = _spawn(boat['boat_id'], boat_command(config, team, boat),
                         cwd=BASE_DIR)
        results.append({'name': boat['boat_id'], 'ok': ok, 'message': msg})
        time.sleep(BOAT_LAUNCH_STAGGER)
    return jsonify({'results': results})


@app.route('/launch_boat', methods=['POST'])
def launch_boat():
    """Launch a single boat (per-boat button)."""
    config = load_config()
    boat_id = request.form.get('boat_id')
    # Find this boat + its team in the config.
    for team, boat in all_boats(config):
        if boat['boat_id'] == boat_id:
            if not (_safe_ip(boat['ip']) and _safe_port(boat['port'])):
                return jsonify({'ok': False, 'message': 'invalid ip/port'})
            ok, msg = _spawn(boat_id, boat_command(config, team, boat),
                             cwd=BASE_DIR)
            return jsonify({'ok': ok, 'message': msg})
    return jsonify({'ok': False, 'message': f'unknown boat {boat_id}'})


# Name under which the manual-control bridge is tracked in PROCS. Using a fixed
# logical name means Stop All tears it down with everything else, and /status
# reports its liveness dot, exactly like a boat.
CONTROL_PROC_NAME = 'mctf_control'
CONTROL_UI_PORT = 5005


@app.route('/launch_control', methods=['POST'])
def launch_control():
    """Launch the manual-control bridge (the MCTF override UI) as a tracked
    process. Idempotent: _spawn refuses if it's already running.

    WHY sys.executable: this app IS a Flask app, so the interpreter running it
    is guaranteed to have Flask -- which the bridge needs. (Boats use the same
    interpreter via boat_command, but they only need pyquaticus; the bridge
    needs Flask, and app.py's interpreter has it by definition.)

    The bridge inherits this app's environment, so MCTF_CONTROL_FILE (set by
    run.sh) flows through to it -- the bridge and the boats then share the same
    fallback control file. We also pass the roster from wp_config.json so the
    bridge's boat list always matches the actual mission, not its own default.
    """
    config = load_config()
    boat_ids = [b['boat_id'] for _, b in all_boats(config)]

    # The bridge lives in ./control/ next to this app.
    control_script = os.path.join(BASE_DIR, 'control', 'mctf_control_server.py')
    if not os.path.exists(control_script):
        return jsonify({'ok': False,
                        'message': f'control server not found at {control_script}'})

    # Pass the live roster so the UI matches wp_config.json exactly. The bridge
    # otherwise inherits this app's environment, so MCTF_CONTROL_FILE (set by
    # run.sh) flows through and the bridge + boats share the same fallback file.
    # We also pass the wp_config.json path so the bridge reads the SAME per-boat
    # MOOSDB ports the harness uses (each boat has its own DB; the bridge opens
    # one connection per boat to read status and write commands).
    child_env = dict(os.environ,
                     MCTF_BOAT_IDS=','.join(boat_ids),
                     MCTF_WP_CONFIG=CONFIG_PATH)

    cmd = [sys.executable, '-u', control_script]
    ok, msg = _spawn(CONTROL_PROC_NAME, cmd, cwd=BASE_DIR, env=child_env)
    if ok:
        msg += f" -- open http://127.0.0.1:{CONTROL_UI_PORT} in a new window"
    return jsonify({'ok': ok, 'message': msg, 'url': f'http://127.0.0.1:{CONTROL_UI_PORT}'})


# -----------------------------------------------------------------------------
# Stop routes
# -----------------------------------------------------------------------------
@app.route('/stop_all', methods=['POST'])
def stop_all():
    results = []
    with PROCS_LOCK:
        names = list(PROCS.keys())
    for name in names:
        ok, msg = _stop(name)
        results.append({'name': name, 'ok': ok, 'message': msg})
    # Sweep any remaining/orphaned MOOS processes (the killmoos equivalent).
    ok, msg = _run_killmoos()
    results.append({'name': 'killmoos', 'ok': ok, 'message': msg})
    return jsonify({'results': results})


@app.route('/stop_proc/<name>', methods=['POST'])
def stop_proc(name):
    ok, msg = _stop(name)
    return jsonify({'ok': ok, 'message': msg})


# -----------------------------------------------------------------------------
# Status / output streaming (polled by the page)
# -----------------------------------------------------------------------------
@app.route('/status', methods=['GET'])
def status():
    """Return liveness + recent output for every tracked process. The page polls
    this; the per-process 'output' is only rendered when its panel is expanded."""
    out = {}
    with PROCS_LOCK:
        items = list(PROCS.items())
    for name, rec in items:
        # Liveness: boats run a blocking process, so PID liveness is valid.
        # Shoreside's launch script exits immediately (it backgrounds pAntler),
        # so for shoreside we check whether the MOOSDB port is listening instead.
        if name == 'shoreside':
            alive = _port_listening(SHORESIDE_MOOS_PORT)
        else:
            alive = rec['popen'].poll() is None
        out[name] = {
            'alive': alive,
            'pid': rec['popen'].pid,
            'returncode': rec['popen'].returncode,
            'started': rec['started'],
            'output': list(rec['buffer']),
        }
    return jsonify(out)


def _cleanup_all_on_exit():
    """Stop every tracked process when the console itself shuts down.

    WHY: without this, killing the console (Ctrl-C, SIGTERM, or a crash) leaves
    every child in PROCS running -- boats AND the control bridge. The boats are
    loud enough to notice, but the bridge is silent and would orphan, holding
    port 5005 against the next run. This handler tears them all down so closing
    the console closes everything it launched. It's idempotent and safe to call
    more than once (already-dead processes are skipped by _stop)."""
    with PROCS_LOCK:
        names = list(PROCS.keys())
    for name in names:
        try:
            _stop(name)
        except Exception:
            pass


# Register for normal interpreter exit (covers Ctrl-C after Flask returns, and
# any clean shutdown). We also catch SIGTERM explicitly so `kill` of the console
# triggers the same cleanup rather than dropping children.
atexit.register(_cleanup_all_on_exit)


def _on_signal(signum, frame):
    # Clean up children, then exit. _cleanup_all_on_exit is idempotent, so it's
    # fine that atexit may also run it. We raise SystemExit (via sys.exit) rather
    # than os._exit so stdout/log buffers still flush on the way out.
    _cleanup_all_on_exit()
    sys.exit(0)


for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _on_signal)
    except Exception:
        # Signal handlers can only be set in the main thread; if app.py is ever
        # imported off-thread this is a no-op, which is fine.
        pass


if __name__ == '__main__':
    # threaded=True so /status polling and reader threads don't block launches.
    # debug=False because the reloader would spawn a second process registry.
    app.run(debug=False, threaded=True)
