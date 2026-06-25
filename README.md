# USV_Deploy_UI

A lightweight Flask web console for launching Unmanned Surface Vehicles (USVs)
into a **MOOS-IvP MCTF** (Marine Capture-The-Flag) mission, with a machine-
learning / heuristic policy entry per team. The operator selects a team and a
submitted policy `.zip`, then launches boats one at a time — in simulation on a
single machine, or out to real Raspberry-Pi-based USVs over SSH.

---

## Architecture

```
  Browser (templates/index.html)
        |  HTTP (forms + a little JSON)
        v
  app.py  -- Flask routes ----------------------------------------------
        |   /submit         stage a chosen zip for a team
        |   /agent_action   launch ONE boat (opens a gnome-terminal)
        |   /get_rsync_command   preview the command (dry run)
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

## Runtime modes

Selected by `"shore_ip"` in `wp_config.json`:

| `shore_ip`   | Mode       | Behavior                                                        |
|--------------|------------|----------------------------------------------------------------|
| `localhost`  | Simulation | Everything local. Entry zip copied into `./<team>_entry/`. Timewarp 4. |
| an IP        | Hardware   | Commands SSH'd to each boat; entries rsync'd out. Timewarp 1.   |

## Files

| File | Role |
|------|------|
| `app.py` | Flask UI; builds and runs the per-boat launch commands. |
| `submission_runner.py` | Unzips one entry for its team color, then starts the launcher. |
| `pyquaticus_moos_launcher.py` | Launches the MOOS surveyor, opens the bridge, runs the control loop. |
| `wp_config.json` | Shore IP + per-team boat roster (id, name, ip, port). |
| `submissions/` | Pool of uploaded entry zips the operator can choose from. |

### Competition entry files (do not edit)

The contents of `<team>_entry/` are **competitor-submitted** and are never
modified by this project — the infrastructure conforms to them, not the other
way around. For reference, each entry contains:

| File | Role |
|------|------|
| `<team>_entry/solution.py` | Standard entry adapter exposing `solution.compute_action(...)`. |
| `<team>_entry/heuristic_policy.py` | The actual policy (`Agent_0`) — roles, behaviors, navigation. |
| `<team>_entry/gen_config.py` | Field geometry, Aquaticus field points, and the discrete `ACTION_MAP`. |

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
the live `WestPoint2026` bridge (boats drive and `pRLMonitor` receives valid
speeds/headings/actions). `gen_config.ACTION_MAP` defines a 17-entry **discrete**
menu for policies that instead emit an integer index — if you swap in such a
policy, set `action_space='discrete'` to match. This setting must agree with
both what the policy emits and what the bridge supports.

## Configuration

`pyquaticus_moos_launcher.py` reads two optional environment variables:

```bash
export MCTF_MISSION_PATH=/home/<you>/moos-ivp-mctf/missions/<mission>
export MCTF_LOG_PATH=/path/to/surveyor/logs    # defaults to <mission>/logs
```

If unset, `MCTF_MISSION_PATH` defaults to the thesis mission and the log path is
derived from it (so the two can never disagree, which was a prior bug).

## Running (simulation)

```bash
# 1. Ensure wp_config.json has "shore_ip": "localhost"
# 2. Drop entry zips in ./submissions/
# 3. Activate the pyquaticus conda environment FIRST (see note below)
# 4. Start the console
python app.py            # serves http://127.0.0.1:5000   (or: flask run)
# 5. In the UI: choose team -> choose zip -> Submit, then launch each boat.
```

**Environment:** any terminal you launch from must have the pyquaticus conda
env active *before* starting `app.py` (or before running `submission_runner.py`
directly). The runner spawns the launcher via `sys.executable`, so the child
inherits whatever interpreter the parent is running — which is only the right
one (with `pyquaticus` installed) if the env was activated in that shell. A
shell without it active falls through to the system/pyenv Python and fails with
`ModuleNotFoundError: No module named 'pyquaticus'`.

**Time warp:** in sim the boats are launched at timewarp 4. The shoreside MOOS
community **must be launched at the same warp (4)** or `pHelmIvP` reports large
clock-skew errors. Matching the two eliminates the skew.

## Security note

`app.py` interpolates operator-supplied form values (IP, port, names) into shell
command strings (`sshpass`, `gnome-terminal`, `rsync`). This is acceptable on a
trusted single-operator mission LAN only. `_safe_ip()` / `_safe_port()` validate
the two free-form values, and the SSH password is stored in plaintext as a
LAN-only convenience. **Do not expose this app on an untrusted or multi-user
network without hardening it first.**

## Notable fixes in this cleanup

1. **`app.py`** — `handle_submission` hardware branch was a non-f-string, so the
   rsync command shipped literal `{zip_file}` / `{USERNAME}` / `{ip}` to the
   shell. Now interpolated; added `_safe_ip()` / `_safe_port()` validation.
2. **`submission_runner.py`** — removed a dead `from <team>_entry.solution import
   solution` that did nothing and could crash before the unzip completed;
   tightened blue/red routing from `"b" in color` to `color == 'blue'`.
3. **`submission_runner.py`** — spawn the launcher with `sys.executable` instead
   of the bare string `'python3'`, so the child process inherits the same
   interpreter (and conda env) as the parent rather than re-resolving `python3`
   from `PATH` (where a pyenv shim could select the wrong, pyquaticus-less one).
4. **`pyquaticus_moos_launcher.py`** — `action_space` corrected to `'continuous'`
   to match the policy; mission/log paths made env-configurable and consistent;
   removed the unreachable post-`while True` code and the discarded in-loop
   `normalize`/`state_to_obs` calls.

The competition entry files (`<team>_entry/solution.py`, `heuristic_policy.py`,
`gen_config.py`) were **not** modified — they are competitor submissions and the
infrastructure conforms to their contract.

## Status / known gaps

Resolved during cleanup:

- **`index.html` form contract — verified.** The form fields (`team`, `boat_id`,
  `boat_name`, `boat_ip`, `boat_port`, `action`) match what `app.py` reads. The
  `action` (form POST) vs `target` (JSON body to `/get_rsync_command`) split is
  internally consistent, not a bug.
- **`launch_surveyor.sh` interaction — verified.** The deploy UI calls it with
  the letter vehicle form (e.g. `-vu`) plus a role flag and `--role=CONTROL`.
  The cleaned `launch_surveyor.sh` intentionally accepts both letter (`-vu`) and
  numbered (`-v3`) forms so both this UI and the mission's `launch_sim.sh` work.

Still open:

- **Hardware rsync vs runner path.** Hardware mode rsyncs entries to `~/entries/`
  on each boat, but `/agent_action` passes `--entry_name=./<team>_entry/test.zip`.
  Reconcile these two locations before a real hardware run.
- **`index.html` C-USV button (minor).** The "C-USV" button references
  `{{ action }}` outside the `{% for action, label %}` loop, so it renders empty.
  Harmless today (the previewed command doesn't use the target), but a latent bug.
- **`static/style.css`** was not part of this pass.
