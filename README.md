# Kestrel Talent — an autonomous hiring agent for a live market

Built for the **Innov8 4.0 Battle Arena** grand finale (Eightfold.ai × ARIES, IIT Delhi), 28 September 2026.
Fifty-one autonomous agents competed in one shared market for six hours. No human could touch an agent once
the arena opened.

| | |
| --- | --- |
| **Final standing** | Rank 1 of 3,100+ registered teams (70% arena score + 30% judged code, edge cases, logic, readability) |
| **Arena leaderboard at the close** | 2nd of 51 finalists — 1,985 points, 9,876 credits, **score 1,491.2** |
| **Slots filled** | **116 of 116**, every one verified before the offer |
| **Speed** | 115 slots signed within **10 seconds** of offers opening |
| **Budget used** | 9,876 of 50,000 credits — **20%** |
| **Crashes** | 0, across 6 hours and 9 live redeploys |

> **The arena is closed**, so this code can no longer be run against it. What it did is recorded
> purchase-by-purchase in [`logs/agent.log`](logs/agent.log) — 1,180 lines, every credit spent with the
> reason it was spent. That file is the most interesting thing in this repository.

---

## The problem

- **1,000,000 candidate profiles**, shared by every team. A candidate signed by a rival is gone for good.
- **10 requisitions, 116 slots.** Each has a bar — role, minimum assessment score, maximum notice period,
  salary budget. A signing below the bar scores **zero** and keeps the slot occupied.
- **50,000 credits, hard-enforced.** Every API call costs: search 1, profile 2, batch of 50 profiles 60,
  verified assessment 25, offer 10 (20 in the closing hour), release 5.
- **Arena score = points held at the final whistle − (credits used × penalty factor).** The penalty factor
  was announced only at kick-off, so it had to be a runtime parameter, not an assumption in the code.
- Profiles are messy: mixed formats, missing fields, duplicates under several IDs, and **fabricated
  identities**. Signing a fake is worse than not signing at all.

## The one rule

> **Buy an action only if its expected points exceed its credits × penalty factor.**

At the announced factor of 0.05, that converts every price into points:

| Action | Credits | Cost in points |
| --- | ---: | ---: |
| Search — 100 summaries | 1 | 0.05 |
| One full profile | 2 | 0.10 |
| Batch of 50 profiles | 60 | 3.00 (0.06 each) |
| Verified assessment | 25 | 1.25 |
| Offer | 10 | 0.50 |

A good signing was measured at **17.1 points**. Against that, reading the pool is almost free and
verification is cheap insurance — which is what the whole design follows from. Every threshold in the code
derives from `PENALTY_FACTOR`; none is hard-coded.

---

## Quick tour of the code

1,314 lines, five files, **Python standard library only** — no third-party packages, because the sandbox
was 0.5 vCPU / 1 GB / ARM64 and nothing should fail at import.

| File | Lines | What it holds |
| --- | ---: | --- |
| [`agent.py`](agent.py) | 884 | The decision loop: phase machine, per-requisition buying pipeline, the market-opening race, persistence, reconciliation |
| [`scoring.py`](scoring.py) | 180 | Value model (five points terms), the requisition bar, fabrication-risk flags, identity key for de-duplication |
| [`parsing.py`](parsing.py) | 145 | Parsers for messy fields: `63/100`, `0.85`, `2260000`, `11.0 LPA`, `$22,369`, `Immediate`, skill aliases |
| [`arena_client.py`](arena_client.py) | 87 | **The organisers' starter-kit client, unmodified** — auth, retries, rate-limit back-off, credit accounting |
| [`run_agent.sh`](run_agent.sh) | 18 | Supervisor: reads the key from the environment, restarts at most twice |

```
run_agent.sh ──> agent.py ──> arena_client.py ──> Arena API
                    │                              (10 req/s per team)
      ┌─────────────┼─────────────┐
   scoring.py   parsing.py   state.json + agent.log
```

The split is deliberate: `agent.py` decides **what to buy**, `scoring.py` decides **who is good**, and
`parsing.py` turns the API's strings into numbers. Anyone looking for "why was this candidate signed" goes
to `scoring.py`; "why was this bought" goes to `agent.py`.

## How it decides

**Recon (offers locked).** Nothing can be taken from you in this hour, so information bought now cannot go
stale — two thirds of the budget went here. `advance()` runs one step per requisition per round, taking the
first action that applies:

1. Verify a finalist with `/assess` — closest to a signing
2. Else batch-profile the best unread summaries — 50 at a time, 1.2 credits each instead of 2
3. Else read another page of search — 100 summaries for 1 credit

Search is filtered server-side by role, because a role mismatch is an automatic bar failure: every record on
a page is someone who could actually fill that slot. Summaries carry only skills, so candidates are ranked
by skill match and anyone below 3 of the 6 wanted skills is never paid for. The queue stops at **1.25
verified candidates per open slot** — enough spare to absorb names lost to rivals.

**The race.** The moment offers opened, `sign_from_queues()` fired every queued candidate best-value-first
across all ten requisitions at once, 8 workers deep. Each call reserves the slot *and* the person under a
mutex before paying, so no requisition overfilled and nobody was offered to twice under two IDs. 129 offers,
116 accepted, 11 lost to rivals who signed first — all backfilled from the buffer.

**Stopping.** With every slot full, each further purchase had to beat its own cost. After one controlled
experiment (below) the agent spent **nothing** from 12:30 to the whistle, including the closing hour when
offers cost double.

## Why verification was worth 25 credits a time

The measurement that justified the largest line of spending. Of **192 candidates verified during recon**:

| Outcome | Count | Share |
| --- | ---: | ---: |
| Passed — identity, reference and score all verified | 150 | 78% |
| Fabricated identity (`identity_not_verified`) | 25 | 13% |
| Real person, verified score below the bar | 17 | 9% |

**22% of profiles that looked eligible would have scored zero.** A check costs 1.25 points; skipping one
risks 22% × 17 ≈ 3.7 points. So verification returns about three times its price — and the code does not
assume this: after the first 25 checks it uses the *measured* failure rate and would stop paying if the rate
ever dropped far enough (`verification_pays()`).

Free signals rank suspicious profiles lower before any money is spent: two or more templated praise clauses
in one note, an expected CTC more than 3× current, impossible values. Duplicates are caught with an identity
key of `name | city | rounded years of experience`, since the pool has no phone or email.

Result: **17.1 points per signing**, against roughly 13 for teams that signed at face value.

## The experiment the agent stopped itself

After the first ledger refresh, the sum of verified assessment margins (1,884) was 95% of our actual points
(1,974). If points simply *were* the margin, swapping weak signings for strong backups would pay well.

So the agent tested it — but bounded the bet:

- **At most 10 swaps** before the model had to prove itself (`PILOT_SWAPS`)
- **A 2-credit freshness check** before releasing anyone, after a first attempt lost backups to rivals who
  had signed them in the hours since verification
- **If the replacement offer failed, the original candidate was taken back immediately**
- **The agent graded itself**: accept the model only if measured ÷ predicted landed between 0.6 and 2.0

It predicted **+72.6 points**. The ledger moved **+11.0** — a ratio of 0.15. `check_model()` rejected the
model and swaps were switched off:

```
12:30:00 MODEL CHECK: swaps predicted +72.6 points, ledger moved +11.0 (ratio 0.15) -> rejected
```

Honest cost: about 95 points, or 2,131 credits. Without the pilot the final score would have been 1,586.8.
What it bought was a second measurement, which shows all five terms matter and margin carries roughly 2.5×
the weight of the others:

> points ≈ 4.19 × (skill match + 2.5 × margin + notes + notice + salary headroom)

The lesson, and the thing worth taking from this repository: **a points model is a hypothesis until the
ledger agrees — so cap the bet while you test it.**

## Edge cases

| Situation | Handling |
| --- | --- |
| Rate limited (`429`) | Client backs off; a call that still fails is deferred and retried, never counted as a failure (`is_transient`) |
| `404` or malformed record | Candidate marked dead, never paid for again |
| Empty search / role read in full | Role marked exhausted; weaker skill matches become eligible to profile |
| Offer rejected | `already_signed` → next in queue; `requisition_full` → closed locally; same person under another ID → skipped before paying |
| Credits exhausted | Every paid call stops, holdings kept, free calls continue (`Exhausted`) |
| Crash or redeploy | State saved atomically and on `SIGTERM`; at most 2 restarts; a restart resumes without re-buying |
| Late opening or a pause | Never exits on `closed` — polls every second and raced within ~1 s of the real opening |
| Arena reset | `reconcile()` compares holdings against the server's per-requisition counts and re-signs what was lost |
| Closing hour | Offers cost 20 credits, so no swaps; holdings kept to the whistle |

Three of these happened for real: offers opened **12 minutes late**, the organisers **paused** the arena
mid-run, and rivals took **11** queued names during the race. All three were handled with nobody steering.

State persistence is snapshot-under-lock → unique temp file → `os.replace()`, which is atomic on POSIX, so
`state.json` is always either the old complete file or the new one — a `SIGTERM` arriving mid-save cannot
corrupt it.

## Build timeline

Nine deploys across the day, each one visible in the log:

| Time | Change | Why |
| --- | --- | --- |
| 11:04 | First launch: recon pipeline and race | Offers locked until the market opened |
| 11:08 | Rate-limit give-ups retryable; 8 offer workers; save on stop | A give-up had wrongly killed a candidate |
| 11:10 | Each round waits for every requisition | An `any()` short-circuit let two workers buy the same candidate (325 credits) |
| 11:12 | Race starts the instant offers open | Removed ~1 s of calls between the phase flip and the first offer |
| 11:33 | Quality-aware verification; keep working if the opening is late | The market opened 12 minutes late |
| 11:59 | Survive pauses and resets; `MARKET_OPEN` from the environment | The organisers paused the arena |
| 12:23 | Upgrade swaps with a 10-swap pilot, judged by the ledger | Tested the margin hypothesis |
| 12:26 | Replacement re-checked (2 credits) before any release | Rivals had signed stale backups |
| 12:30 | Swaps off | The ledger moved +11.0 against +72.6 predicted |

## Running it

The arena is closed, so this is for reference only.

```bash
export ARENA_KEY=...            # from the environment, never hard-coded
export PENALTY_FACTOR=0.05      # announced at kick-off
./run_agent.sh
tail -f logs/agent.log          # every purchase, with its reason
```

Stop it with `kill <supervisor pid>` then `kill -TERM <agent pid>` — the `SIGTERM` handler saves state
before exiting, and a restart resumes from `state/state.json` without re-buying anything. Never
`pkill -f run_agent.sh`: the pattern matches the calling shell too.

| Setting | Default | Meaning |
| --- | --- | --- |
| `PENALTY_FACTOR` | `0.05` | Credits-to-points exchange rate; every threshold derives from it |
| `MARKET_OPEN` | `11:30` | Scheduled opening (`HH:MM`), used only to stop recon work early |
| `SWAPS` | `0` | Post-fill upgrade swaps. Off: the live pilot rejected the model they relied on |

## Not in this repository

- **The API key.** It was read from the environment at runtime and is not in any commit.
- **`state/`** — 8 MB holding 12,000 candidate records (names, cities, salaries, recruiter notes). That is
  the organisers' data, not ours to republish.
- **The problem statement and datasets**, which are marked for finalists only.

## Credits

Team **alpha_beta**, IIT Kanpur — Abhinav Raj, Abhinav Kumar, Megha Kumari.

`arena_client.py` is the organisers' starter-kit client, included unmodified so the agent reads as it ran.
Everything else is ours.

**AI assistant:** Claude was used for help in data analysis, writing and testing the agent's code. The team
reviewed and approved every deployment and decision.

Further reading: [`docs/strategy_note.pdf`](docs/strategy_note.pdf) — the one-page note submitted at the
whistle; [`docs/presentation.pdf`](docs/presentation.pdf) — the 10-slide finale presentation.
