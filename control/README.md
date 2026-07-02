# MCTF Manual Control — Integration Guide (Path B, boat-qualified)

This wires a manual-override control surface into your USV_Deploy_UI setup so you
can command any boat — blue or red — to a posture or a specific role mid-match.
Commands are keyed on **boat_id** (`blue_one`, `red_two`, …), so the two teams
never collide even though both internally use `agent_0..agent_5`.

## How it fits together

```
control/mctf_control_server.py + index.html      (the UI + bridge — runs once)
      |  writes  MCTF_POSTURE_<boat_id> / MCTF_ROLE_<boat_id>
      v
   MOOSDB  (shoreside, :9000)         <- live path
      |
      v
 solution.py  (one process per boat)  <- reads its OWN boat's vars, because the
                                          launcher tells it MCTF_BOAT_ID=<boat_id>
```

If the MOOSDB is unreachable, the bridge and the policy both fall back to a
shared JSON control file (`MCTF_CONTROL_FILE`).

## The variable schema

| Variable                   | Values                                              |
|----------------------------|-----------------------------------------------------|
| `MCTF_POSTURE_<boat_id>`   | `auto` `attack` `balance` `defend`                  |
| `MCTF_ROLE_<boat_id>`      | `auto` `attack` `defend` `chase` `escort` `patrol`  |

`auto` = hand that lever back to the policy. Role beats posture for a given boat.
All-`auto` = the unmodified entry.

---

## Step 1 — the ONE harness edit

`pyquaticus_moos_launcher.py` already knows the boat it's running (`args.boat_id`).
Tell the policy process its own name by exporting `MCTF_BOAT_ID` **before** the
policy is instantiated. Find this line (~line 195):

```python
        agent_id = args.boat_id
```

Add one line right after it (or anywhere before `sol = solution()`):

```python
        os.environ["MCTF_BOAT_ID"] = args.boat_id   # tell the entry its own boat id
```

That's the entire harness change. It's pure identity-passing — the launcher
learns nothing about postures or roles. (`os` is already imported in that file.)

> Why this is enough: the policy reads `MCTF_BOAT_ID` at import and keys its MOOS
> subscriptions / lookups on `MCTF_ROLE_<that id>`. Without the var (e.g. the
> plain competition harness), the entry reverts to its old agent-keyed behavior
> and still runs as a normal submission.

## Step 2 — make solution.py BE the SAI policy

The harness imports the module named `solution` and `unzip -o`'s the entry dir on
every launch, so:

- Put the SAI policy in your entry zip **as `solution.py`** (and the SAI config as
  `gen_config.py`). Don't ship `_sai`-suffixed names — the launcher imports
  `solution`, not `solution_sai`.
- The provided `solution_sai.py` IS that policy; rename it to `solution.py` when
  you build the zip.

Quick check that your zip lands files at the entry root (not nested in a folder):

```bash
unzip -l your_entry.zip      # solution.py should appear with no dir prefix
```

## Step 3 — a stable control-file path (survives re-staging)

The staged `blue_entry/` / `red_entry/` dirs get wiped and re-extracted each
launch, so the fallback control file must live OUTSIDE them. Pick a stable path
and point BOTH the policy and the bridge at it. Add to `run.sh`, next to the
existing `export MCTF_MISSION_PATH=...`:

```bash
export MCTF_CONTROL_FILE="/home/tyler/USV_Deploy_UI/mctf_control.json"
```

Because `run.sh` `exec`s `app.py`, this env var flows down the whole chain
(`app.py -> submission_runner.py -> pyquaticus_moos_launcher.py -> solution.py`),
so every boat's policy reads the same fallback file. One file holds all boats'
commands; each boat only reads its own keys, so sharing it is fine.

## Step 4 — run the control UI

The bridge needs Flask. Confirm it's in the same env `run.sh` uses
(`/home/tyler/pyquaticus/env-full`):

```bash
/home/tyler/pyquaticus/env-full/bin/python -c "import flask" || \
  /home/tyler/pyquaticus/env-full/bin/pip install flask
```

Then start the bridge with the SAME control file path:

```bash
MCTF_CONTROL_FILE="/home/tyler/USV_Deploy_UI/mctf_control.json" \
  python control/mctf_control_server.py
# open http://127.0.0.1:5005
```

Or have `run.sh` launch it too, before the `exec "$PYTHON" app.py` line (it will
inherit MCTF_CONTROL_FILE from the export in Step 3):

```bash
( "$PYTHON" "$APP_DIR/control/mctf_control_server.py" ) >/dev/null 2>&1 &
```

## Verifying it works

- The UI's `link` dot: **MOOSDB** (green) = commands reaching the live DB;
  **FILE** (amber) = DB unreachable, writing the fallback file.
- Live-DB sanity check while clicking the UI:
  ```bash
  uXMS MCTF_POSTURE_blue_one MCTF_ROLE_blue_one
  ```
  You should see the value change as you click.
- File sanity check: `cat /home/tyler/USV_Deploy_UI/mctf_control.json` after a click.

## Known characteristics

- The bridge starts every lever at `auto` and does NOT adopt a pre-existing
  control file; the first command overwrites it. Fresh start = all auto = safe.
- The roster in `mctf_control_server.py` (`_DEFAULT_BOATS`) must match your
  `wp_config.json` boat_ids. They do today (`blue_one`…`red_three`). Override at
  runtime with `MCTF_BOAT_IDS="blue_one,blue_two,blue_three"` to show only your
  three.
- The live MOOS write path is coded to the documented pymoos `notify()` API but
  should be smoke-tested against your real DB. The file path is fully tested.

## TAK / ATAK later

Every command funnels through `ControlBridge.set_value(var, value)`. A CoT /
GeoChat listener (or your ATAK plugin) maps an operator action to the same call —
e.g. a chat command `blue_one chase` -> `bridge.set_value("MCTF_ROLE_blue_one",
"chase")`. No policy change, no second schema; the browser and TAK share one
write path, one MOOS connection, one fallback file.
