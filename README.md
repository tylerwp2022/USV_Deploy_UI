# USV_Deploy_UI

A Flask web console for launching a **MOOS-IvP MCTF** (Marine Capture-The-Flag)
mission — shoreside plus a roster of Unmanned Surface Vehicles (USVs), each
running a machine-learning / heuristic policy entry. The operator stages a
submitted policy `.zip` per team, then launches the whole field (or individual
boats) from the browser. Runs in simulation on a single machine, or against real
Raspberry-Pi-based USVs over SSH.

The console manages all launched processes itself: no per-boat terminals. Output
is captured to in-memory buffers (shown on demand in the page) and to per-process
log files, and everything can be torn down with one **Stop All**.

A companion **manual-control console** (the `control/` package, launched from the
main page) lets an operator override the tyler_thesis entry at runtime — set a
team-wide posture or a per-boat role — and watch each boat report back what it is
actually executing and *why* (commanded vs autonomous vs safety reflex). See
**Manual control** below.

---

## Quick start

```bash
./run.sh                 # timewarp 1 (real time), opens the browser, starts the app
./run.sh 4               # timewarp 4 (fast, for unattended runs)
./run.sh --no-browser    # don't auto-open a browser (e.g. over SSH)
./run.sh 4 --no-browser  # combinable, either order
```

`run.sh` handles the Python environment for you (see **Environment** below), sets
`MCTF_MISSION_PATH` / `MCTF_TIME_WARP` / `MCTF_CONTROL_FILE`, clears any manual
commands left over from a previous session, opens `http://127.0.0.1:5000`, and
starts the app. Then in the UI: stage a ZIP per team, click **Launch All**, and
(optionally) **Launch MCTF Control** for the manual-override console.

---

## Architecture

```
  Browser (templates/index.html)                Browser, separate window
        |  HTTP: launch/stop buttons            (control/index.html :5005)
        |  + /status polling (JSON)                   |  HTTP: posture/role
        v                                             v  commands + state poll
  app.py  -- Flask + process registry -----.   control/mctf_control_server.py
        |   /launch_all        shoreside,  |         |  one pymoos client PER
        |                      then boats  |         |  BOAT DB (9011..9017),
        |   /launch_boats      all boats   |         |  watchdog reconnect;
        |   /launch_shoreside  shoreside   |         |  writes MCTF_ROLE_/
        |   /launch_boat       one boat    |         |  POSTURE_<boat>, reads
        |   /launch_control    manual-     |         |  MCTF_ACTIVE_<boat> +
        |                      control     '-spawns->|  MCTF_AUTO_POSTURE_<team>
        |                      bridge                v
        |   /stop_all          kill tracked     each boat's own MOOSDB
        |                      + killmoos            ^
        |   /stop_proc/<name>  kill one              |  policy publishes status,
        |   /status            liveness+output       |  reads commands (its own
        |   /submit            stage entry zip       |  DB only; no cross-DB
        |                                            |  sharing -- see Manual
        |   Each launched process runs in its        |  control)
        |   OWN process group; a reader thread       |
        |   drains stdout/stderr to ring buffer      |
        |   + ./logs/<name>.log. On console exit,    |
        |   ALL tracked processes are stopped.       |
        v                                            |
  submission_runner.py  (one process per boat)       |
        |   unzip entry  ->  ./blue_entry/ or        |
        |                    ./red_entry/            |
        v                                            |
  pyquaticus_moos_launcher.py  (one per boat)        |
        |   1. launch MOOS surveyor community        |
        |   2. exports MCTF_BOAT_ID + MCTF_MOOS_PORT |
        |   3. open pyquaticus MOOS bridge           |
        |   4. control loop: obs -> policy -> action |
        v                                            |
  <team>_entry/solution.py  ---------- (tyler_thesis entry only) ----'
```

Shoreside is launched from the mission tree (`launch_shoreside.sh`), at the same
time warp as the boats; it is the hub the boats connect to.

## Controls

| Control | Effect |
|---------|--------|
| **Launch All** | Shoreside first, brief settle, then every boat in the roster, staggered. |
| **Launch Boats** | All boats from `wp_config.json` (assumes entries already staged). |
| **Launch Shoreside** | Shoreside community only. |
| **Launch MCTF Control** | Starts the manual-control bridge (tracked as `mctf_control`, like a boat) and opens its console at `http://127.0.0.1:5005` in a **separate browser window**. Idempotent — clicking again refuses/refocuses rather than double-launching. |
| Per-boat **Launch** / **Stop** | Start or stop a single boat. |
| **Stop All** | Kill every tracked process group (including `mctf_control`), then run `killmoos --force` to sweep any orphans. |
| **Show output** | Expand a collapsible live-output panel for that process (hidden by default). |
| **Copy output** | Copy a process's full captured output to the clipboard. |
| Status dots | Green = up, red = down. Boats report by process liveness; shoreside reports by whether its MOOSDB port (9000) is listening. |

**Stopping is verified, not assumed:** `_stop` sends SIGTERM to the process
group, waits up to ~2 s for actual exit, and escalates to SIGKILL if the process
ignored it (Flask dev servers sometimes do). The flash message says "stopped via
SIGKILL" when the escalation fired. Closing the console itself (Ctrl-C or `kill`)
also stops everything it launched — an atexit/signal handler tears down all
tracked processes, so nothing orphans holding a port.

## Manual control (`control/`)

A second, standalone Flask app (`control/mctf_control_server.py` + its
`control/index.html`) providing runtime **mixed-initiative override** of the
**tyler_thesis (SAI) entry only** — other competition entries ignore these
commands entirely (the page carries a banner saying so). Launched from the main
console's **Launch MCTF Control** button; serves at `http://127.0.0.1:5005`.

### What the operator can do

- **Team posture** (coarse, one click per team): `auto | attack | balance |
  defend` — sets all three of a team's boats at once.
- **Per-boat role override** (fine): `auto | attack | defend | chase | escort |
  patrol` — role beats posture for that boat. Role rows are hidden by default
  behind a per-team **"role overrides"** checkbox; a boat with an *active* role
  stays visible regardless, so a command can never be hidden and forgotten.
- **Hand all back to auto** — clears every command (both transports).

### What the boats report back (the return channel)

Every tick, each boat publishes what it is **actually executing** and **why**,
shown as a color-coded chip on its card:

```
MCTF_ACTIVE_<boat_id> = "<behavior>:<source>"     e.g.  defend:posture
```

| source | meaning |
|--------|---------|
| `role` | a per-boat role override is driving this behavior (green) |
| `posture` | a team-wide posture command is driving it, no role overrides it (cyan) |
| `auto` | the policy's own logic chose it (dim) |
| `reflex` | a safety action (tagged / carrying flag / wall dodge) took precedence over *everything*, including a command — the command didn't fail, the boat is surviving first (amber) |

This is the confirmation loop: command a team to defend and you should see three
`defend / posture` chips. See `auto` instead → the command never reached that
boat. See `untag / reflex` → it heard you but got tagged.

The autonomy's own **team-level stance** is also reported. The auto logic picks a
team posture from the score (`MCTF_AUTO_POSTURE_<team>`): winning big →
`defend`, winning by 1 → `balance`, tied/losing → `adaptive` (attack when clear,
match defenders to intruders — deliberately *not* labeled "attack" because it
isn't purely that). It appears as an `auto: <posture>` tag on each team's posture
bar, dimmed when a manual command overrides it.

### Command variable schema

Boat-qualified (both teams' policies see agent_0..5 internally, so agent-keyed
names would collide; the globally-unique boat_id disambiguates):

```
MCTF_POSTURE_<boat_id>   auto | attack | balance | defend     (UI -> boat)
MCTF_ROLE_<boat_id>      auto | attack | defend | chase       (UI -> boat)
                              | escort | patrol
MCTF_ACTIVE_<boat_id>    "<behavior>:<source>"                (boat -> UI)
MCTF_AUTO_POSTURE_<team> defend | balance | adaptive          (boat -> UI)
```

Precedence: **reflex > role > posture > auto.** `"auto"` (or unset) means "no
override" and hands that lever back to the policy.

### Topology: why the bridge opens six MOOS connections

In this mission **each boat runs its own surveyor MOOSDB** (ports from
`wp_config.json`: blue 9015–9017, red 9011–9013) and those DBs **do not share
variables** — each boat's `pShare` is input-only (no outbound routes), verified
by poking a var into a boat DB and confirming it never reaches shoreside. There
is no shared bus. Consequently:

- the **policy** reads/writes its command + status vars on **its own boat's DB**
  (port handed to it via `MCTF_MOOS_PORT`, see below), and
- the **bridge** opens **one pymoos client per boat** (ports read from
  `wp_config.json` directly, so the roster/ports can't drift from the mission),
  reading each boat's status and writing each boat's commands on its own
  connection. A **watchdog thread retries dead links every 2 s**, so launch
  order doesn't matter — start the bridge before or after the boats, and it
  links to each as it comes up (and re-links a boat that restarts).

Per-boat link dots (green/grey next to each boat name) and a `MOOSDB n/6 boats`
indicator show which boats are actually reachable. A boat that isn't linked, or
is running a non-tyler_thesis entry, shows *no report*.

### Transports and the control file

Commands travel over live MOOS when connected, with a JSON file fallback
(`MCTF_CONTROL_FILE`, default `mctf_control.json` in this directory — outside
the entry dirs, which are wiped on every stage). The file **persists commands
across re-staging within a session**, and `run.sh` **deletes it at console
startup** so each session begins with every boat on auto — a stale "attack"
from last week can't silently carry into today's run (this bit us once: a boat
reporting `attack / role` while the operator believed it was in auto).

### The two harness lines (deliberate exception)

The infrastructure-conforms-to-entries rule has exactly one exception: two
exports in `pyquaticus_moos_launcher.py`, set after the entry import and before
`solution()` is constructed:

```python
os.environ["MCTF_BOAT_ID"]   = args.boat_id          # e.g. "blue_one"
os.environ["MCTF_MOOS_PORT"] = str(args.boat_port)   # this boat's own DB port
```

These hand the entry two environment facts it cannot learn otherwise (the policy
sees only agent_0..5, and each boat's DB port is launcher-side knowledge). They
inject no game data and are ignored by entries that don't read them. The entry
reads them **lazily at `solution()` construction, not at module import** —
the launcher imports the module (line ~117) *before* setting these (line ~189),
so an import-time read freezes them as unset (a bug we hit: the policy silently
never published because `MCTF_BOAT_ID` was `None` at import).

### Diagnostics

The policy logs its MOOS lifecycle (identity resolution, connect attempt,
handshake, first publish, or exactly why a publish was skipped) to
`MCTF_DIAG_LOG` (default: `mctf_policy_diag.log` next to the control file).
Connection failures are swallowed by design — a bad DB must never crash a
competition entry — so this log is the *only* visibility into that path. First
place to look when a chip shows *no report*:

```bash
cat mctf_policy_diag.log
# want:  "MOOS on_connect fired -> CONNECTED on port 9015"
#        "publish_status OK: wrote MCTF_ACTIVE_blue_one=..."
```

Also useful: scope a boat's own DB directly (note `--serverhost/--serverport`
for uXMS vs `--host/--port` for uPokeDB):

```bash
uXMS --serverhost=localhost --serverport=9015 -p \
    MCTF_ACTIVE_blue_one MCTF_ROLE_blue_one MCTF_POSTURE_blue_one
```

## Runtime modes

Selected by `"shore_ip"` in `wp_config.json`:

| `shore_ip`   | Mode       | Behavior                                                        |
|--------------|------------|----------------------------------------------------------------|
| `localhost`  | Simulation | Everything local. Entry zip copied into `./<team>_entry/`. Timewarp from `MCTF_TIME_WARP` (run.sh arg; default 1). |
| an IP        | Hardware   | Commands SSH'd to each boat; entries rsync'd out. Timewarp 1 always. |

## Files

| File | Role |
|------|------|
| `run.sh` | One-command launcher: prepares the Python env, sets mission path / timewarp / control-file paths, clears stale manual commands, opens the browser, starts the app. Takes the timewarp as an optional argument. |
| `app.py` | Flask UI + process registry: launch/stop routes (incl. `/launch_control`), verified stop with SIGKILL escalation, exit cleanup, output capture, status. |
| `submission_runner.py` | Unzips one entry for its team color, then starts the launcher. |
| `pyquaticus_moos_launcher.py` | Launches the MOOS surveyor, exports `MCTF_BOAT_ID`/`MCTF_MOOS_PORT`, opens the bridge, runs the control loop. |
| `wp_config.json` | Shore IP + per-team boat roster (id, name, ip, port). Also the port source for the manual-control bridge. |
| `templates/index.html` | The console page (buttons, per-boat rows, `mctf_control` row, output panels, status polling). |
| `control/mctf_control_server.py` | Manual-control bridge: Flask API + one pymoos client per boat DB with reconnect watchdog. |
| `control/index.html` | Manual-control console: team posture bars, per-boat role overrides (per-team toggle), active-behavior chips, auto-posture tags, link dots. |
| `logs/` | Per-process output logs (generated; gitignored). |
| `submissions/` | Pool of uploaded entry zips the operator stages from (zips gitignored). |
| `mctf_control.json` | Manual-command state (generated; cleared at console startup). |
| `mctf_policy_diag.log` | Policy MOOS-lifecycle diagnostics (generated). |

### Competition entry files

The contents of `<team>_entry/` are staged from submitted zips and are wiped and
re-extracted on every stage — **edit the zip source, never the staged copy.**
Competitor-submitted entries are never modified by this project; the
infrastructure conforms to them (with the single deliberate exception of the two
env exports documented under **Manual control**).

The **tyler_thesis (SAI) entry is ours**, and it is where the entire manual-
control contract lives: reading `MCTF_POSTURE_/ROLE_<boat_id>`, publishing
`MCTF_ACTIVE_<boat_id>` and `MCTF_AUTO_POSTURE_<team>`, the transport fallbacks,
and the diagnostics. Inside any staged zip, the policy must be named
`solution.py` (the launcher does `from <team>_entry.solution import solution`).

| File | Role |
|------|------|
| `<team>_entry/solution.py` | Standard entry adapter exposing `solution.compute_action(...)`. In the tyler_thesis entry this also implements the manual-control plane. |
| `<team>_entry/heuristic_policy.py` | (heuristic entry) The actual policy (`Agent_0`) — roles, behaviors, navigation. |
| `<team>_entry/gen_config.py` | Field geometry, Aquaticus field points, and the discrete `ACTION_MAP`. |

## Process model (why no terminals)

Each boat and shoreside is a backgrounded `subprocess.Popen` started in its own
process group (`start_new_session=True`) and tracked in a registry. A daemon
reader thread per process drains its merged stdout/stderr into:

- an in-memory ring buffer (last ~500 lines) for the live page view, and
- a per-process file under `./logs/<name>.log` for durable debugging.

**Stopping** kills the whole process group (`os.killpg`), which tears down the
launcher *and* the MOOS community it spawned — a bare PID kill would orphan the
MOOS apps. The kill is verified: SIGTERM first, then SIGKILL after ~2 s if the
process ignored it. **Stop All** additionally runs `killmoos --force` to sweep
any MOOS processes not tracked by this session (e.g. from a crashed or external
run), and console exit (Ctrl-C / SIGTERM) stops every tracked process via an
atexit/signal handler.

**Launch stagger:** boats are launched a few seconds apart
(`BOAT_LAUNCH_STAGGER`). This avoids a race where two boats' bridges run
`get_field.sh` concurrently and collide writing the shared `field.txt` /
`flags.txt`, which would yield an empty zone and crash the bridge. If you ever
see that crash under load, increase the stagger.

## Environment

The web app (`app.py`) needs only Flask, but the **boats it launches need
`pyquaticus`** — and boat subprocesses inherit this app's interpreter (via
`sys.executable`), so the app must run in a Python where `pyquaticus` imports.

`run.sh` handles this: it first checks whether `pyquaticus` is importable in the
current `python3`; if so it uses that and skips conda; otherwise it sources conda
and activates the project env by absolute path, then re-verifies. If you launch
`app.py` by hand instead, activate that env first, or you'll get
`ModuleNotFoundError: No module named 'pyquaticus'` when a boat starts.

Optional environment variables (`run.sh` sets the `MCTF_*` ones for you):

```bash
export MCTF_MISSION_PATH=/home/<you>/moos-ivp-mctf/missions/<mission>
export MCTF_LOG_PATH=/path/to/surveyor/logs    # defaults to <mission>/logs
export MCTF_TIME_WARP=1                        # sim warp; run.sh sets from its arg
export MCTF_CONTROL_FILE=/stable/path/mctf_control.json  # manual-command file
export MCTF_DIAG_LOG=/stable/path/mctf_policy_diag.log   # policy MOOS diagnostics
```

**Time warp:** configurable. `run.sh` takes it as an argument (`./run.sh 4`),
defaulting to **1 (real time)** — 4x changes the game state faster than a human
can react against, which defeats the manual-control console; use 4 for fast
unattended runs. `app.py` reads `MCTF_TIME_WARP` and applies the *same* value to
both shoreside and the boats — they must match or `pHelmIvP` reports clock-skew
errors — so they cannot drift apart. Sim only; hardware boats always run 1x. The
value is read at console startup: changing it means restarting `run.sh`, not
just relaunching boats.

**The manual-control bridge needs Flask.** It is launched by `app.py` with
`sys.executable`, which is guaranteed Flask-capable (app.py *is* a Flask app),
so no separate env handling is needed for it.

## Agent-id namespaces (important)

Two naming schemes coexist and must stay aligned:

- **boat_id** — `blue_one`, `red_two`, … (MOOS / UI side)
- **agent_id** — `agent_0` … `agent_5` (policy side)

`pyquaticus_moos_launcher.py` holds `agent_id_mapping` (boat_id -> agent_id) and
remaps **everything** into agent_id space before calling the policy: the per-
agent obs dicts *and* the `(agent_id, field)` tuple keys inside `global_state`.
`solution.compute_action` indexes those dicts purely by agent_id. Change one
mapping and you must change all of them.

## Action space (continuous vs discrete)

The bundled heuristic policy returns **continuous** `[speed, heading]` actions
(see `heuristic_policy.bearing_to_action`). The bridge in the launcher is
therefore created with `action_space='continuous'` — confirmed working against
the live `WestPoint2026` bridge. `gen_config.ACTION_MAP` defines a 17-entry
**discrete** menu for policies that instead emit an integer index — if you swap
in such a policy, set `action_space='discrete'` to match. This setting must agree
with both what the policy emits and what the bridge supports.

## Security note

On hardware (`shore_ip` != localhost), `app.py` interpolates operator-supplied
values (IP, port) into SSH/rsync commands; `_safe_ip()` / `_safe_port()` validate
the free-form ones, and the SSH password is stored in plaintext as a LAN-only
convenience. **Do not expose this app on an untrusted or multi-user network
without hardening it first.**

## History of fixes

Earlier cleanup (entry/launcher correctness):

1. `submission_runner.py` — spawn the launcher with `sys.executable` (not bare
   `'python3'`), so the child inherits the right interpreter/conda env instead of
   re-resolving from `PATH` (where a pyenv shim could pick a pyquaticus-less one).
2. `pyquaticus_moos_launcher.py` — `action_space` corrected to `'continuous'`;
   mission/log paths made env-configurable; removed unreachable post-loop code.
3. `submission_runner.py` — removed a dead pre-unzip import; tightened blue/red
   routing from `"b" in color` to `color == 'blue'`.

No-terminal redesign (this version):

4. Replaced per-boat `gnome-terminal` spawning with a managed process registry +
   output capture (in-memory buffers + `./logs` files).
5. Added **Launch All / Launch Boats / Launch Shoreside / Stop All** plus
   per-boat controls; **Stop All** runs `killmoos --force` to sweep orphans.
6. Added live output panels (toggle + copy-to-clipboard) and `/status` polling.
7. Added `run.sh` (env handling + browser open + start) and shoreside launching
   from the console.
8. Fixed the parallel-launch field-file race via a launch stagger; shoreside
   liveness now reported by MOOSDB-port check (its launch script exits early).

Manual-control era (current):

9. Built the `control/` package: bridge + console for runtime posture/role
   override of the tyler_thesis entry, with a boat→UI return channel
   (`MCTF_ACTIVE_*` behavior:source chips) confirming command execution.
10. **Import-order bug:** the entry read `MCTF_BOAT_ID` at module import, which
    runs *before* the launcher sets it — identity froze as `None` and the policy
    silently never published. Fixed by lazy resolution at `solution()`
    construction. Symptom to remember: `uXMS` shows `n/a` and the policy
    handshakes as unsuffixed `pMCTFCtrl`.
11. **Topology discovery:** boat DBs don't share variables (pShare input-only,
    proven by probe), so the bridge was rebuilt from one shoreside connection to
    one connection per boat DB (ports from `wp_config.json`), with a 2-second
    reconnect watchdog eliminating launch-order sensitivity. The policy connects
    to its own boat's DB via `MCTF_MOOS_PORT` instead of a hardcoded 9000.
12. Added policy MOOS-lifecycle diagnostics (`mctf_policy_diag.log`) — the
    connect path swallows errors by design, which made every failure above
    invisible; the log is the antidote.
13. Shutdown hardening: `_stop` verifies death and escalates SIGTERM→SIGKILL
    (the Flask bridge can ignore SIGTERM and orphan holding port 5005); console
    exit tears down all tracked processes; the bridge handles SIGTERM itself.
14. Stale-command fix: `run.sh` clears `mctf_control.json` at startup after a
    leftover per-boat "attack" from a prior session masqueraded as an auto-mode
    bug. Sessions start clean; commands persist only within a session.
15. Split the report source `commanded` into `role` vs `posture` (distinct
    colors), and surfaced the autonomy's own team stance
    (`MCTF_AUTO_POSTURE_<team>`: defend/balance/adaptive from score_diff) as an
    `auto:` tag per team. Configurable timewarp (`./run.sh N`, default 1) since
    4x is too fast for human-in-the-loop command.

Competitor-submitted entry files were **not** modified; the tyler_thesis entry
(ours) carries the control contract, and the harness gained only the two env
exports documented under **Manual control**.

## Known gaps

- **Hardware rsync vs runner path.** Hardware mode rsyncs entries to `~/entries/`
  on each boat, but the per-boat launch passes `--entry_name=./<team>_entry/
  test.zip`. Reconcile before a real hardware run.
- **`static/style.css`** is optional — the page ships with inline styling so it
  renders without it.
- **Launch stagger is timing-based.** Robust in practice; the fully race-proof
  fix would be making the bridge read a pre-generated field file rather than
  regenerating per boat (a `pyquaticus` change).
- **The return channel is MOOS-only.** Commands fall back to the JSON file when
  MOOS is down, but `MCTF_ACTIVE_*` / `MCTF_AUTO_POSTURE_*` status does not — a
  file-only session shows commands taking effect with every chip on *no report*.
- **Manual control assumes the tyler_thesis entry.** Staging any other entry to
  a boat makes the levers inert for that boat (by design; the page banner says
  so), but nothing *detects* the mismatch beyond the missing status report.
- **Control-page toggles repaint on the poll.** The console rebuilds its DOM
  every second; toggle state survives (held in JS), but expect a brief repaint
  when clicking near a poll tick.
- **Honest-fallback labels can surprise.** A commanded `chase` with no valid
  target reports the fallback it actually runs (e.g. `defend / role`) rather
  than pretending to chase — correct, but worth knowing before assuming a bug.
- **Hardware mode + manual control is untested.** The bridge reads boat DB ports
  from `wp_config.json` and connects to `localhost`; on real boats the DBs are
  on the boats' IPs, so the bridge would need to use each boat's `ip` field too.
