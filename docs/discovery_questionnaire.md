# Make-Goods Agent — Data Discovery Questionnaire (Phase 0.1)

Bring this to the data discovery session with the station group's ad ops and engineering teams. Each section maps to a feed
in the reference build. The synthetic build already shows the target shape, so every answer is "what does
your real system have, and how do we land it in that shape?"

| Feed (reference table) | Synthetic source today | What we need to learn |
|---|---|---|
| `bronze_ad_schedule` | daily as-run CSV per station | Traffic system, export method, grain, latency |
| `bronze_inventory_avails` | weekly avails CSV | How unsold inventory is exposed, and how often |
| `bronze_audience_segments` | taxonomy JSON | Whether a canonical audience_segment_id exists |
| `bronze_delivery_pacing` | hourly measurement CSV | Delivery metrics, latency, grain (go/no-go for Phase 2) |
| `bronze_contracts` | order JSON | Guarantee structure and make-good eligibility |
| `bronze_make_good_offers` | offer history JSON | Past decisions (this becomes the evaluation dataset) |

## 1. Inventory and schedule (traffic system)
1. Which traffic/scheduling systems hold the log and avails? Is it one system for all owned stations or several?
2. How do we get the data: API, scheduled export, or database replica? What are the rate limits and auth model?
3. Grain: is the as-run at spot level with scheduled and actual air time? Is break/pod position available?
4. How fast does a preemption show up in the log: minutes, end of day, or next day?
5. How are avails represented: unsold units per break, or a daypart-level count? Is held vs open inventory distinguishable?
6. Is program genre or program type on the log, or does it come from a separate program guide?
7. Is competitive separation encoded in the pod: do we know which categories are already in a break?

## 2. Audience taxonomy
1. Is there a canonical `audience_segment_id`, or do orders carry free text ("Adults 25-54", "P25-54", "W25-54")?
2. Who owns the taxonomy and its aliases? How often does it change?
3. Which rating source and universe estimates (by DMA and segment) are authoritative for guarantees?
4. Are segment ratings by daypart and program available (the index we use to turn household ratings into segment ratings)?

## 3. Delivery / pacing (critical for Phase 2 forecasting)
1. Which delivery metrics exist: impressions delivered vs contracted, and per spot, flight or campaign?
2. Latency: set-top box / ACR (hours), Nielsen overnights (next day), final C3/C7 (days)? Which stations have which?
3. Are restatements (preliminary → final) sent as new rows or updates?
4. Is pacing available for every guaranteed order, or only some? What is the coverage percentage?

## 4. Contracts / orders
1. Where do guaranteed-audience contracts live (order management system / proposal tool)?
2. Fields: guaranteed rating or impressions, audience target, flight dates, weekly spot count, unit rate, spot length.
3. Make-good terms: eligibility, window (same week / same flight / N days), approval rules, and categories that never get make-goods (e.g. political).
4. Advertiser-specific terms: written adjacency requirements, exclusions, competitive separation.

## 5. Historical make-good outcomes (evaluation dataset)
1. Where are past make-good offers recorded: traffic notes, email, a spreadsheet, the GPT tool's logs?
2. For each: preempted spot, offered replacement, who proposed it, accepted/rejected/expired, refund amount.
3. How many months of history are reliable? We need at least ~150 accepted expert decisions for a fair test.
4. Token and cost logs from the previous GPT attempt (to set the baseline the new agent must beat by 10x).

## 6. "Commensurate" business rules (see `commensurate_rules.md`)
Walk through every rule and weight in `mg_rules_config` with ad ops and record the decision and its owner.

## 7. Access and environments
1. Target workspaces: DATA_DEV / DATA_PROD for pipelines, AI_DEV / AI_PROD for the agent app. Which catalog and schemas?
2. Service principals for the pipeline and the app. Who approves the UC grants?
3. Lakebase: who will own the agent memory database? Are there retention requirements for decision logs?
