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

---

## Quick start

```bash
./run.sh              # activates the env if needed, opens the browser, starts the app
./run.sh --no-browser # same, but don't auto-open a browser (e.g. over SSH)
```

`run.sh` handles the Python environment for you (see **Environment** below), sets
`MCTF_MISSION_PATH`, opens `http://127.0.0.1:5000`, and starts the app. Then in
the UI: stage a ZIP per team, and click **Launch All**.

---

## Architecture

```
  Browser (templates/index.html)
        |  HTTP: launch/stop buttons + /status polling (JSON)
        v
  app.py  -- Flask + process registry ----------------------------------
        |   /launch_all        shoreside, then all boats (staggered)
        |   /launch_boats      all boats from wp_config.json
        |   /launch_shoreside  shoreside only (warp 4)
        |   /launch_boat       one boat (per-boat button)
        |   /stop_all          kill tracked groups + killmoos sweep
        |   /stop_proc/<name>  kill one
        |   /status            liveness + captured output (polled)
        |   /submit            stage an entry zip for a team
        |
        |   Each launched process runs in its OWN process group; a reader
        |   thread drains its stdout/stderr into an in-memory ring buffer
        |   (live view) and a per-process file under ./logs (durable).
        v
  submission_runner.py  (one process per boat)
        |   unzip entry  ->  ./blue_entry/ or ./red_entry/
        v
  pyquaticus_moos_launcher.py  (one process per boat)
        |   1. launch MOOS surveyor community (launch_surveyor.sh)
        |   2. open pyquaticus MOOS bridge
        |   3. control loop: obs -> policy -> action -> env.step
        v
  <team>_entry/solution.py  ->  heuristic_policy.Agent_0
```

Shoreside is launched from the mission tree (`launch_shoreside.sh`), at the same
time warp as the boats; it is the hub the boats connect to.

## Controls

| Control | Effect |
|---------|--------|
| **Launch All** | Shoreside first, brief settle, then every boat in the roster, staggered. |
| **Launch Boats** | All boats from `wp_config.json` (assumes entries already staged). |
| **Launch Shoreside** | Shoreside community only. |
| Per-boat **Launch** / **Stop** | Start or stop a single boat. |
| **Stop All** | Kill every tracked process group, then run `killmoos --force` to sweep any orphans. |
| **Show output** | Expand a collapsible live-output panel for that process (hidden by default). |
| **Copy output** | Copy a process's full captured output to the clipboard. |
| Status dots | Green = up, red = down. Boats report by process liveness; shoreside reports by whether its MOOSDB port (9000) is listening. |

## Runtime modes

Selected by `"shore_ip"` in `wp_config.json`:

| `shore_ip`   | Mode       | Behavior                                                        |
|--------------|------------|----------------------------------------------------------------|
| `localhost`  | Simulation | Everything local. Entry zip copied into `./<team>_entry/`. Timewarp 4. |
| an IP        | Hardware   | Commands SSH'd to each boat; entries rsync'd out. Timewarp 1.   |

## Files

| File | Role |
|------|------|
| `run.sh` | One-command launcher: prepares the Python env, sets mission path, opens the browser, starts the app. |
| `app.py` | Flask UI + process registry: launch/stop routes, output capture, status. |
| `submission_runner.py` | Unzips one entry for its team color, then starts the launcher. |
| `pyquaticus_moos_launcher.py` | Launches the MOOS surveyor, opens the bridge, runs the control loop. |
| `wp_config.json` | Shore IP + per-team boat roster (id, name, ip, port). |
| `templates/index.html` | The console page (buttons, per-boat rows, output panels, status polling). |
| `logs/` | Per-process output logs (generated; gitignored). |
| `submissions/` | Pool of uploaded entry zips the operator stages from (zips gitignored). |

### Competition entry files (do not edit)

The contents of `<team>_entry/` are **competitor-submitted** and are never
modified by this project — the infrastructure conforms to them, not the other
way around. They are gitignored (transient staging, not ours to commit). For
reference, each entry contains:

| File | Role |
|------|------|
| `<team>_entry/solution.py` | Standard entry adapter exposing `solution.compute_action(...)`. |
| `<team>_entry/heuristic_policy.py` | The actual policy (`Agent_0`) — roles, behaviors, navigation. |
| `<team>_entry/gen_config.py` | Field geometry, Aquaticus field points, and the discrete `ACTION_MAP`. |

## Process model (why no terminals)

Each boat and shoreside is a backgrounded `subprocess.Popen` started in its own
process group (`start_new_session=True`) and tracked in a registry. A daemon
reader thread per process drains its merged stdout/stderr into:

- an in-memory ring buffer (last ~500 lines) for the live page view, and
- a per-process file under `./logs/<name>.log` for durable debugging.

**Stopping** kills the whole process group (`os.killpg`), which tears down the
launcher *and* the MOOS community it spawned — a bare PID kill would orphan the
MOOS apps. **Stop All** additionally runs `killmoos --force` to sweep any MOOS
processes not tracked by this session (e.g. from a crashed or external run).

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

Optional environment variables (read by `pyquaticus_moos_launcher.py`; `run.sh`
sets `MCTF_MISSION_PATH` for you):

```bash
export MCTF_MISSION_PATH=/home/<you>/moos-ivp-mctf/missions/<mission>
export MCTF_LOG_PATH=/path/to/surveyor/logs    # defaults to <mission>/logs
```

**Time warp:** in sim, boats and shoreside both launch at warp 4. They must
match, or `pHelmIvP` reports large clock-skew errors. The console launches
shoreside at warp 4 to match the boats automatically.

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

The competition entry files were **not** modified — they are competitor
submissions and the infrastructure conforms to their contract.

## Known gaps

- **Hardware rsync vs runner path.** Hardware mode rsyncs entries to `~/entries/`
  on each boat, but the per-boat launch passes `--entry_name=./<team>_entry/
  test.zip`. Reconcile before a real hardware run.
- **`static/style.css`** is optional — the page ships with inline styling so it
  renders without it.
- **Launch stagger is timing-based.** Robust in practice; the fully race-proof
  fix would be making the bridge read a pre-generated field file rather than
  regenerating per boat (a `pyquaticus` change).
