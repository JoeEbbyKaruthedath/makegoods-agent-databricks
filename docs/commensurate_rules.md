# "Commensurate" Make-Good Rules (Phase 0.3)

The hardest domain problem in the use case. Every rule below runs as deterministic SQL in a Unity Catalog
function. The LLM never evaluates a rule; it only orchestrates and explains. Thresholds and weights live in
`YOUR_CATALOG.YOUR_SCHEMA.mg_rules_config`, so ad ops can tune them without a code change.

## Where each rule lives

| Function | Role |
|---|---|
| `evaluate_inventory(spot_id, as_of)` | The rules engine: every must-match rule, for every avail on the spot's station, with the list of failed rules |
| `is_commensurate(spot_id, avail_id, as_of)` | Pass/fail for one pair (used by tests and the evaluation's `inventory_relevance` scorer) |
| `inventory_search(...)` | Only the avails that pass, plus reviewer constraints (exclude tags/avails, restrict dayparts) |
| `inventory_rule_summary(spot_id)` | Explains the search: evaluated, commensurate, failures per rule |
| `commensurate_scorer(spot_id, avail_ids, prefs)` | Soft-match ranking with a per-dimension breakdown and plain-English reasons |
| `impression math: shortfall_calculator(spot_id, avail_ids)` | Owed vs proposed impressions in the contracted segment, plus the order-level gap |
| `audience_mapper(advertiser_id, text)` / `normalize_audience_key(text)` | Free-text audience → canonical segment |
| `daypart_rank(daypart)` | Policy daypart value order |

## Must-match rules (all required)

| # | Rule | Failure code | Config key | Question for ad ops |
|---|---|---|---|---|
| 1 | Same station (same DMA) | (station join) | — | Can a make-good run on a sister station in the same DMA (duopoly)? |
| 2 | Same or better daypart: Daytime < Early Morning < Early Fringe < Late Fringe < Prime Access < Prime | `daypart_lower_value` | `daypart_rank()` | Is this value order right? Is an upgrade always acceptable? |
| 3 | Avail rating **in the contract's audience segment** ≥ floor × guaranteed rating | `rating_below_floor` | `rating_floor_pct` = 0.90 | Is 90% right? Does it vary by advertiser tier or agency? |
| 4 | Same spot length | `spot_length_mismatch` | — | Are 2×15s an acceptable make-good for a :30? |
| 5a | Airs within the make-good window after the preempted spot | `outside_makegood_window` | contract `makegood_window_days`, default `default_makegood_window_days` = 14 | Same week, same flight, or N days? Who approves exceptions? |
| 5b | Airs no later than flight end | `after_flight_end` | — | Can a make-good extend past the flight with advertiser consent? |
| 5c | Airs after the decision date | `already_aired` | `as_of_date` (simulation clock) | — |
| 6a | Avail is Open (not Held) | `avail_held` | — | Who can release held inventory for make-goods? |
| 6b | Not already booked for another make-good | `already_booked` | — | — |
| 7 | Contract is make-good eligible | `contract_not_makegood_eligible` | contract flag | Which categories never get make-goods (political?) |

Segment rating = household rating × daypart index × genre index, from the audience taxonomy. This is why
audience mapping matters: map "Women 25-54" to "Adults 25-54" and a Sports avail looks commensurate when it is not.

## Soft-match ranking (score = weighted sum; must-match failures always rank last)

| Dimension | Contribution | Config key (default) | Question for ad ops |
|---|---|---|---|
| Impression equivalence | min(ratio, cap) / cap | `w_impressions` 0.35, `ie_cap` 1.2 | Is 1:1 the target, or is a quality multiplier needed? |
| Genre continuity | 1 if same genre as preempted program | `w_genre` 0.15 | How much does genre continuity matter? |
| Advertiser adjacency preference | Σ learned tag weights, clipped to ±1 | `w_preference` 0.20 | Which advertisers have written adjacency requirements? |
| Yield | 1 − abs(rate card / unit rate − 1) | `w_yield` 0.15 | Is giving away premium inventory a revenue concern? |
| Timeliness | 1 − days after spot / window | `w_timeliness` 0.10 | — |
| Competitive separation | −1 if a competitor is in the pod | `w_competitive` 0.30 | Should separation be a hard rule for some categories (auto, legal)? |

Advertiser preference weights are learned with `weight = (accepts − rejects) / (accepts + rejects + 1)`:
- **Baseline from history:** `gold_advertiser_preferences`, learned from like-for-like decisions before `pref_cutoff_date`.
- **Live updates:** reviewer accept/modify/reject decisions, stored in Lakebase preference memory.

## Tiered match score (out of 10)

Every candidate gets a **match score out of 10, rounded to a whole number**. 10/10 is a perfect match.

```
match score = round(10 − Σ graded rule penalties − fit_penalty_max × (1 − soft-match fit))
```

Options are ranked by tier, then by the unrounded score.

| Top option's tier | What the agent does |
|---|---|
| ✓ **Commensurate**: passes every must-match rule (usually 8–10) | Recommends the make-good |
| ≈ **Near-miss**: soft misses only, score ≥ `near_miss_min_score` (4/10) | Recommends it as an **exception** for reviewer approval, with the misses listed. The reviewer can choose **Refund instead** |
| ✕ **Low match**: score below 4/10 | **Shows the best options anyway, then recommends a refund.** The reviewer can **Book selected instead** as an exception |
| Not possible: already aired, already booked, or the contract doesn't allow make-goods | Never offered (these stay hard rules). If nothing else exists, it's a plain refund |

Penalties are graded by how close the miss is (all in `mg_rules_config`, rule_type `near_miss`):

| Miss | Penalty | Example |
|---|---|---|
| Rating below the floor | `pen_rating_base` 1.0 + `pen_rating_per_pct` 0.2 per 1% short | 5.40 vs a 5.58 floor (−3.2%) → 1.64 |
| Lower-value daypart | `pen_daypart_step` 2.0 per step down | Prime Access for a Prime spot → 2.0 |
| Spot length | `pen_length_shorter` 2.5 / `pen_length_longer` 1.0 | :30 avail for a :60 spot → 2.5 |
| Timing (charged once: the larger of the two) | window: `pen_window_base` 1.0 + 0.25/day late; flight: `pen_flight_base` 1.5 + 0.25/day | 8 days past the window, 5 after flight end → 3.0 |
| Avail on hold | `pen_avail_held` 1.5 | Traffic must release it |

On the current work queue (331 spots):
- **239 spots** have commensurate options.
- **All 92 spots ($92.6K) that used to be refunds** now get an exception offer scoring 4/10 or better.

Reviewer constraints (e.g. "avoid entertainment adjacency") can push the best option below 4/10. The agent then shows the options and recommends the refund.

Approved exceptions are logged as `make_good_exception`, with the misses recorded in the decision log.

## Changing a rule
1. Change the threshold or weight in `mg_rules_config` (e.g. `UPDATE mg_rules_config SET num_value = 0.85 WHERE rule_key = 'rating_floor_pct'`).
2. Re-run the evaluation (`agent_app/eval/run_eval.py`) and compare the new run with the last release in the MLflow UI.
3. Changing the rule logic itself means editing `uc_functions/10_agent_tools.sql`. Unity Catalog versions it and records lineage; the agent code does not change.
