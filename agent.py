"""
Kestrel Talent - Battle Arena agent (team alpha_beta, Innov8 4.0 finals).

    export ARENA_URL=https://innov8-battle-arena.onrender.com
    export ARENA_KEY=arena_xxx            # never hard-coded
    export PENALTY_FACTOR=0.05            # announced at kick-off
    python agent.py

How it decides
--------------
Every credit costs PENALTY_FACTOR points, so every purchase must be expected to
return more points than it costs. The arena says points come from skill match,
assessment margin over the bar, recruiter notes, notice period and salary
headroom, and that a candidate below the bar earns nothing (see scoring.py).

  recon   (offers locked) - nothing can be taken yet, so information bought now
          cannot go stale. Build a verified offer queue per requisition:
          cheap wide search -> rank summaries by skill match -> batch-profile the
          best -> apply the bar locally -> verify the finalists with /assess.
  market  - fire the queue the moment offers open, best value first, across all
          requisitions at once; then keep refilling open slots the same way.
  closing - offers cost double; keep filling only open slots.
  stop    - no open slots, or no candidate left whose value beats its cost.

Verification is adaptive: /assess costs 25 credits, so the agent measures how
often profiles that look eligible fail verification, and keeps paying for it
only while the expected slot value it protects exceeds its price.

Robustness: every step is guarded, all knowledge is persisted to disk so a
crash-restart resumes where it stopped, and 409 / 429 / 5xx / empty searches /
full requisitions / exhausted credits are all handled without crashing.
"""
import json
import math
import os
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from arena_client import Arena, Exhausted, WrongPhase
import scoring as S

# ------------------------------------------------------------------ knobs
PENALTY_FACTOR = float(os.environ.get("PENALTY_FACTOR", "0.05"))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("AGENT_STATE", os.path.join(HERE, "state", "state.json"))
LOG_PATH = os.environ.get("AGENT_LOG", os.path.join(HERE, "logs", "agent.log"))

COST = {"search": 1, "profile": 2, "batch": 60, "assess": 25, "offer": 10, "offer_closing": 20, "release": 5}

WORKERS = 8                  # ~1.3 s round trip x 8 threads ~ 6 req/s, inside the 10 req/s limit
OFFER_WORKERS = 8            # the opening race: enough to use the burst of 30, not so many we just queue on 429s
QUEUE_BUFFER = 1.25          # verified candidates held ready per open slot (race losses, rejections)
FIRST_PASS_PAGES = 12        # 1,200 summaries per role; ~22% hold 5-6 of the 6 wanted skills
MIN_SKILL_TO_PROFILE = 0.5   # below 3 of 6 wanted skills a candidate is not worth 1.2 credits yet
RESERVE_CREDITS = 1500       # never spend information below this; offers may use it
RECON_BUDGET = 12000         # hard cap on recon spend: a bug can waste this much at most
SLOT_VALUE_PRIOR = 20.0      # points a good signing is assumed to earn, until the ledger tells us
VERIFY_SAMPLE = 25           # assessments taken before trusting the measured failure rate
RECON_STOP_BEFORE = 25       # seconds before 11:30 at which recon work stops and the race starts
RECON_QUIET_AFTER = 20       # if the market has not opened this long after 11:30, go back to work
CLOSED_POLL = 1.0            # seconds between free phase checks while the arena is closed or paused
MARKET_IDLE = 2.0            # seconds between free checks when every slot is full (a reset must be seen fast)
# Off by default: a live pilot showed the margin-only points model is wrong (10 swaps predicted
# +72.6 points, the ledger moved +11.0), and the agent's own check rejected it. Kept for the record.
SWAPS = os.environ.get("SWAPS", "0") == "1"   # replace weak signings with clearly better ones
SWAP_MIN_GAIN = 3.0          # predicted points a swap must add (its own cost is 0.75; the rest is safety)
SWAP_BUDGET = 8000           # hard cap on credits spent on upgrades after the market opens
PILOT_SWAPS = 10             # swaps allowed before the ledger has confirmed the points model
VALIDATE_MIN_GAIN = 15.0     # predicted gain inside one refresh window needed to judge the model
MIN_SWAP_SKILL = 5 / 6       # profile only summaries this close to the wanted skills when upgrading
UPGRADE_TRANCHE = 1000       # upgrade spend is judged in tranches of this many credits
MARKET_OPEN = tuple(int(x) for x in os.environ.get("MARKET_OPEN", "11:30").split(":"))  # "HH:MM"


# ------------------------------------------------------------------ logging
_log_lock = threading.Lock()


def is_transient(err):
    """The client raises 'gave up on ...' after retrying rate limits and 5xx: nothing was bought."""
    return "gave up" in str(err)


def log(*parts):
    line = time.strftime("%H:%M:%S") + " " + " ".join(str(p) for p in parts)
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# ------------------------------------------------------------------ persistent state
class State:
    """Everything the agent has learnt, saved to disk so a crash-restart resumes."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.summaries = {}       # cid -> summary record (role, name, city, skills text)
        self.profiles = {}        # cid -> raw /candidate record
        self.assessments = {}     # cid -> raw /assess record
        self.next_page = {}       # role -> next search page to read
        self.exhausted_roles = set()
        self.held = {}            # cid -> req_id
        self.held_people = set()  # person keys we hold (no signing the same person twice)
        self.dead = {}            # cid -> reason it must never be spent on again
        self.verify_stats = {"assessed": 0, "failed": 0}
        # Upgrade bookkeeping, saved so a restart never mistakes old swaps for new evidence:
        # log = [time, predicted gain] per swap; validated = None / True / False;
        # last_points = [points_as_of, points]; pending = [predicted, actual] not yet judged.
        self.swap = {"log": [], "validated": None, "last_points": None,
                     "pending": [0.0, 0.0], "start_credits": None}
        self._dirty = 0
        self._write_lock = threading.Lock()

    def save(self, force=False):
        with self.lock:
            self._dirty += 1
            if not force and self._dirty < 25:
                return
            self._dirty = 0
            # Serialise while holding the lock: other threads insert into these dicts.
            text = json.dumps({
                "summaries": self.summaries, "profiles": self.profiles, "assessments": self.assessments,
                "next_page": self.next_page, "exhausted_roles": sorted(self.exhausted_roles),
                "held": self.held, "held_people": sorted(self.held_people), "dead": self.dead,
                "verify_stats": self.verify_stats, "swap": self.swap,
            })
        with self._write_lock:                  # one writer at a time
            tmp = f"{self.path}.{threading.get_ident()}.tmp"
            try:
                with open(tmp, "w") as fh:
                    fh.write(text)
                os.replace(tmp, self.path)      # atomic: a crash mid-write never corrupts the file
            except OSError as e:
                log("state save failed:", e)

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as fh:
                b = json.load(fh)
            self.summaries = b.get("summaries", {})
            self.profiles = b.get("profiles", {})
            self.assessments = b.get("assessments", {})
            self.next_page = b.get("next_page", {})
            self.exhausted_roles = set(b.get("exhausted_roles", []))
            self.held = b.get("held", {})
            self.held_people = set(b.get("held_people", []))
            self.dead = b.get("dead", {})
            self.verify_stats = b.get("verify_stats", self.verify_stats)
            self.swap = {**self.swap, **b.get("swap", {})}
            log(f"resumed state: {len(self.summaries)} summaries, {len(self.profiles)} profiles, "
                f"{len(self.assessments)} assessments, {len(self.held)} held")
        except (OSError, ValueError) as e:
            log("state file unreadable, starting fresh:", e)


# ------------------------------------------------------------------ the agent
class Agent:
    def __init__(self, arena, state):
        self.arena = arena
        self.s = state
        self.reqs = {}                 # req_id -> requisition (authoritative filled/remaining from server)
        self.inflight = {}             # req_id -> offers currently in the air
        self.phase = "closed"
        self.seen_open = False
        self.out_of_credits = False
        self.slot_value = SLOT_VALUE_PRIOR
        self._parsed = {}              # cid -> parsed profile
        self._match = {}               # cid -> skill match of a summary against its role's requisition

    # ---------------------------------------------------------- server truth
    def refresh_requisitions(self):
        try:
            reqs = self.arena.requisitions()
        except Exception as e:
            log("requisitions refresh failed:", e)
            return
        if isinstance(reqs, list) and reqs:
            self.reqs = {r["req_id"]: r for r in reqs}
            self.reconcile()

    # Offer rejections that no longer hold once the arena is reset.
    _RESET_REVIVES = ("offer:already_signed", "offer:requisition_full", "offer:same_person_already_signed")

    def reconcile(self):
        """
        Make our record of who we hold agree with the server. Only we can release
        our own signings, so a requisition the server shows as less filled than we
        recorded means the arena was reset. Forget those holdings, and give the
        candidates we lost in earlier races another chance. Everything we learnt
        about candidates (profiles, verifications) stays valid and is kept.
        """
        reset = False
        with self.s.lock:
            for rid, r in self.reqs.items():
                mine = [c for c, v in list(self.s.held.items()) if v == rid]
                filled = r.get("filled")
                if isinstance(filled, int) and filled < len(mine):
                    for c in mine:
                        self.s.held.pop(c, None)
                    reset = True
                    log(f"reconcile {rid}: server shows {filled} filled, we recorded {len(mine)}; forgetting ours")
            if reset:
                self.s.held_people = {S.person_key(self._profile(c)) for c in self.s.held if self._profile(c)}
                revived = [c for c, why in list(self.s.dead.items()) if why in self._RESET_REVIVES]
                for c in revived:
                    del self.s.dead[c]
                log(f"reconcile: arena reset detected; revived {len(revived)} candidates lost to earlier offers")
        if reset:
            self.s.save(force=True)

    def refresh_ledger(self):
        led = self.arena.ledger()
        self.phase = led.get("phase", self.phase)
        self.check_model(led)
        if self.phase in ("recon", "market", "closing"):
            self.seen_open = True
        # Calibrate what a signing is worth once the ledger has scored some.
        signed, points = led.get("signed") or 0, led.get("points") or 0
        if isinstance(signed, int) and signed >= 5 and points > 0:
            self.slot_value = max(1.0, points / signed)
        return led

    def credits_left(self):
        c = self.arena.credits_remaining
        return 50000 if c is None else c

    def can_spend(self, credits, for_offer=False):
        if self.out_of_credits:
            return False
        if self.phase == "recon" and 50000 - self.credits_left() + credits > RECON_BUDGET:
            return False
        floor = 0 if for_offer else RESERVE_CREDITS
        return self.credits_left() - credits >= floor

    def open_slots(self, rid):
        r = self.reqs.get(rid)
        if not r:
            return 0
        held_here = sum(1 for v in list(self.s.held.values()) if v == rid)
        remaining = min(r.get("remaining", 0), r["headcount"] - held_here)
        return max(0, remaining - self.inflight.get(rid, 0))

    # ---------------------------------------------------------- knowledge queries
    def _role_of(self, rid):
        return self.reqs[rid]["role"]

    def _profile(self, cid):
        """Parsed profile, cached: parsing is the agent's main CPU cost on a 0.5 vCPU box."""
        hit = self._parsed.get(cid)
        if hit is not None:
            return hit
        raw = self.s.profiles.get(cid)
        if not raw:
            return None
        p = S.read_profile(raw)
        self._parsed[cid] = p
        return p

    def verified_queue(self, rid):
        """Candidates verified by /assess for this requisition, best first."""
        req, out = self.reqs[rid], []
        for cid, rec in list(self.s.assessments.items()):
            if cid in self.s.dead or cid in self.s.held:
                continue
            p = self._profile(cid)
            if not p or p["claimed"] or S.person_key(p) in self.s.held_people:
                continue
            ok, score, _ = S.assessment_passes(rec, req)
            if ok and not S.bar_failures(p, req, score):
                out.append((S.predicted_points(p, req, score), cid))
        out.sort(reverse=True)
        return out

    def trusted_queue(self, rid):
        """When verification has stopped paying, profiles that pass the bar are signed on their own."""
        req, out = self.reqs[rid], []
        for cid in list(self.s.profiles):
            if cid in self.s.dead or cid in self.s.held or cid in self.s.assessments:
                continue
            p = self._profile(cid)
            if p["claimed"] or S.person_key(p) in self.s.held_people or S.fabrication_risk(p):
                continue
            if not S.bar_failures(p, req):
                out.append((S.predicted_points(p, req, p["assessment"]), cid))
        out.sort(reverse=True)
        return out

    def ready_queue(self, rid):
        q = self.verified_queue(rid)
        if not self.verification_pays():
            q = sorted(q + self.trusted_queue(rid), reverse=True)
        return q

    def verification_pays(self):
        """
        Keep paying 25 credits per /assess while the expected loss it prevents is
        larger: P(an eligible-looking profile fails) x value of the slot it would waste.
        """
        st = self.s.verify_stats
        if st["assessed"] < VERIFY_SAMPLE:
            return True
        fail_rate = st["failed"] / max(1, st["assessed"])
        return fail_rate * (self.slot_value + COST["offer"] * PENALTY_FACTOR) > COST["assess"] * PENALTY_FACTOR

    def to_assess(self, rid, limit):
        """Profiles that pass the bar on their own numbers and look genuine, best first."""
        req, out = self.reqs[rid], []
        for cid in list(self.s.profiles):
            if cid in self.s.dead or cid in self.s.assessments or cid in self.s.held:
                continue
            p = self._profile(cid)
            if p["claimed"] or S.person_key(p) in self.s.held_people:
                continue
            if S.bar_failures(p, req):
                continue
            risk = S.fabrication_risk(p)
            out.append((S.predicted_points(p, req, p["assessment"]) - 2.0 * len(risk), cid))
        out.sort(reverse=True)
        return [cid for _, cid in out[:limit]]

    def upgrade_candidate(self, rid):
        """
        An unverified, already-profiled candidate worth 25 credits of verification because,
        if they pass, they displace the weakest person we would otherwise sign. The bar for
        'worth it' is the assessment's cost in points, spread over the chance they pass.
        """
        k = self.open_slots(rid)
        ready = self.ready_queue(rid)
        if k <= 0 or len(ready) < k:
            return None
        weakest_signed = ready[k - 1][0]
        margin = self.verify_cost_points()
        req = self.reqs[rid]
        for cid in self.to_assess(rid, 1):
            p = self._profile(cid)
            value = S.predicted_points(p, req, p["assessment"]) - 2.0 * len(S.fabrication_risk(p))
            if value > weakest_signed + margin:
                log(f"upgrade check {rid}: {cid} est {value:.3f} beats weakest planned signing "
                    f"{weakest_signed:.3f} by more than {margin:.3f}")
                return cid
        return None

    def to_profile(self, rid, limit):
        """Summaries of this role not yet profiled, by skill match."""
        req, role, out = self.reqs[rid], self._role_of(rid), []
        for cid, sm in list(self.s.summaries.items()):
            if cid in self.s.profiles or cid in self.s.dead:
                continue
            if str(sm.get("role", "")).lower() != role.lower():
                continue
            match = self._match.get(cid)
            if match is None:
                match = self._match[cid] = S.skill_match(S.P.parse_skills(sm.get("skills")), req)
            out.append((match, cid))
        out.sort(reverse=True)
        return [(m, cid) for m, cid in out[:limit]]

    # ---------------------------------------------------------- purchases
    def search_page(self, role):
        page = self.s.next_page.get(role, 0)
        if role in self.s.exhausted_roles or not self.can_spend(COST["search"]):
            return False
        log(f"BUY search {role} page {page} (1 cr): reading more of the pool for this role")
        res = self.arena.search(role=role, page=page, size=100)
        results = res.get("results") or []
        with self.s.lock:
            for r in results:
                if r.get("candidate_id"):
                    self.s.summaries[r["candidate_id"]] = r
            self.s.next_page[role] = page + 1
            total = res.get("total") or 0
            if not results or (page + 1) * 100 >= total:
                self.s.exhausted_roles.add(role)
                log(f"search {role}: pool exhausted at page {page} (total {total})")
        self.s.save()
        return bool(results)

    def fetch_profiles(self, ids):
        ids = [c for c in ids if c not in self.s.profiles]
        if not ids:
            return
        if len(ids) > 30:                                   # batch is 60 flat: cheaper than singles above 30
            if not self.can_spend(COST["batch"]):
                return
            log(f"BUY batch of {len(ids[:50])} profiles (60 cr): best unread skill matches, "
                f"cheaper than {len(ids[:50])} singles")
            res = self.arena.batch(ids[:50])
            recs = res.get("results") or res.get("candidates") or res.get("profiles") or \
                (res if isinstance(res, list) else [])
            if isinstance(res, dict) and not recs:
                recs = [v for v in res.values() if isinstance(v, dict) and v.get("candidate_id")]
        else:
            recs = []
            for cid in ids:
                if not self.can_spend(COST["profile"]):
                    break
                log(f"BUY profile {cid} (2 cr): fewer than 31 left to read, singles are cheaper")
                try:
                    recs.append(self.arena.candidate(cid))
                except RuntimeError as e:
                    if "404" in str(e):
                        self.s.dead[cid] = "not_found"
                    else:
                        raise
        with self.s.lock:
            for r in recs:
                if isinstance(r, dict) and r.get("candidate_id"):
                    self.s.profiles[r["candidate_id"]] = r
        self.s.save()

    def assess(self, cid, rid):
        if not self.can_spend(COST["assess"]):
            return
        p = self._profile(cid)
        log(f"BUY assess {cid} for {rid} (25 cr): passes the bar on its own numbers "
            f"(assessment {p['assessment'] if p else '?'}), verifying before any offer")
        try:
            rec = self.arena.assess(cid)
        except RuntimeError as e:
            if is_transient(e):                             # rate limited / 5xx: try again later
                log(f"assess {cid} deferred: {e}")
                return
            self.s.dead[cid] = f"assess_error:{str(e)[:40]}"   # 404 etc.: record it, never retry
            log(f"assess {cid} failed: {e}")
            return
        ok, score, why = S.assessment_passes(rec, self.reqs[rid])
        with self.s.lock:
            self.s.assessments[cid] = rec
            self.s.verify_stats["assessed"] += 1
            if not ok:
                self.s.verify_stats["failed"] += 1
                self.s.dead[cid] = f"assess:{why}"
        log(f"assess {cid} for {rid}: {why} (verified {score})")
        self.s.save()

    def offer(self, cid, rid):
        """Sign one candidate. Every outcome is recorded so no credit is spent on them twice."""
        cost = COST["offer_closing"] if self.phase == "closing" else COST["offer"]
        if not self.can_spend(cost, for_offer=True):
            return False
        p = self._profile(cid)
        log(f"BUY offer {cid} -> {rid} ({cost} cr): best available candidate for an open slot")
        try:
            res = self.arena.offer(cid, rid)
        except WrongPhase:
            return None                                    # offers not open yet: caller retries
        except RuntimeError as e:
            if is_transient(e):                             # rate limited / 5xx: stays in the queue
                log(f"offer {cid} -> {rid} deferred: {e}")
                return False
            self.s.dead[cid] = f"offer_error:{str(e)[:40]}"    # 404 / 422: never retry this one
            log(f"offer {cid} -> {rid} errored: {e}")
            return False
        with self.s.lock:
            if res.get("accepted"):
                self.s.held[cid] = rid
                if p:
                    self.s.held_people.add(S.person_key(p))
                log(f"SIGNED {cid} -> {rid}" + (" (already ours)" if res.get("already_yours") else ""))
                ok = True
            else:
                reason = res.get("reason", "rejected")
                self.s.dead[cid] = f"offer:{reason}"
                log(f"offer {cid} -> {rid} rejected: {reason}")
                if reason == "requisition_full" and rid in self.reqs:
                    self.reqs[rid]["remaining"] = 0
                ok = False
        self.s.save(force=True)
        return ok

    # ---------------------------------------------------------- one unit of work per requisition
    def advance(self, rid):
        """
        Move one requisition one step closer to a full, verified queue.
        Order: verify a finalist > profile the best unread summaries > read more of the pool.
        Returns False when there is nothing useful left to buy for it.
        """
        role = self._role_of(rid)
        need = math.ceil(self.open_slots(rid) * QUEUE_BUFFER) - len(self.ready_queue(rid))
        if need <= 0:
            # The queue is long enough, but it may not be good enough: verify an already
            # profiled candidate if they would beat the weakest person we plan to sign.
            cid = self.upgrade_candidate(rid) if self.verification_pays() else None
            if cid:
                self.assess(cid, rid)
                return True
            return False
        if self.verification_pays():
            for cid in self.to_assess(rid, 1):
                self.assess(cid, rid)
                return True
        scanned = self.s.next_page.get(role, 0)
        if scanned < FIRST_PASS_PAGES and role not in self.s.exhausted_roles:
            return self.search_page(role)
        batch = self.to_profile(rid, 50)
        good = [cid for m, cid in batch if m >= MIN_SKILL_TO_PROFILE]
        if len(good) >= 30 or (good and role in self.s.exhausted_roles):
            self.fetch_profiles(good)
            return True
        if role not in self.s.exhausted_roles:
            return self.search_page(role)
        if batch:                                           # pool read in full: take weaker matches too
            self.fetch_profiles([cid for _, cid in batch])
            return True
        return False

    def work_round(self, pool):
        """Advance every requisition that still needs candidates, in parallel."""
        rids = [rid for rid in self.reqs if self.open_slots(rid) > 0]
        if not rids:
            return False
        futures = [pool.submit(self._guarded, self.advance, rid) for rid in rids]
        # Wait for EVERY requisition before the next round. any() over a generator would
        # return at the first success and let a second worker start on a requisition whose
        # first is still mid-purchase - both would then buy the same candidate.
        results = [f.result() for f in futures]
        return any(results)

    def _guarded(self, fn, *args):
        try:
            return fn(*args)
        except Exhausted:
            self.out_of_credits = True
            log("credits exhausted: only free calls from now on")
        except WrongPhase as e:
            log("wrong phase:", e)
        except Exception as e:                              # never let one bad call kill the agent
            log(f"step {getattr(fn, '__name__', fn)} failed: {e}")
        return False

    # ---------------------------------------------------------- signing
    def sign_from_queues(self, pool):
        """
        Offer the best candidates across every requisition at once, best value first.
        Slots are reserved before the call so parallel offers never overfill a requisition.
        """
        jobs = []
        for rid in self.reqs:
            for value, cid in self.ready_queue(rid):
                jobs.append((value, cid, rid))
        jobs.sort(reverse=True)
        lock = threading.Lock()
        signed = [0]
        people_in_air = set()

        def take():
            with lock:
                while jobs:
                    value, cid, rid = jobs.pop(0)
                    if cid in self.s.held or cid in self.s.dead or self.open_slots(rid) <= 0:
                        continue
                    p = self._profile(cid)
                    who = S.person_key(p) if p else cid
                    if who in self.s.held_people or who in people_in_air:
                        continue                        # same person under another id: skip, don't pay
                    people_in_air.add(who)
                    self.inflight[rid] = self.inflight.get(rid, 0) + 1
                    return cid, rid, who
            return None

        def worker():
            while not self.out_of_credits:
                job = take()
                if not job:
                    return
                cid, rid, who = job
                try:
                    if self._guarded(self.offer, cid, rid):
                        signed[0] += 1
                finally:
                    with lock:
                        self.inflight[rid] -= 1
                        people_in_air.discard(who)

        futures = [pool.submit(worker) for _ in range(OFFER_WORKERS)]
        for f in futures:
            f.result()
        return signed[0]

    # ---------------------------------------------------------- phases
    # ---------------------------------------------------------- upgrades after the slots are full
    def verify_cost_points(self):
        """What one successful verification costs in points, given how often candidates fail it."""
        st = self.s.verify_stats
        pass_rate = max(0.05, 1.0 - st["failed"] / max(1, st["assessed"]))
        return COST["assess"] * PENALTY_FACTOR / pass_rate

    def value_of(self, cid, rid):
        """Predicted points of a candidate we have verified, for this requisition."""
        p, rec = self._profile(cid), self.s.assessments.get(cid)
        if not p or not rec:
            return 0.0
        ok, score, _ = S.assessment_passes(rec, self.reqs[rid])
        return S.predicted_points(p, self.reqs[rid], score) if ok else 0.0

    def held_by_value(self, rid):
        """Our signings in this requisition, weakest first."""
        return sorted((self.value_of(c, rid), c) for c, r in list(self.s.held.items()) if r == rid)

    def check_model(self, led):
        """
        Judge the points model against the ledger. Points refresh every 15 minutes and
        only our own swaps change our holdings, so the change between two refreshes is
        the true value of the swaps made in between. If reality delivers under 30% of
        what the model promised, the model is wrong and swapping stops. Evidence adds up
        across windows until there is enough to judge, so a pilot split over two refreshes
        still gets a verdict.
        """
        sw = self.s.swap
        as_of, points = led.get("points_as_of"), led.get("points")
        if not isinstance(as_of, (int, float)) or not isinstance(points, (int, float)):
            return
        if sw["last_points"] is None:
            sw["last_points"] = [as_of, points]
            return
        prev_as_of, prev_points = sw["last_points"]
        if as_of <= prev_as_of:
            return
        sw["last_points"] = [as_of, points]
        sw["pending"][0] += sum(g for t, g in sw["log"] if prev_as_of < t <= as_of)
        sw["pending"][1] += points - prev_points
        predicted, actual = sw["pending"]
        if predicted < VALIDATE_MIN_GAIN:
            return
        sw["pending"] = [0.0, 0.0]
        ratio = actual / predicted
        # Far more than predicted is as suspicious as far less: something other than our
        # swaps moved the ledger, so it cannot confirm the model.
        verdict = ("confirmed" if 0.6 <= ratio <= 2.0 else
                   "rejected" if ratio < 0.3 else "unclear")
        log(f"MODEL CHECK: swaps predicted {predicted:+.1f} points, ledger moved {actual:+.1f} "
            f"(ratio {ratio:.2f}) -> {verdict}")
        if verdict == "confirmed":
            sw["validated"] = True
        elif verdict == "rejected":
            sw["validated"] = False
        else:                                               # not enough signal: allow one more pilot
            sw["pilot_limit"] = len(sw["log"]) + PILOT_SWAPS
        self.s.save(force=True)

    def swaps_allowed(self):
        sw = self.s.swap
        if not SWAPS or self.phase != "market" or sw["validated"] is False:
            return False
        if sw["validated"] is None and len(sw["log"]) >= sw.get("pilot_limit", PILOT_SWAPS):
            return False                                    # wait for the ledger to judge the pilot
        if sw.get("stopped"):
            return False
        used = 50000 - self.credits_left()
        if sw["start_credits"] is None:
            sw["start_credits"] = used
        if used - sw["start_credits"] >= SWAP_BUDGET:
            return False
        # Stop when upgrading stops paying: each tranche of spend must buy 1.5x its cost in points.
        gained = sum(g for _, g in sw["log"])
        t_start, g_start = sw.setdefault("tranche", [used, gained])
        if used - t_start >= UPGRADE_TRANCHE:
            if gained - g_start < 1.5 * UPGRADE_TRANCHE * PENALTY_FACTOR:
                log(f"upgrades stopped: the last {used - t_start} credits bought "
                    f"only {gained - g_start:.1f} predicted points")
                sw["stopped"] = True
                self.s.save(force=True)
                return False
            sw["tranche"] = [used, gained]
        return True

    def verified_replacements(self, rid, exclude=()):
        return [(v, c) for v, c in self.verified_queue(rid) if c not in exclude]

    def swap(self, old, new, rid, old_value, new_value):
        """
        Release our weakest signing and sign a better verified candidate. A full
        requisition refuses offers (and still charges), so the release must come first.
        If the new candidate is taken in that gap, sign the next best that still beats
        the old one; failing that, take the released candidate back.
        """
        if not self.can_spend(COST["profile"] + COST["release"] + COST["offer"], for_offer=True):
            return False
        # Rivals keep signing all day, and our verification may be hours old: confirm the
        # replacement is still free (2 credits) before giving up a signing for them. A stale
        # candidate then costs 2 credits instead of a release, a refused offer and a re-sign.
        try:
            fresh = self.arena.candidate(new)
        except RuntimeError as e:
            log(f"swap {rid}: could not re-check {new}, keeping {old}: {e}")
            return False
        with self.s.lock:
            self.s.profiles[new] = fresh
            self._parsed.pop(new, None)
            if fresh.get("claimed"):
                self.s.dead[new] = "offer:already_signed"
        if fresh.get("claimed"):
            log(f"swap {rid}: {new} was signed by a rival since we verified them; keeping {old}")
            return True                                     # progress: one stale candidate cleared
        log(f"SWAP {rid}: release {old} ({old_value:.1f} pts) for {new} ({new_value:.1f} pts, "
            f"+{new_value - old_value:.1f} predicted)")
        try:
            self.arena.release(old)
        except RuntimeError as e:
            log(f"release {old} failed, swap abandoned: {e}")
            return False
        with self.s.lock:
            self.s.held.pop(old, None)
            p_old = self._profile(old)
            if p_old:
                self.s.held_people.discard(S.person_key(p_old))
            self.s.dead[old] = "released"
        self.s.save(force=True)
        if self.offer(new, rid):
            self.s.swap["log"].append((time.time(), new_value - old_value))
            return True
        # Taken in the second between our check and our offer: take the released candidate
        # back at once rather than gamble on other, possibly stale, replacements.
        self.s.dead.pop(old, None)
        log(f"swap {rid}: {new} was taken in the gap, taking {old} back")
        self.offer(old, rid)
        return True

    def upgrade_step(self, rid):
        """
        One unit of upgrade work for a full requisition: swap in a verified candidate
        who clearly beats our weakest signing; else verify a profiled candidate whose own
        numbers promise that; else read and profile more of the pool.
        """
        if self.open_slots(rid) > 0 or not self.swaps_allowed():
            return False
        held = self.held_by_value(rid)
        if not held:
            return False
        weakest_value, weakest = held[0]
        for value, cid in self.verified_replacements(rid):
            if value - weakest_value < SWAP_MIN_GAIN:
                break
            return self.swap(weakest, cid, rid, weakest_value, value)
        need = weakest_value + SWAP_MIN_GAIN + self.verify_cost_points()
        req, best = self.reqs[rid], None
        for cid in self.to_assess(rid, 5):
            p = self._profile(cid)
            v = S.predicted_points(p, req, p["assessment"])
            if v >= need and (best is None or v > best[0]):
                best = (v, cid)
        if best:
            self.assess(best[1], rid)
            return True
        role = self._role_of(rid)
        batch = [cid for m, cid in self.to_profile(rid, 50) if m >= MIN_SWAP_SKILL]
        if len(batch) >= 30 or (batch and role in self.s.exhausted_roles):
            self.fetch_profiles(batch)
            return True
        if role not in self.s.exhausted_roles:
            return self.search_page(role)
        return False

    def upgrade_round(self, pool):
        rids = [rid for rid in self.reqs if self.open_slots(rid) == 0]
        if not rids or not self.swaps_allowed():
            return False
        futures = [pool.submit(self._guarded, self.upgrade_step, rid) for rid in rids]
        return any([f.result() for f in futures])

    def market_open_ts(self):
        """Local clock time the market is scheduled to open. Only used to stop recon work
        early; the actual switch to signing waits for the API to report the market phase."""
        now = time.localtime()
        return time.mktime(now[:3] + (MARKET_OPEN[0], MARKET_OPEN[1], 0) + now[6:])

    def run_recon(self, pool):
        """Build verified queues until just before the market opens."""
        open_at = self.market_open_ts()
        while self.phase == "recon":
            now = time.time()
            if open_at - RECON_STOP_BEFORE <= now <= open_at + RECON_QUIET_AFTER:
                time.sleep(0.4)                             # the scheduled opening: poll, start no work
                self._poll_phase()
                continue
            progressed = self.work_round(pool)
            self._poll_phase()
            if not progressed:
                self.s.save(force=True)                     # idle: make sure everything is on disk
                # Past the scheduled opening the market can flip any second: poll tightly.
                time.sleep(0.4 if now > open_at else 5)
        self.s.save(force=True)
        log(f"recon over: phase is now {self.phase}")

    def _poll_phase(self):
        try:
            self.refresh_ledger()
        except Exception as e:
            log("ledger poll failed:", e)

    def run_open_market(self, pool):
        """Keep open slots filled: sign what is ready, then buy more knowledge where slots are empty."""
        n = self.sign_from_queues(pool)          # our own holdings decide open slots: sign first
        if n:
            log(f"signed {n} this round")
        self.refresh_requisitions()
        progressed = self.work_round(pool)
        progressed = self.upgrade_round(pool) or progressed
        if not progressed:
            time.sleep(MARKET_IDLE)                         # nothing to buy: idle on free calls only
        self.refresh_ledger()

    def report(self, label):
        try:
            led = self.arena.ledger()
        except Exception:
            led = {}
        per_req = {rid: f"{len(self.ready_queue(rid))}q/{self.open_slots(rid)}open" for rid in self.reqs}
        log(f"[{label}] credits_used={led.get('credits_used')} signed={led.get('signed')} "
            f"points={led.get('points')} score={led.get('score')} verify={self.s.verify_stats} "
            f"slot_value={self.slot_value:.1f} queues={per_req}")

    def run(self):
        log(f"agent starting; PENALTY_FACTOR={PENALTY_FACTOR}; market scheduled {MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d}")
        last_report, last_phase, closed_reported = 0, None, False
        with ThreadPoolExecutor(max_workers=max(WORKERS, OFFER_WORKERS)) as pool:
            while True:
                try:
                    self.refresh_ledger()
                    if self.phase != last_phase or not self.reqs:
                        log(f"phase {last_phase} -> {self.phase}")
                        self.refresh_requisitions()            # also reconciles holdings after a reset
                        last_phase = self.phase
                    if self.phase == "closed":
                        # A pause and the final whistle look the same, so never exit on our own:
                        # hold what we have and resume if the arena reopens. We are stopped at the end.
                        if self.seen_open and not closed_reported:
                            self.report("arena closed; holding, will resume if it reopens")
                            closed_reported = True
                        time.sleep(CLOSED_POLL)                 # it may reopen straight into the market
                        continue
                    closed_reported = False
                    if self.out_of_credits:
                        time.sleep(30)
                        continue
                    if self.phase == "recon":
                        self.run_recon(pool)
                        last_report = time.time()            # no status call in the middle of the race
                        if self.phase in ("market", "closing"):
                            self.run_open_market(pool)       # race the instant offers open
                    elif self.phase in ("market", "closing"):
                        self.run_open_market(pool)
                    if time.time() - last_report > 300:
                        self.report(self.phase)
                        last_report = time.time()
                except Exhausted:
                    self.out_of_credits = True
                    log("credits exhausted: holding what we have")
                except Exception as e:
                    log("main loop error (continuing):", e)
                    log(traceback.format_exc().splitlines()[-1])
                    time.sleep(3)
        self.s.save(force=True)
        self.report("final")


def main():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    state = State(STATE_PATH)
    state.load()

    def on_stop(signum, _frame):                          # killed at the whistle or by a redeploy
        log(f"signal {signum}: saving state and stopping")
        state.save(force=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    Agent(Arena(), state).run()


if __name__ == "__main__":
    main()
