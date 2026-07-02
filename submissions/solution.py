"""
MCTF 2026 Strategic Submission — Heuristic Manager + Apollonius Defense

Architecture:
  - Game-state manager decides team strategy (all-attack, balanced, all-defend)
  - Per-agent role assignment based on game state and position
  - Apollonius Circle pursuit for mathematically guaranteed interception
  - Corner-aware flag carrying (MCTF 2026 corner capture mechanic)
  - No hardcoded team — works on both Blue and Red side

Key advantages over 2025 winner:
  1. Apollonius pursuit > proportional navigation (provably optimal)
  2. Corner-aware captures (2026 mechanic most teams won't know)
  3. Dynamic role switching based on score differential
  4. Catch-radius-aware interception (10m tag range)
"""

import math
import numpy as np
import os
import warnings

# Optional: load a trained actor for fallback
try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

import json
import time

# Optional: live MOOSDB connection. Present when running under the pyquaticus
# MOOS bridge; absent in the plain competition harness. Either way the rest of
# the control plane (file / env) still works, so this import never gates control.
try:
    import pymoos
    HAS_PYMOOS = True
except ImportError:
    HAS_PYMOOS = False


# =========================================================================
# Manual game-state override control plane
# =========================================================================
#
# WHAT
#   Lets a human operator override the strategic manager at runtime via a
#   MOOS-style variable, without editing or redeploying strategy code.
#
# WHY A SEPARATE "CONTROL PLANE" AT ALL
#   The strategy code (the manager + behaviors) should not care HOW a command
#   reaches it. We may run this entry in three different worlds:
#     - under a real MOOSDB (pyquaticus MOOS bridge),
#     - under the plain pyquaticus competition harness (no MOOS at all),
#     - on a teammate's laptop with neither, just env vars at launch.
#   If the strategy code reached out to pymoos directly, it would break the
#   moment pymoos isn't there. So we isolate "where does the command come from"
#   behind one object (PostureControl) and let the strategy ask it a plain
#   question: "what posture/role am I commanded to run, if any?" That keeps the
#   strategy identical across all three worlds and makes the transport swappable.
#
# THREE TRANSPORTS, READ IN A FIXED PRIORITY ORDER
#   Each step we consult, in order, the first source that yields a value. The
#   order below is a chosen convention; what matters for correctness is only that
#   it is fixed and documented, not that it ranks the sources by importance.
#     1. Live MOOSDB    (pymoos)  -- available under a real MOOS / pyquaticus
#                                    bridge. Can change mid-run via DB pokes.
#     2. Control file   (JSON)    -- available anywhere (no MOOS needed). Can
#                                    change mid-run by editing the file.
#     3. Environment vars         -- read from the process environment; fixed for
#                                    the process lifetime, so they cannot change
#                                    mid-run and act as a launch-time default.
#   Consequence of the order: a value present in a higher source hides any value
#   in a lower one (e.g. a live DB value hides a file value; a file value hides
#   an env value). To change the order, edit the tuple in _resolve.
#
# VARIABLE SCHEMA (values case-insensitive; "auto" hands control back to manager)
#
#   This entry runs one process PER BOAT, and the launcher tells each process its
#   own boat id via the MCTF_BOAT_ID environment variable (e.g. "blue_one"). The
#   command variables are keyed on that boat id so blue and red never collide
#   (both teams internally use agent_0..agent_5, so an agent-keyed name could not
#   tell them apart):
#
#     MCTF_POSTURE_<boat_id>   -> auto | attack | balance | defend
#     MCTF_ROLE_<boat_id>      -> auto | attack | defend | chase | escort | patrol
#         e.g.  MCTF_ROLE_blue_one = chase
#               MCTF_POSTURE_red_two = defend
#
#   FALLBACK (no MCTF_BOAT_ID, e.g. the plain pyquaticus competition harness,
#   which has no boats): the names revert to the un-suffixed / agent-keyed form
#     MCTF_POSTURE  and  MCTF_ROLE_<agent_id>
#   so the entry still works as an ordinary single-process submission.
#
#   "auto" IS A FIRST-CLASS VALUE (distinct from unset):
#     Setting a var to "auto" is treated identically to leaving it unset: the
#     override returns None and the automatic logic runs. This gives a way to
#     write "auto" explicitly (e.g. to leave the var in the control file as a
#     record) and get the same result as removing it. Posture "auto" + role
#     "auto" reproduces the original, unmodified entry for that boat.
#
#   PRECEDENCE -- role vs posture:
#     When BOTH a role (MCTF_ROLE_<boat_id>) and a posture (MCTF_POSTURE_<boat_id>)
#     are set for this boat, the role is the one that takes effect. This is simply
#     the order they are checked in (role first; see _strategic_action), not a
#     claim that one is the "right" lever. Practical consequence: a boat with
#     posture=defend and role=attack will attack.
#
# CONTROL-FILE FORMAT (JSON; keys are the full variable names for this boat):
#   { "MCTF_POSTURE_blue_one": "defend",
#     "MCTF_ROLE_blue_one": "auto" }
#   (In the no-boat fallback, use the un-suffixed keys instead.)

# Whitelists of accepted values. WHY validate against a set rather than trust
# the input: the variable is operator-typed (or poked from a shell), so typos
# like "defned" can occur. An unrecognized value must NOT crash the policy
# mid-match and must NOT be coerced into some arbitrary behavior; it is rejected
# (treated as "no valid command"), which means the automatic logic runs for that
# agent instead of the invalid command.
VALID_POSTURES = {"auto", "attack", "balance", "defend"}
VALID_ROLES = {"auto", "attack", "defend", "chase", "escort", "patrol"}

# ---- Boat identity & variable naming ----------------------------------------
#
# WHY BOAT-QUALIFIED VARIABLE NAMES:
#   In this harness, blue and red boats run as SEPARATE processes, and the policy
#   inside each is called with agent ids agent_0..agent_5 -- but BOTH teams use
#   that same agent_0..agent_5 space (blue's agent_0 and red's agent_0 are
#   different boats). So a variable keyed on agent_id alone (MCTF_ROLE_agent_0)
#   cannot tell the two teams' agent_0 apart: one command would hit both. To make
#   control team-agnostic, we key on the BOAT id (blue_one, red_two, ...), which
#   is globally unique across both teams.
#
# WHY AN ENV VAR (MCTF_BOAT_ID):
#   The policy is handed agent ids (agent_0), never boat ids -- the boat_id ->
#   agent_id mapping lives in the launcher, which the policy never sees. So the
#   launcher tells each policy process its own boat id by exporting MCTF_BOAT_ID
#   before running the policy. Each policy process is exactly one boat, so a
#   single value is all it needs.
#
#   If MCTF_BOAT_ID is UNSET (e.g. the plain pyquaticus competition harness,
#   which has no notion of boats), we fall back to the old agent-id-keyed names
#   so the entry still works as an ordinary submission.
#
# CRITICAL -- LAZY RESOLUTION (import-order bug fix):
#   These used to be resolved at MODULE IMPORT time. That is too early: the
#   launcher imports this module (`from blue_entry.solution import solution`)
#   BEFORE it sets os.environ["MCTF_BOAT_ID"]. So at import, MCTF_BOAT_ID is
#   unset, _BOAT_ID froze to None, the policy connected as the unsuffixed
#   "pMCTFCtrl", and publish_status() silently no-op'd forever (its first line is
#   `if _BOAT_ID is None: return`). The symptom was MCTF_ACTIVE_* never being
#   written (uXMS showed n/a).
#   Fix: resolve these lazily in _resolve_boat_identity(), which PostureControl
#   calls from its __init__ -- and __init__ runs when solution() is constructed,
#   which the launcher does AFTER setting the env var. So by then MCTF_BOAT_ID is
#   present and the names/identity resolve correctly.
_BOAT_ID = None
_MOOS_PORT = 9000
_POSTURE_VAR = "MCTF_POSTURE"
_ROLE_VAR = None
_ROLE_VAR_PREFIX = "MCTF_ROLE_"             # used only in the no-boat fallback


def _resolve_boat_identity():
    """(Re)read MCTF_BOAT_ID and MCTF_MOOS_PORT from the environment and rebuild
    the variable names + connection port. Called from PostureControl.__init__ so
    it runs AFTER the launcher has exported these, not at import time. Idempotent
    and safe to call more than once.

    WHY MCTF_MOOS_PORT matters: in this mission each boat runs its OWN surveyor
    MOOSDB on its own port (blue_one:9015, red_two:9012, ...), and those DBs do
    NOT share variables with shoreside (pShare here is input-only, no outbound
    routes -- verified). So the policy must publish/read its command + status
    vars on ITS OWN boat DB, not a global one. The launcher knows the right port
    (args.boat_port) and exports it; we read the real value rather than guessing
    a port scheme, so a config renumber can't silently break us. Falls back to
    9000 when unset (plain harness)."""
    global _BOAT_ID, _MOOS_PORT, _POSTURE_VAR, _ROLE_VAR
    _BOAT_ID = os.environ.get("MCTF_BOAT_ID")   # e.g. "blue_one"; None if unset
    try:
        _MOOS_PORT = int(os.environ.get("MCTF_MOOS_PORT", "9000"))
    except (TypeError, ValueError):
        _MOOS_PORT = 9000
    if _BOAT_ID:
        # Boat-qualified, team-agnostic names for THIS boat.
        _POSTURE_VAR = f"MCTF_POSTURE_{_BOAT_ID}"
        _ROLE_VAR = f"MCTF_ROLE_{_BOAT_ID}"
    else:
        # No-boat fallback: original posture name; roles keyed per agent id
        # (built at lookup time in get_role).
        _POSTURE_VAR = "MCTF_POSTURE"
        _ROLE_VAR = None


# The control file path. By default it sits next to this script, but the harness
# re-extracts the entry directory on every launch, which would wipe a file kept
# there. Set MCTF_CONTROL_FILE to a stable path OUTSIDE the entry dir so the
# command state survives re-staging. Falls back to the __file__-relative path
# when the env var is unset (e.g. plain harness).
_CONTROL_FILE = os.environ.get(
    "MCTF_CONTROL_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mctf_control.json"),
)


def _diag(msg):
    """Write a one-line diagnostic to a fixed log, so the policy's MOOS path is
    not a black box. WHY: connection failures here are swallowed by design (a bad
    DB must never crash a competition entry), but that silence made debugging
    'why is MCTF_ACTIVE n/a' nearly impossible. This logs to a stable path
    OUTSIDE the entry dir (which is wiped each launch) when MCTF_DIAG_LOG is set,
    else next to the control file. Best-effort: logging must never throw."""
    try:
        path = os.environ.get(
            "MCTF_DIAG_LOG",
            os.path.join(os.path.dirname(os.path.abspath(_CONTROL_FILE)),
                         "mctf_policy_diag.log"))
        with open(path, "a") as f:
            bid = _BOAT_ID or "?"
            f.write(f"[{time.strftime('%H:%M:%S')}] {bid}: {msg}\n")
    except Exception:
        pass


class PostureControl:
    """
    Single source of truth for manual posture/role overrides.

    WHY A DEDICATED CLASS (instead of a few module-level functions):
      It owns mutable state that must persist across calls -- the cached file
      contents, the file's last-read timestamp, and (if present) the live MOOS
      connection plus the latest values it has mailed us. Bundling that state
      with the lookup methods keeps it tidy and makes the "shared singleton"
      pattern below trivial: one object, one connection, one cache.

    WHY SHARED ACROSS ALL SIX AGENTS (see the singleton at the bottom):
      All six `solution` instances live in one process and tick in lockstep. If
      each made its own PostureControl, we'd open six MOOS connections and stat()
      the control file six times per simulation step -- wasteful and a source of
      subtle inconsistency (one agent could read a half-written file a tick
      before another). One shared instance gives every agent an identical,
      consistent view of the operator's intent.

    Reads are cheap and side-effect-free, and the file is polled on a throttle
    (see _refresh_file_cache) so we do not hit the disk every tick per agent.
    """

    def __init__(self, control_file=_CONTROL_FILE, file_poll_period=0.5):
        # Resolve boat identity NOW (not at import). The launcher sets
        # MCTF_BOAT_ID before constructing solution() -> PostureControl(), so by
        # this point the env var is present and the variable names resolve to the
        # boat-qualified form. See _resolve_boat_identity for the why.
        _resolve_boat_identity()
        _diag(f"PostureControl init: _BOAT_ID={_BOAT_ID} _MOOS_PORT={_MOOS_PORT}"
              f" HAS_PYMOOS={HAS_PYMOOS} env_BOAT_ID={os.environ.get('MCTF_BOAT_ID')}"
              f" env_MOOS_PORT={os.environ.get('MCTF_MOOS_PORT')}")

        self._control_file = control_file
        # WHY throttle file reads: compute_action runs at sim rate (tau=0.1s ->
        # ~10 Hz, often faster under sim_speedup). Re-reading + JSON-parsing the
        # file at full rate is pure overhead -- an operator can't type that fast
        # and we don't need sub-second reaction to a manual command. 0.5s is a
        # comfortable human-reaction cadence that costs ~2 disk checks/sec total.
        self._file_poll_period = file_poll_period   # seconds between disk reads
        self._file_cache = {}                       # last successfully parsed contents
        self._file_last_read = 0.0                  # wall-clock of last poll attempt
        self._file_mtime = None                     # mtime of last parsed version

        # Live MOOSDB state. _moos_vars is filled asynchronously by the on_mail
        # callback whenever the DB notifies us of a change; the lookup methods
        # just read this dict. WHY a cache dict rather than querying the DB on
        # demand: pymoos is push-based (mail callbacks), not request/response, so
        # the idiomatic pattern is to cache the latest notified value and read it.
        self._moos_vars = {}                        # var name -> raw string value
        self._comms = None
        self._connected = False                     # set True in _on_connect
        self._last_status_published = None          # de-dupe status notifies
        # Only attempt a connection if pymoos imported. WHY guard here too (the
        # import is already guarded): so that on a no-MOOS machine we never even
        # construct a comms object, and the file/env paths are the whole story.
        if HAS_PYMOOS:
            self._try_connect_moos()

    # ----- MOOSDB transport -------------------------------------------------

    def _try_connect_moos(self):
        """
        Best-effort async MOOSDB connection.

        WHY 'best-effort' / why every failure is swallowed: a missing or
        unreachable MOOSDB is a totally normal, expected condition (it's the
        whole point that this also runs without MOOS). A connection failure here
        must NEVER propagate -- it would take down an otherwise-valid competition
        submission. On any failure we simply leave _comms as None and the
        resolver falls through to the file and env transports as if MOOS didn't
        exist.
        """
        try:
            self._comms = pymoos.comms()

            def _on_connect():
                # Subscribe to the variables this boat cares about. The second
                # arg (0) is the change-threshold: 0 == notify on every write.
                # WHY register on connect rather than lazily: subscriptions are
                # cheap and the set is tiny, and doing it here means we never miss
                # a notification because we hadn't subscribed yet.
                self._connected = True   # gate for publish_status notifies
                _diag(f"MOOS on_connect fired -> CONNECTED on port {_MOOS_PORT};"
                      f" registering {_POSTURE_VAR}, {_ROLE_VAR}")
                self._comms.register(_POSTURE_VAR, 0)
                if _ROLE_VAR is not None:
                    # Boat mode: one role var for THIS boat (MCTF_ROLE_blue_one).
                    self._comms.register(_ROLE_VAR, 0)
                else:
                    # No-boat fallback: the six agent-keyed role vars.
                    for i in range(6):
                        self._comms.register(f"{_ROLE_VAR_PREFIX}agent_{i}", 0)
                return True

            def _on_mail():
                # Drain all pending mail and cache the latest string value of
                # each var. WHY wrap each message in try/except: one malformed
                # message (e.g. a var poked as a double instead of a string)
                # must not abort processing the rest of the mail batch.
                for msg in self._comms.fetch():
                    try:
                        self._moos_vars[msg.key()] = msg.string().strip()
                    except Exception:
                        pass
                return True

            self._comms.set_on_connect_callback(_on_connect)
            self._comms.set_on_mail_callback(_on_mail)
            # Host / port / process-name. The port is THIS boat's own surveyor
            # MOOSDB (from MCTF_MOOS_PORT, set by the launcher to args.boat_port);
            # each boat has its own DB and they don't share vars, so we must use
            # the boat's own port, not a global 9000. The client name must be
            # UNIQUE per connection -- two boats connecting with the same name
            # would make the DB bounce one -- so we suffix it with the boat id
            # (e.g. pMCTFCtrl_blue_one). The name shows up in the DB's client
            # list, handy when debugging which clients are connected.
            client_name = f"pMCTFCtrl_{_BOAT_ID}" if _BOAT_ID else "pMCTFCtrl"
            _diag(f"MOOS connect attempt: localhost:{_MOOS_PORT} as {client_name}"
                  f" (HAS_PYMOOS={HAS_PYMOOS})")
            self._comms.run("localhost", _MOOS_PORT, client_name)
            _diag(f"MOOS run() returned (async connect started, port {_MOOS_PORT})")
        except Exception as e:
            self._comms = None  # fall back to file / env transparently
            _diag(f"MOOS connect FAILED: {type(e).__name__}: {e}")

    def _moos_lookup(self, var):
        # Return the cached value lower-cased, or None if absent/empty. WHY
        # lower-case here: so the rest of the code can compare against the
        # lower-case whitelists without caring how the operator capitalized it.
        v = self._moos_vars.get(var)
        return v.lower() if isinstance(v, str) and v else None

    # ----- status return channel (boat -> UI) -------------------------------

    def publish_status(self, behavior, source):
        """
        Report this boat's currently-executing behavior back to the operator.

        WHY a return channel at all: commands flow UI -> policy, but without this
        the operator has no confirmation the boat actually did what was asked.
        This publishes MCTF_ACTIVE_<boat_id> = "<behavior>:<source>" so the
        control UI can show, per boat, both WHAT it is doing and WHY:
          source = "role"     -> a per-boat ROLE override drove this behavior
          source = "posture"  -> a team-wide POSTURE drove this behavior (and no
                                 per-boat role overrode it; role wins if both set)
          source = "auto"      -> the policy's own logic chose it
          source = "reflex"    -> a hard safety action (tagged/disabled/etc.)
                                  took precedence over everything, including a
                                  command -- so the operator sees the command did
                                  not "fail", the boat is just surviving first.

        Best-effort and side-effect-free on failure: if MOOS isn't connected we
        simply don't publish (the UI will show the boat as "no report"). We only
        re-publish when the value changes, to avoid spamming the DB every tick.
        """
        if _BOAT_ID is None:
            if not getattr(self, "_warned_no_boat", False):
                self._warned_no_boat = True
                _diag("publish_status SKIPPED: _BOAT_ID is None (no identity)")
            return  # no boat identity -> nothing meaningful to key the report on
        val = f"{behavior}:{source}"
        if val == self._last_status_published:
            return  # unchanged since last tick; don't re-notify
        self._last_status_published = val
        if self._comms and self._connected:
            try:
                self._comms.notify(f"MCTF_ACTIVE_{_BOAT_ID}", val, pymoos.time())
                if not getattr(self, "_logged_first_pub", False):
                    self._logged_first_pub = True
                    _diag(f"publish_status OK: wrote MCTF_ACTIVE_{_BOAT_ID}={val}"
                          f" on port {_MOOS_PORT}")
            except Exception as e:
                _diag(f"publish_status notify FAILED: {type(e).__name__}: {e}")
        else:
            # Connected==False or no comms: this is the silent-failure case we
            # spent rounds chasing. Log it ONCE so it's visible.
            if not getattr(self, "_warned_not_connected", False):
                self._warned_not_connected = True
                _diag(f"publish_status NOT SENT: comms={self._comms is not None},"
                      f" connected={self._connected} (would-be"
                      f" MCTF_ACTIVE_{_BOAT_ID}={val}) -- file fallback only")

    def publish_team_posture(self, team, auto_posture):
        """Report the posture the AUTONOMY has chosen for the team (defend /
        balance / adaptive), derived from the score situation. This is the
        team-level analogue of publish_status: it tells the operator what stance
        the auto logic is running team-wide when not commanded, so the team
        section can show "auto: defend" the same way a boat shows its role.

        Published as MCTF_AUTO_POSTURE_<team>. All three boats on a team compute
        the identical value (it's a pure function of the score), so each just
        publishes its own team's value to its own DB; the bridge reads any one.
        De-duped so we only notify on change."""
        if _BOAT_ID is None:
            return
        val = str(auto_posture)
        if val == getattr(self, "_last_team_posture_published", None):
            return
        self._last_team_posture_published = val
        if self._comms and self._connected:
            try:
                self._comms.notify(f"MCTF_AUTO_POSTURE_{team}", val, pymoos.time())
            except Exception:
                pass

    # ----- file transport ---------------------------------------------------

    def _refresh_file_cache(self):
        """
        Poll the control file at most once per _file_poll_period seconds, and
        only re-parse it when its modification time has actually changed.

        WHY the two-layer throttle (time gate THEN mtime gate):
          - The time gate bounds how often we touch the disk at all (rate limit).
          - The mtime gate avoids re-parsing JSON when the file is unchanged,
            which is the common case -- the operator edits it rarely, but we
            check it often. Together they make the steady-state cost a single
            cheap getmtime() call every 0.5s, with a full read+parse only right
            after an actual edit.
        """
        now = time.time()
        if now - self._file_last_read < self._file_poll_period:
            return
        self._file_last_read = now
        try:
            mtime = os.path.getmtime(self._control_file)
            if mtime == self._file_mtime:
                return  # file unchanged since we last parsed it -> nothing to do
            with open(self._control_file, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Normalize every key and value to a stripped, lower-cased string
                # so downstream lookups are case/whitespace insensitive and so a
                # value written as a JSON number (e.g. forgetting the quotes)
                # still becomes a comparable string rather than crashing.
                self._file_cache = {
                    str(k): str(v).strip().lower() for k, v in data.items()
                }
                self._file_mtime = mtime
        except FileNotFoundError:
            # No file is the normal "no overrides" state, not an error. Clear any
            # previous cache so a deleted file correctly means "back to auto".
            self._file_cache = {}
            self._file_mtime = None
        except Exception:
            # Malformed JSON / permission error / a half-written file caught
            # mid-save. WHY keep the last good cache rather than clearing it: an
            # operator saving the file in a non-atomic editor can momentarily
            # present invalid JSON; we don't want that transient to blip every
            # agent back to auto. We just keep the last valid command until the
            # next clean read.
            pass

    def _file_lookup(self, var):
        self._refresh_file_cache()
        v = self._file_cache.get(var)
        return v if v else None

    # ----- env transport ----------------------------------------------------

    @staticmethod
    def _env_lookup(var):
        # Lowest-priority source. Static for the process lifetime, so there's no
        # caching or polling to do -- just read it each time. Stripped+lowered
        # for the same case-insensitivity reason as the other transports.
        v = os.environ.get(var)
        return v.strip().lower() if isinstance(v, str) and v.strip() else None

    # ----- resolution -------------------------------------------------------

    def _resolve(self, var):
        """
        Return the first non-empty value across the transports, in priority
        order (MOOS -> file -> env), or None if no transport has it.

        WHY "first non-empty wins" and NOT "first VALID wins": validity is
        checked one level up, in get_posture/get_role. The distinction matters --
        see the note there about why an invalid-but-present value deliberately
        does not fall through to a lower-priority transport.
        """
        for fn in (self._moos_lookup, self._file_lookup, self._env_lookup):
            val = fn(var)
            if val:
                return val
        return None

    def get_posture(self):
        """
        The commanded team posture, or None if on auto / unset / invalid.

        Returning None is the signal to the caller "I have no posture command
        for you, run your normal logic", so all three of {unset, "auto",
        garbage} collapse to None on purpose.
        """
        val = self._resolve(_POSTURE_VAR)
        # "auto" is explicitly excluded so it behaves exactly like unset.
        if val in VALID_POSTURES and val != "auto":
            return val
        return None

    def get_role(self, agent_id):
        """
        The commanded role override for this boat, or None.

        In boat mode (MCTF_BOAT_ID set), the whole process is a single boat, so
        there is one role var (_ROLE_VAR, e.g. MCTF_ROLE_blue_one) and the passed
        agent_id is ignored -- it is always this boat's agent. In the no-boat
        fallback, the var is keyed per agent id (MCTF_ROLE_agent_0).

        BEHAVIOR NOTE -- invalid values do NOT fall through to a lower-priority
        transport. If the file says the role is "atack" (typo) while an env var
        says "defend", get_role returns None, not "defend". This is a consequence
        of how _resolve works: it returns the first transport with ANY non-empty
        value, and validity is only checked afterward here. So a present-but-
        invalid value in a higher-priority transport ends the search and then
        fails the whitelist, yielding None (-> the automatic logic runs). To
        change which source wins, correct the value in the higher-priority
        transport rather than relying on a lower one.
        """
        var = _ROLE_VAR if _ROLE_VAR is not None else f"{_ROLE_VAR_PREFIX}{agent_id}"
        val = self._resolve(var)
        if val in VALID_ROLES and val != "auto":
            return val
        return None


# Process-wide singleton. WHY a module-level singleton rather than constructing
# one per solution instance: see PostureControl's docstring -- one shared object
# means one MOOS connection and one throttled file poller for all six agents,
# giving them a single consistent view of operator intent each tick. Lazily
# created so importing this module has no side effects (no socket opened) until
# the first agent actually asks for control.
_POSTURE_CONTROL_SINGLETON = None


def _get_posture_control():
    global _POSTURE_CONTROL_SINGLETON
    if _POSTURE_CONTROL_SINGLETON is None:
        _POSTURE_CONTROL_SINGLETON = PostureControl()
    return _POSTURE_CONTROL_SINGLETON


# =========================================================================
# Observation indices (MCTF 2026, 3v3, normalized obs, no lidar)
# =========================================================================
OPP_HOME_BEARING = 0;  OPP_HOME_DIST = 1
OWN_HOME_BEARING = 2;  OWN_HOME_DIST = 3
WALL_S_BEARING = 4;    WALL_S_DIST = 5    # south wall
WALL_W_BEARING = 6;    WALL_W_DIST = 7    # west wall
WALL_N_BEARING = 8;    WALL_N_DIST = 9    # north wall
WALL_E_BEARING = 10;   WALL_E_DIST = 11   # east wall
SCRIM_BEARING = 12;    SCRIM_DIST = 13
SPEED = 14
HAS_FLAG = 15
ON_SIDE = 16
TAG_COOLDOWN = 17
IS_TAGGED = 18
IS_DISABLED = 19
TEAM_SCORE = 20
OPP_SCORE = 21
# Other agents: 9 features each starting at index 22
# Order: teammates first (2), then opponents (3)
# Per agent: bearing, dist, rel_heading, speed, has_flag, on_side, tag_cooldown, is_tagged, is_disabled

# Max desired speed — env clips to actual max internally
MAX_SPEED = 3.0

def _other_agent(obs, idx):
    """Get 9-feature block for another agent (0-indexed from first other agent)."""
    base = 22 + idx * 9
    return obs[base:base + 9]


# =========================================================================
# Geometry helpers (no hardcoded positions — everything from obs/global_state)
# =========================================================================

def _bearing_to_heading(bearing_norm):
    """Convert normalized bearing [-1,1] to heading command [-90,90]."""
    return float(np.clip(bearing_norm * 180.0, -90.0, 90.0))


def _angle_between(pos1, pos2):
    """Absolute bearing from pos1 to pos2 in degrees (0=north, 90=east)."""
    dx = pos2[0] - pos1[0]
    dy = pos2[1] - pos1[1]
    return math.degrees(math.atan2(dx, dy)) % 360


def _rel_heading(my_heading, abs_bearing):
    """Relative heading from my_heading to abs_bearing, in [-180, 180]."""
    rel = (abs_bearing - my_heading) % 360
    if rel > 180:
        rel -= 360
    return rel


def _dist(p1, p2):
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)


# =========================================================================
# Apollonius Circle utilities
# =========================================================================

def can_intercept(my_pos, opp_pos, target_pos, my_speed, opp_speed):
    """
    Can I reach target_pos before opponent does?
    Uses time-to-reach comparison.
    """
    my_time = _dist(my_pos, target_pos) / max(my_speed, 0.01)
    opp_time = _dist(opp_pos, target_pos) / max(opp_speed, 0.01)
    return my_time < opp_time


def apollonius_heading(my_pos, my_heading, opp_pos, catch_radius=10.0):
    """
    Compute heading to intercept opponent.
    Full ±90° for maximum agility when chasing.
    """
    dx = opp_pos[0] - my_pos[0]
    dy = opp_pos[1] - my_pos[1]
    dist = math.sqrt(dx*dx + dy*dy)

    if dist < 0.1:
        return [MAX_SPEED, 0.0]

    abs_bearing = math.degrees(math.atan2(dx, dy)) % 360
    rel = _rel_heading(my_heading, abs_bearing)

    return [MAX_SPEED, float(np.clip(rel, -90.0, 90.0))]


# =========================================================================
# Action behaviors (each returns [speed, heading])
# =========================================================================

def action_go_to_flag(obs, global_state=None, agent_id=None):
    """
    Smart attack: head toward opponent flag using Voronoi gap + wall hugging.
    
    Strategy based on reach-avoid game theory:
    1. If no defenders nearby → go straight to flag (fastest)
    2. If defender in front → hug the nearest wall (limits their approach angle)
    3. Use the side with more space to flank around defenders
    """
    flag_heading = float(np.clip(obs[OPP_HOME_BEARING] * 180, -60, 60))

    # Find closest untagged opponent and their position
    closest_dist = float('inf')
    closest_bearing = 0
    for i in range(3):
        blk = _other_agent(obs, 2 + i)
        if blk[7] > 0:  # tagged, skip
            continue
        opp_dist = blk[1] + 1.0  # unnormalize
        if opp_dist < closest_dist:
            closest_dist = opp_dist
            closest_bearing = blk[0] * 180

    # No nearby defenders → go straight
    if closest_dist > 0.6:
        return [MAX_SPEED, flag_heading]

    # Defender is close — use wall hugging evasion
    # Check which wall (top/bottom) is closer and hug it
    # This limits the defender's approach angle to one side
    north_dist = obs[WALL_N_DIST]  # normalized, more negative = closer
    south_dist = obs[WALL_S_DIST]

    if abs(closest_bearing) < 45:
        # Defender directly in front — flank via nearest wall
        if north_dist > south_dist:
            # More space to north → go north then toward flag
            return [MAX_SPEED, float(np.clip(flag_heading - 40, -60, 60))]
        else:
            # More space to south → go south then toward flag
            return [MAX_SPEED, float(np.clip(flag_heading + 40, -60, 60))]

    # Defender to the side — angle away slightly
    if closest_bearing > 0:
        return [MAX_SPEED, float(np.clip(flag_heading - 20, -60, 60))]
    else:
        return [MAX_SPEED, float(np.clip(flag_heading + 20, -60, 60))]

def action_go_to_flag_with_bias(obs, biased_heading):
    """Go to flag with a directional bias (for split attack coordination)."""
    # Find closest untagged opponent
    closest_dist = float('inf')
    closest_bearing = 0
    for i in range(3):
        blk = _other_agent(obs, 2 + i)
        if blk[7] > 0:
            continue
        opp_dist = blk[1] + 1.0
        if opp_dist < closest_dist:
            closest_dist = opp_dist
            closest_bearing = blk[0] * 180

    # No nearby defenders → use biased heading
    if closest_dist > 0.6:
        return [MAX_SPEED, biased_heading]

    # Defender close and in front → increase the bias to flank harder
    if abs(closest_bearing) < 45:
        north_dist = obs[WALL_N_DIST]
        south_dist = obs[WALL_S_DIST]
        if north_dist > south_dist:
            return [MAX_SPEED, float(np.clip(biased_heading - 40, -60, 60))]
        else:
            return [MAX_SPEED, float(np.clip(biased_heading + 40, -60, 60))]

    # Defender to the side → angle away slightly
    if closest_bearing > 0:
        return [MAX_SPEED, float(np.clip(biased_heading - 20, -60, 60))]
    else:
        return [MAX_SPEED, float(np.clip(biased_heading + 20, -60, 60))]


    """
    Check if I'm the closest entity to the opponent flag.
    Adapted from Tim's closest_to_their_flag.
    Only attack if we're the best positioned — prevents wasted effort.
    """
    # My distance to opponent flag
    flag_bearing_rad = obs[OPP_HOME_BEARING] * math.pi
    flag_dist = obs[OPP_HOME_DIST] + 1.0

    my_flag_x = flag_dist * math.cos(flag_bearing_rad)
    my_flag_y = flag_dist * math.sin(flag_bearing_rad)
    my_dist_to_flag = math.sqrt(my_flag_x**2 + my_flag_y**2)

    # Check if any teammate is closer (or already has the flag)
    for i in range(2):  # 2 teammates
        blk = _other_agent(obs, i)
        if blk[4] > 0:  # teammate has flag already
            return False  # don't attack, escort instead

        # Compute teammate's distance to flag
        tm_bearing_rad = blk[0] * math.pi
        tm_dist = blk[1] + 1.0
        tm_x = tm_dist * math.cos(tm_bearing_rad)
        tm_y = tm_dist * math.sin(tm_bearing_rad)

        # Teammate position relative to flag
        tm_to_flag = math.sqrt(
            (my_flag_x - tm_x)**2 + (my_flag_y - tm_y)**2
        )
        if tm_to_flag < my_dist_to_flag * 0.8:  # teammate significantly closer
            return False

    return True


def action_carry_to_corner(obs, global_state=None, agent_id=None):
    """
    Carry flag: if on opponent side → rush to scrimmage (safety).
    Once on own side → untouchable → head to nearest corner.
    """
    if global_state and agent_id and isinstance(global_state, dict):
        try:
            my_pos = np.array(global_state.get((agent_id, 'pos'), None), dtype=np.float64)
            my_heading = global_state.get((agent_id, 'heading'), 0.0)
            on_side = global_state.get((agent_id, 'on_side'), True)

            if my_pos is not None:
                idx = int(agent_id.split('_')[1])
                if idx < 3:
                    corners = [np.array([0.0, 80.0]), np.array([0.0, 0.0])]
                    scrimmage_x = 80.0  # midfield
                else:
                    field_w = global_state.get('red_flag_home', [140, 40])[0] + \
                              global_state.get('blue_flag_home', [20, 40])[0]
                    corners = [np.array([field_w, 80.0]), np.array([field_w, 0.0])]
                    scrimmage_x = 80.0

                if not on_side:
                    # ON OPPONENT SIDE — can be tagged! Rush to scrimmage first
                    # Head straight toward our side (minimize time in danger zone)
                    if idx < 3:
                        target = np.array([scrimmage_x - 5, my_pos[1]])  # just past scrimmage
                    else:
                        target = np.array([scrimmage_x + 5, my_pos[1]])

                    dx = target[0] - my_pos[0]
                    dy = target[1] - my_pos[1]
                    abs_bearing = math.degrees(math.atan2(dx, dy)) % 360
                    rel = _rel_heading(my_heading, abs_bearing)
                    return [MAX_SPEED, float(np.clip(rel, -90.0, 90.0))]
                else:
                    # ON OWN SIDE — untouchable! Head to nearest corner
                    dists = [_dist(my_pos, c) for c in corners]
                    target = corners[0] if dists[0] < dists[1] else corners[1]

                    dx = target[0] - my_pos[0]
                    dy = target[1] - my_pos[1]
                    abs_bearing = math.degrees(math.atan2(dx, dy)) % 360
                    rel = _rel_heading(my_heading, abs_bearing)
                    return [MAX_SPEED, float(np.clip(rel, -90.0, 90.0))]
        except Exception:
            pass

    # Fallback: head toward own home at max speed
    return [MAX_SPEED, float(np.clip(obs[OWN_HOME_BEARING] * 180, -60, 60))]


def action_defend_patrol(obs):
    """Patrol near own flag. Go home if far, slow down if close."""
    home_dist = (obs[OWN_HOME_DIST] + 1) / 2  # unnormalize to [0,1]
    if home_dist > 0.3:
        return [2.5, _bearing_to_heading(obs[OWN_HOME_BEARING])]
    else:
        # Slow patrol — small random heading adjustments
        return [1.0, _bearing_to_heading(obs[OWN_HOME_BEARING])]


def action_intercept_opponent(obs, opp_idx):
    """Head toward a specific opponent using lead pursuit."""
    blk = _other_agent(obs, opp_idx)
    bearing_norm = blk[0]
    dist_norm = blk[1] + 1.0
    rel_heading_norm = blk[2]
    target_speed_norm = blk[3] + 1.0

    # Lead pursuit: aim where they'll be
    bearing_rad = math.pi / 2 - bearing_norm * math.pi
    target_x = dist_norm * math.cos(bearing_rad)
    target_y = dist_norm * math.sin(bearing_rad)

    target_heading_rad = -rel_heading_norm * math.pi + bearing_rad - math.pi
    vx = target_speed_norm * math.cos(target_heading_rad)
    vy = target_speed_norm * math.sin(target_heading_rad) - (obs[SPEED] + 1.0)

    lead = 0.3 * np.clip(dist_norm, 0.05, 1.0)
    aim_x = target_x + vx * lead
    aim_y = target_y + vy * lead

    angle = 90.0 - math.degrees(math.atan2(aim_y, aim_x))
    while angle > 180: angle -= 360
    while angle < -180: angle += 360

    return [MAX_SPEED, float(np.clip(angle, -90.0, 90.0))]


def action_dodge_walls(obs):
    """If near a wall, turn away. Returns (action, urgency)."""
    best_angle = 0.0
    best_weight = 0.0
    for i in range(4):
        bearing_idx = 4 + 2 * i
        dist_idx = 5 + 2 * i
        wall_dist = obs[dist_idx] + 1.0
        wall_bearing = obs[bearing_idx]
        weight = 0.2 / (wall_dist ** 2 + 1e-6)
        if abs(wall_bearing * 180.0) < 90.0 and weight > best_weight:
            if wall_bearing > 0:
                best_angle = np.clip(-180.0 - wall_bearing * 180.0, -70.0, 70.0)
            else:
                best_angle = np.clip(180.0 - wall_bearing * 180.0, -70.0, 70.0)
            best_weight = weight
    return [MAX_SPEED, best_angle], best_weight


def action_retreat_to_corner(obs, global_state=None, agent_id=None):
    """When tagged, head to nearest untag corner (home side corners)."""
    return action_carry_to_corner(obs, global_state, agent_id)


def _apply_collision_avoidance(obs, action):
    """
    Nudge heading away from very close agents (within ~6m) to reduce collisions.
    Collisions (within 4m) are a tiebreaker penalty.
    """
    nudge = 0.0
    for i in range(5):  # all 5 other agents (2 teammates + 3 opponents)
        blk = _other_agent(obs, i)
        dist = blk[1] + 1.0  # unnormalize
        bearing = blk[0] * 180
        # If very close and in front, nudge away
        if dist < 0.1 and abs(bearing) < 60:  # ~8m, in front
            if bearing > 0:
                nudge -= 10.0
            else:
                nudge += 10.0
    if abs(nudge) > 0:
        new_heading = float(np.clip(action[1] + nudge, -90, 90))
        return [action[0], new_heading]
    return action


def action_escort(obs, teammate_idx):
    """Follow a teammate (stay near them for protection)."""
    blk = _other_agent(obs, teammate_idx)
    bearing = blk[0]
    dist = blk[1]
    # Stay close but not on top
    if dist > -0.5:  # normalized, roughly > 10m
        return [MAX_SPEED, _bearing_to_heading(bearing)]
    else:
        return [2.0, _bearing_to_heading(bearing)]


# =========================================================================
# Strategic manager — decides what each agent should do
# =========================================================================

def _auto_team_posture(gs):
    """The posture the AUTONOMY runs team-wide, derived from the score the same
    way _strategic_action branches on score_diff. Returned in the same
    vocabulary the operator sees, so the UI can show "auto: <posture>":

        score_diff >= 2  -> 'defend'    (winning big: all defend)
        score_diff == 1  -> 'balance'   (winning by 1: 1 attacker + 2 defenders)
        score_diff <= 0  -> 'adaptive'  (tied/losing: attack when clear, else
                                         match defenders to intruders)

    'adaptive' is named honestly rather than 'attack': in that state the team
    does NOT simply attack -- it assigns defenders to threats and attacks only
    when the field is clear. Keep this mapping in lockstep with the score_diff
    branches in _strategic_action; if those thresholds change, change them here."""
    sd = gs.score_diff
    if sd >= 2:
        return 'defend'
    if sd >= 1:
        return 'balance'
    return 'adaptive'


class GameState:
    """Parse global_state into a clean structure. Works for both Blue and Red."""

    def __init__(self, agent_id, global_state):
        self.gs = global_state
        self.agent_id = agent_id

        # Detect team from agent_id
        idx = int(agent_id.split('_')[1])
        if idx < 3:
            self.team = 'blue'
            self.team_ids = ['agent_0', 'agent_1', 'agent_2']
            self.opp_ids = ['agent_3', 'agent_4', 'agent_5']
        else:
            self.team = 'red'
            self.team_ids = ['agent_3', 'agent_4', 'agent_5']
            self.opp_ids = ['agent_0', 'agent_1', 'agent_2']

        self.opp_team = 'red' if self.team == 'blue' else 'blue'

    @property
    def my_score(self):
        return self.gs.get(f'{self.team}_team_score', 0)

    @property
    def opp_score(self):
        return self.gs.get(f'{self.opp_team}_team_score', 0)

    @property
    def score_diff(self):
        return self.my_score - self.opp_score

    @property
    def my_flag_taken(self):
        return self.gs.get(f'{self.team}_flag_pickup', False)

    @property
    def opp_flag_taken(self):
        return self.gs.get(f'{self.opp_team}_flag_pickup', False)

    def agent_pos(self, aid):
        return np.array(self.gs.get((aid, 'pos'), [0, 0]), dtype=np.float64)

    def agent_heading(self, aid):
        return self.gs.get((aid, 'heading'), 0.0)

    def agent_has_flag(self, aid):
        return self.gs.get((aid, 'has_flag'), False)

    def agent_is_tagged(self, aid):
        return self.gs.get((aid, 'is_tagged'), False)

    def agent_on_side(self, aid):
        return self.gs.get((aid, 'on_side'), True)

    def agent_speed(self, aid):
        return self.gs.get((aid, 'speed'), 0.0)

    def agent_tag_cooldown(self, aid):
        return self.gs.get((aid, 'tagging_cooldown'), 0.0)

    def agent_is_disabled(self, aid):
        return self.gs.get((aid, 'is_disabled'), False)

    @property
    def my_flag_home(self):
        return np.array(self.gs.get(f'{self.team}_flag_home', [0, 0]), dtype=np.float64)

    @property
    def opp_flag_pos(self):
        return np.array(self.gs.get(f'{self.opp_team}_flag_pos', [0, 0]), dtype=np.float64)

    def intruders(self):
        """Opponents on OUR side, untagged, not disabled."""
        result = []
        for aid in self.opp_ids:
            if not self.agent_on_side(aid) and not self.agent_is_tagged(aid) and not self.agent_is_disabled(aid):
                result.append(aid)
        return result

    def flag_carrier(self):
        """Opponent carrying our flag, or None."""
        for aid in self.opp_ids:
            if self.agent_has_flag(aid):
                return aid
        return None

    def teammate_with_flag(self):
        """Teammate carrying opponent's flag, or None."""
        for aid in self.team_ids:
            if self.agent_has_flag(aid):
                return aid
        return None


# =========================================================================
# Main solution class
# =========================================================================

class solution:
    """
    MCTF 2026 Strategic Submission.
    
    Game-state manager + heuristic behaviors + Apollonius defense.
    Works on both Blue and Red side. No hardcoded positions.
    """

    def __init__(self):
        # Scouting + opening phase
        self.opening_done = False
        self.step_count = 0
        self.max_intruders_seen = 0  # track opponent aggression
        self.opp_is_3A = False

        # Manual override control plane (MOOS var / control file / env var).
        # WHY fetch the shared singleton instead of constructing our own: all six
        # agents must see one identical, consistent command source each tick, and
        # we want exactly one MOOS connection / file poller for the whole process
        # rather than six. See _get_posture_control / PostureControl docstring.
        self.posture_control = _get_posture_control()

        # Optional: load trained actor models for fallback
        self.actors = {}
        if HAS_TORCH:
            model_dir = os.path.dirname(os.path.abspath(__file__))
            for role, fname in [('attacker', 'attacker_actor.pt'),
                                ('defender', 'defender_actor.pt'),
                                ('combined', 'combined_actor.pt')]:
                path = os.path.join(model_dir, fname)
                if os.path.exists(path):
                    try:
                        from dqn_models import ActorNetwork
                        state_dict = torch.load(path, map_location='cpu', weights_only=True)
                        obs_dim = state_dict['fc1.weight'].shape[1]
                        actor = ActorNetwork(obs_dim)
                        actor.load_state_dict(state_dict)
                        actor.eval()
                        self.actors[role] = actor
                    except Exception:
                        pass

    def compute_action(self, agent_id: str, full_obs_normalized: dict,
                       full_obs: dict, global_state: dict) -> list:
        """Main entry point. Returns [speed, heading_angle] continuous action.

        Wraps the real logic so that, after deciding an action, we publish this
        boat's currently-executing behavior + source back to the operator (see
        PostureControl.publish_status). _behavior / _source are set at each
        decision point below; reflexes set source='reflex', the strategic path
        sets 'role', 'posture', or 'auto' inside _strategic_action.
        """
        self._behavior = 'unknown'
        self._source = 'auto'
        action = self._compute_action_impl(agent_id, full_obs_normalized,
                                           full_obs, global_state)
        # Report what we actually decided this tick (best-effort; no-op if MOOS
        # isn't connected). This is the return channel that lets the UI confirm
        # the boat is doing what was commanded.
        try:
            self.posture_control.publish_status(self._behavior, self._source)
        except Exception:
            pass
        # Also report the posture the AUTONOMY would run team-wide (from the
        # score situation), so the team section can show "auto: defend/balance/
        # adaptive". This mirrors, at team level, what each boat's chip shows at
        # the per-boat level. Best-effort; needs a valid game state.
        try:
            gs = GameState(agent_id, global_state) if (
                isinstance(global_state, dict) and global_state) else None
            if gs is not None:
                self.posture_control.publish_team_posture(
                    gs.team, _auto_team_posture(gs))
        except Exception:
            pass
        return action

    def _compute_action_impl(self, agent_id, full_obs_normalized,
                             full_obs, global_state):
        obs = full_obs_normalized.get(agent_id)
        if obs is None:
            self._behavior, self._source = 'stop', 'reflex'
            return [0.0, 0.0]

        # --- 1. Tagged → go to nearest untag corner ASAP ---
        if obs[IS_TAGGED] > 0:
            self._behavior, self._source = 'untag', 'reflex'
            return action_carry_to_corner(obs, global_state, agent_id)

        # --- 2. Wall danger → dodge ---
        dodge_action, dodge_weight = action_dodge_walls(obs)
        if dodge_weight > 10.0:
            self._behavior, self._source = 'wall_dodge', 'reflex'
            return dodge_action

        # --- 3. Carrying flag → rush to corner ---
        if obs[HAS_FLAG] > 0:
            # Dodge nearby opponents while carrying
            self._behavior, self._source = 'carry', 'reflex'
            action = action_carry_to_corner(obs, global_state, agent_id)
            action = _apply_collision_avoidance(obs, action)
            return action

        # --- 4. Strategic decision based on game state ---
        if isinstance(global_state, dict) and len(global_state) > 0:
            action = self._strategic_action(agent_id, obs, global_state)
            action = _apply_collision_avoidance(obs, action)
            return action

        # --- 5. Fallback: obs-only heuristic ---
        self._behavior, self._source = 'obs_only', 'auto'
        return self._obs_only_action(agent_id, obs)

    def _strategic_action(self, agent_id, obs, global_state):
        """Use global_state for full strategic decision-making."""
        gs = GameState(agent_id, global_state)
        self.step_count += 1

        # Determine team role index (0, 1, 2 within team)
        idx = int(agent_id.split('_')[1])
        role_idx = idx % 3

        # --- MANUAL OVERRIDE (MOOS var / control file / env) ---------------
        # This block is checked first in the strategic layer, so a manual command
        # takes effect ahead of the automatic manager below -- including the
        # opening scout phase. The reflexes upstream in compute_action (tagged ->
        # corner, wall-dodge, carrying-flag -> corner) run BEFORE this block is
        # ever reached, so those are not affected by posture/role commands; the
        # override only redirects the strategic decision.
        #
        # Order within the block: per-agent role is checked before team posture.
        # The practical effect is that when both are set for this agent, the role
        # is the one applied (get_role returns -> we dispatch and return here,
        # never consulting posture). See the PRECEDENCE note in the schema header.
        forced_role = self.posture_control.get_role(agent_id)
        if forced_role is not None:
            # Source 'role': a per-boat role override is driving this boat. Role
            # is checked first and returns immediately, so 'role' unambiguously
            # means a per-boat command (not a team posture).
            self._source = 'role'
            return self._dispatch_forced_role(forced_role, agent_id, obs, gs)

        forced_posture = self.posture_control.get_posture()
        if forced_posture is not None:
            # Source 'posture': a team-wide posture is driving this boat, and no
            # per-boat role overrides it (role was checked above and was None).
            self._source = 'posture'
            return self._dispatch_forced_posture(
                forced_posture, agent_id, obs, gs, role_idx)
        # Neither override present (both unset/auto) -> fall through to the
        # original automatic logic completely unchanged.
        self._source = 'auto'
        # --- end manual override -------------------------------------------

        # --- OPENING PHASE: wait near scrimmage, tag opponents first ---
        if not self.opening_done:
            intruders = gs.intruders()
            if len(intruders) > self.max_intruders_seen:
                self.max_intruders_seen = len(intruders)

            any_opp_tagged = any(gs.agent_is_tagged(a) for a in gs.opp_ids)
            any_score = gs.my_score > 0 or gs.opp_score > 0
            timeout = self.step_count > 300

            if any_opp_tagged or any_score or timeout:
                self.opening_done = True
                if self.max_intruders_seen >= 3:
                    self.opp_is_3A = True
            else:
                # If intruders exist — defend aggressively
                intruders = gs.intruders()
                if intruders:
                    return self._defend_action(agent_id, obs, gs)

                # No intruders yet — patrol near scrimmage
                self._behavior = 'opening_patrol'
                scrimmage_heading = float(np.clip(obs[SCRIM_BEARING] * 180, -60, 60))
                scrimmage_dist = obs[SCRIM_DIST]

                if scrimmage_dist > -0.5:
                    return [2.5, scrimmage_heading]
                else:
                    return [1.0, scrimmage_heading]

        # --- Detect opponent composition ---
        opp_on_their_side = 0
        for aid in gs.opp_ids:
            if gs.agent_on_side(aid) and not gs.agent_is_tagged(aid):
                opp_on_their_side += 1

        # --- OPPORTUNISTIC RUSH: when 2+ opponents are tagged, all rush ---
        opp_tagged = sum(1 for a in gs.opp_ids if gs.agent_is_tagged(a))
        if opp_tagged >= 2:
            # Most opponents recovering — rush the flag!
            self._behavior = 'attack'
            flag_heading = float(np.clip(obs[OPP_HOME_BEARING] * 180, -60, 60))
            offsets = [-15, 0, 15]
            biased = float(np.clip(flag_heading + offsets[role_idx], -60, 60))
            return action_go_to_flag_with_bias(obs, biased)

        # --- Priority: intercept flag carrier (always) ---
        carrier = gs.flag_carrier()
        if carrier is not None:
            carrier_pos = gs.agent_pos(carrier)
            my_pos = gs.agent_pos(agent_id)
            my_heading = gs.agent_heading(agent_id)

            # Closest untagged agent intercepts
            team_dists = []
            for aid in gs.team_ids:
                if not gs.agent_is_tagged(aid) and not gs.agent_has_flag(aid) and not gs.agent_is_disabled(aid):
                    d = _dist(gs.agent_pos(aid), carrier_pos)
                    team_dists.append((d, aid))
            team_dists.sort()
            if team_dists and team_dists[0][1] == agent_id:
                self._behavior = 'chase'
                return apollonius_heading(my_pos, my_heading, carrier_pos)

        # --- SCORE-BASED STRATEGY ---
        score_diff = gs.score_diff

        # === WINNING BIG (2+): ALL DEFEND ===
        if score_diff >= 2:
            return self._defend_action(agent_id, obs, gs)

        # === WINNING BY 1: 1 attacker + 2 defend ===
        if score_diff >= 1:
            # Furthest from our flag attacks, others defend
            flag_home = gs.my_flag_home
            team_by_flag_dist = []
            for aid in gs.team_ids:
                if not gs.agent_is_tagged(aid) and not gs.agent_has_flag(aid) and not gs.agent_is_disabled(aid):
                    d = _dist(gs.agent_pos(aid), flag_home)
                    team_by_flag_dist.append((d, aid))
            team_by_flag_dist.sort(reverse=True)  # furthest first
            if team_by_flag_dist and team_by_flag_dist[0][1] == agent_id:
                return self._attack_action(agent_id, obs, gs)
            return self._defend_action(agent_id, obs, gs)

        # === TIED OR LOSING: DYNAMIC ROLE ASSIGNMENT ===
        intruders = gs.intruders()
        my_pos = gs.agent_pos(agent_id)

        if not intruders:
            return self._attack_action(agent_id, obs, gs)

        # Build list of available teammates (untagged, no flag, not disabled)
        available = []
        for aid in gs.team_ids:
            if not gs.agent_is_tagged(aid) and not gs.agent_has_flag(aid) and not gs.agent_is_disabled(aid):
                available.append(aid)

        # vs 3A: cap at 2 defenders, always keep 1 attacker rushing open flag
        # vs others: match all intruders (1-2 max anyway)
        max_defenders = 2 if self.opp_is_3A else len(intruders)
        intruders_to_cover = intruders[:max_defenders]

        assigned_defenders = set()
        for opp_aid in intruders_to_cover:
            opp_pos = gs.agent_pos(opp_aid)
            best_aid = None
            best_dist = float('inf')
            for aid in available:
                if aid in assigned_defenders:
                    continue
                d = _dist(gs.agent_pos(aid), opp_pos)
                if d < best_dist:
                    best_dist = d
                    best_aid = aid
            if best_aid is not None:
                assigned_defenders.add(best_aid)

        # Am I assigned to defend?
        if agent_id in assigned_defenders:
            return self._defend_action(agent_id, obs, gs)
        else:
            return self._attack_action(agent_id, obs, gs)

    def _dispatch_forced_role(self, role, agent_id, obs, gs):
        """
        Execute a manually commanded per-agent role.

        WHY this reuses the existing behaviors (_attack_action, _defend_action,
        apollonius_heading, action_escort, action_defend_patrol) rather than
        implementing fresh movement: a forced role runs the exact same code path
        as the automatic version of that role, so there is one implementation of
        each behavior to maintain and a forced role moves identically to how the
        manager would have run it.

        Note the reflexes (tagged / carrying-flag / wall-dodge) are already
        handled upstream in compute_action and have returned before we get here,
        so this method only ever maps a STRATEGIC role to a behavior.
        """
        if role == "attack":
            return self._attack_action(agent_id, obs, gs)

        if role == "defend":
            return self._defend_action(agent_id, obs, gs)

        if role == "chase":
            # "chase" hunts a specific enemy. Target selection, in order:
            #   1. the enemy carrying our flag, if any,
            #   2. else the nearest untagged intruder on our side,
            #   3. else there is no one to chase -> fall back to _defend_action.
            # apollonius_heading is the same pursuit primitive the automatic
            # carrier-intercept path uses (see _strategic_action), so a forced
            # chase and the built-in intercept produce the same pursuit.
            my_pos = gs.agent_pos(agent_id)
            my_heading = gs.agent_heading(agent_id)
            target = gs.flag_carrier()
            if target is None:
                intr = gs.intruders()
                if intr:
                    target = min(
                        intr, key=lambda a: _dist(my_pos, gs.agent_pos(a)))
            if target is not None:
                self._behavior = 'chase'
                return apollonius_heading(
                    my_pos, my_heading, gs.agent_pos(target))
            return self._defend_action(agent_id, obs, gs)

        if role == "escort":
            # "escort" shadows a teammate who is carrying the enemy flag. If no
            # teammate is carrying, there is no one to escort -> fall back to
            # _attack_action.
            carrier_tm = gs.teammate_with_flag()
            if carrier_tm is not None and carrier_tm != agent_id:
                # action_escort reads the teammate out of the ego-centric obs
                # vector, which only contains the TWO *other* teammates (the obs
                # never lists the agent itself). So we must translate the
                # carrier's team-roster index into its slot among those two
                # blocks. WHY the "-1 if past my own index" shuffle: removing
                # myself from the roster shifts everyone after me down by one.
                # And WHY the "< 2" guard: bounds-check in case the roster/obs
                # layout assumption ever breaks, so we fall back to attacking
                # instead of indexing past the obs block.
                for i, aid in enumerate(gs.team_ids):
                    if aid == carrier_tm and aid != agent_id:
                        tm_idx = gs.team_ids.index(aid)
                        if tm_idx > gs.team_ids.index(agent_id):
                            tm_idx -= 1
                        if tm_idx < 2:
                            self._behavior = 'escort'
                            return action_escort(obs, tm_idx)
                        break
            return self._attack_action(agent_id, obs, gs)

        if role == "patrol":
            # Routes to action_defend_patrol: hold near our own flag (slow near
            # the flag, drive home if far). Distinct from "defend", which routes
            # to _defend_action and actively assigns/chases a target opponent.
            self._behavior = 'patrol'
            return action_defend_patrol(obs)

        # Reached only if a value passed the whitelist but has no branch above
        # (e.g. a value added to VALID_ROLES without a handler here). Routes to
        # _attack_action so the agent keeps moving rather than returning nothing.
        return self._attack_action(agent_id, obs, gs)

    def _dispatch_forced_posture(self, posture, agent_id, obs, gs, role_idx):
        """
        Execute a manually commanded TEAM posture.

        WHY each agent maps the shared posture independently (using its own
        role_idx) instead of a central coordinator handing out roles: every
        agent already runs the same code with the same global_state, so if they
        all apply the same deterministic rule to the same posture they reach the
        same split with no inter-agent messaging -- the same
        "consensus-by-identical-computation" the original manager relies on.
        role_idx is the agent's 0/1/2 slot within its team, used as a fixed
        tiebreaker so the split is deterministic (in "balance", slot 0 always
        takes the defend branch, so the team can't accidentally field 0 or 3
        defenders from a nondeterministic choice).
        """
        if posture == "defend":
            # All three agents route to _defend_action.
            return self._defend_action(agent_id, obs, gs)

        if posture == "attack":
            # All three agents route to _attack_action.
            return self._attack_action(agent_id, obs, gs)

        if posture == "balance":
            # Splits the team: slot 0 -> _defend_action, slots 1 and 2 ->
            # _attack_action (1 defender, 2 attackers). The 0->defend mapping is
            # a fixed, arbitrary choice; what it guarantees is determinism, not a
            # claim about the split being correct. Change the condition here for
            # a different split (e.g. role_idx in (0, 1) for 2 defenders).
            if role_idx == 0:
                return self._defend_action(agent_id, obs, gs)
            return self._attack_action(agent_id, obs, gs)

        # Defensive default (see _dispatch_forced_role's tail for the same
        # reasoning): unreachable for whitelisted values, attack keeps us active.
        return self._attack_action(agent_id, obs, gs)

    def _attack_action(self, agent_id, obs, gs):
        """
        Attacker: adaptive strategy based on opponent defender count.
        
        - vs 0 defenders: straight rush, skip escort (scoring race)
        - vs 1 defender: use evasion + moderate split
        - vs 2+ defenders: narrow split, direct rush
        """
        self._behavior = 'attack'
        # Count opponent defenders (on their side, untagged)
        opp_defenders = 0
        for aid in gs.opp_ids:
            if gs.agent_on_side(aid) and not gs.agent_is_tagged(aid):
                opp_defenders += 1

        flag_heading = float(np.clip(obs[OPP_HOME_BEARING] * 180, -60, 60))

        # --- 0 defenders: straight rush, no escort (all-out scoring race) ---
        if opp_defenders == 0:
            return [MAX_SPEED, flag_heading]

        # --- Escort teammate with flag (only when opponent has defenders) ---
        carrier_teammate = gs.teammate_with_flag()
        if carrier_teammate is not None and carrier_teammate != agent_id:
            teammate_obs_idx = None
            for i, aid in enumerate(gs.team_ids):
                if aid == carrier_teammate and aid != agent_id:
                    teammate_obs_idx = gs.team_ids.index(aid)
                    if teammate_obs_idx > gs.team_ids.index(agent_id):
                        teammate_obs_idx -= 1
                    break
            if teammate_obs_idx is not None and teammate_obs_idx < 2:
                self._behavior = 'escort'
                return action_escort(obs, teammate_obs_idx)

        # --- 1 defender: use evasion + moderate split ---
        if opp_defenders == 1:
            idx = int(agent_id.split('_')[1])
            role_idx = idx % 3
            if role_idx == 0:
                biased = float(np.clip(flag_heading - 15, -60, 60))
            else:
                biased = float(np.clip(flag_heading + 15, -60, 60))
            return action_go_to_flag_with_bias(obs, biased)

        # --- 2+ defenders: narrow split, direct rush ---
        idx = int(agent_id.split('_')[1])
        role_idx = idx % 3
        if role_idx == 0:
            biased = float(np.clip(flag_heading - 8, -60, 60))
        else:
            biased = float(np.clip(flag_heading + 8, -60, 60))

        return [MAX_SPEED, biased]

    def _defend_action(self, agent_id, obs, gs):
        """
        Defender with smart target assignment.
        - Each defender locks onto a specific opponent (no double-chasing)
        - Prioritizes: flag carrier > intruders near flag > approaching opponents
        - When no intruders, patrol near flag or position at scrimmage
        """
        self._behavior = 'defend'
        my_pos = gs.agent_pos(agent_id)
        my_heading = gs.agent_heading(agent_id)
        flag_home = gs.my_flag_home

        # Collect all active (untagged) opponents as potential targets
        active_opps = []
        for aid in gs.opp_ids:
            if not gs.agent_is_tagged(aid):
                active_opps.append(aid)

        if not active_opps:
            return action_defend_patrol(obs)

        # Collect teammate defenders (on our side, not tagged, not carrying)
        my_team_defenders = []
        for aid in gs.team_ids:
            if aid == agent_id:
                continue
            if gs.agent_is_tagged(aid) or gs.agent_has_flag(aid):
                continue
            if gs.agent_on_side(aid):
                my_team_defenders.append(aid)

        # Sort opponents by threat level:
        # 1. Has our flag (highest threat)
        # 2. On our side (intruding), sorted by distance to our flag
        # 3. On their side, sorted by distance to scrimmage
        def threat_score(aid):
            if gs.agent_has_flag(aid):
                return -1000  # highest priority
            if not gs.agent_on_side(aid):  # intruding (on our side)
                return _dist(gs.agent_pos(aid), flag_home)
            else:  # on their side
                return 500 + _dist(gs.agent_pos(aid), flag_home)

        opps_by_threat = sorted(active_opps, key=threat_score)

        # Assign: I take the highest-threat opponent where I'm the closest defender
        my_target = None
        my_target_dist = float('inf')

        for opp_aid in opps_by_threat:
            opp_pos = gs.agent_pos(opp_aid)
            my_dist = _dist(my_pos, opp_pos)

            teammate_closer = False
            for tm_aid in my_team_defenders:
                if _dist(gs.agent_pos(tm_aid), opp_pos) < my_dist:
                    teammate_closer = True
                    break

            if not teammate_closer:
                my_target = opp_aid
                my_target_dist = my_dist
                break  # take the highest-threat one I'm closest to

        # Fallback: take the highest-threat opponent regardless
        if my_target is None:
            my_target = opps_by_threat[0]
            my_target_dist = _dist(my_pos, gs.agent_pos(my_target))

        opp_pos = gs.agent_pos(my_target)
        is_intruding = not gs.agent_on_side(my_target)

        if is_intruding:
            # Opponent on our side — chase with Apollonius
            opp_to_flag = _dist(opp_pos, flag_home)
            if my_target_dist < opp_to_flag + 10:
                return apollonius_heading(my_pos, my_heading, opp_pos)
            else:
                # Position between intruder and flag
                mid_x = (opp_pos[0] + flag_home[0]) / 2
                mid_y = (opp_pos[1] + flag_home[1]) / 2
                abs_bearing = _angle_between(my_pos, [mid_x, mid_y])
                rel = _rel_heading(my_heading, abs_bearing)
                return [MAX_SPEED, float(np.clip(rel, -90.0, 90.0))]
        else:
            # Opponent still on their side — patrol near our flag
            return action_defend_patrol(obs)

    def _combined_action(self, agent_id, obs, gs):
        """Combined/flex: attack-biased, defend only when truly outnumbered."""
        intruders = gs.intruders()
        if not intruders:
            return self._attack_action(agent_id, obs, gs)

        # Detect opponent composition: how many opponents are defending?
        opp_on_their_side = 0
        for aid in gs.opp_ids:
            if gs.agent_on_side(aid) and not gs.agent_is_tagged(aid):
                opp_on_their_side += 1

        # If opponent has 0 defenders (all-attack), defense is futile — outscore them
        if opp_on_their_side == 0:
            return self._attack_action(agent_id, obs, gs)

        # Count teammates already defending (on our side, not carrying, not tagged)
        active_defenders = 0
        for aid in gs.team_ids:
            if aid == agent_id:
                continue
            if gs.agent_is_tagged(aid) or gs.agent_has_flag(aid):
                continue
            if gs.agent_on_side(aid):
                active_defenders += 1

        # Defend only if intruders outnumber active defenders
        if len(intruders) > active_defenders:
            return self._defend_action(agent_id, obs, gs)

        return self._attack_action(agent_id, obs, gs)

    def _obs_only_action(self, agent_id, obs):
        """Fallback when global_state is not available. Uses obs only."""
        # Check for opponents carrying our flag (has_flag in opponent obs blocks)
        for i in range(3):  # 3 opponents
            blk = _other_agent(obs, 2 + i)  # opponents start at index 2
            if blk[4] > 0:  # has_flag
                return action_intercept_opponent(obs, 2 + i)

        # Check for intruders (opponents not on their side)
        for i in range(3):
            blk = _other_agent(obs, 2 + i)
            if blk[5] < 0 and blk[7] < 0:  # not on_side and not tagged
                return action_intercept_opponent(obs, 2 + i)

        # Default: go to opponent flag
        return action_go_to_flag(obs)
