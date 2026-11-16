CREATE OR REPLACE VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_offer_outcome_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: YOUR_CATALOG.YOUR_SCHEMA.gold_make_good_outcomes
  comment: "Make-good offers made by the inherited process (rules engine, GPT assist, ad-ops experts): what was offered, whether the advertiser accepted, refunds issued, and downgrades to a weaker daypart. One row per offer."
  dimensions:
    - name: Market
      expr: market
      comment: "Designated market area (DMA) of the station."
      display_name: Market
      synonyms: ["DMA", "city"]
    - name: Station
      expr: call_letters
      comment: "Station call letters."
      display_name: Station
      synonyms: ["call letters"]
    - name: Advertiser
      expr: advertiser_name
      comment: "Advertiser of the preempted spot."
      display_name: Advertiser
      synonyms: ["client"]
    - name: Advertiser Category
      expr: advertiser_category
      comment: "Advertiser business category."
      display_name: Advertiser Category
      synonyms: ["category", "vertical"]
    - name: Preempted Daypart
      expr: original_daypart
      comment: "Daypart of the spot that was preempted. This is the default meaning of 'daypart' for offers and refunds (e.g. 'refunds by daypart')."
      display_name: Preempted Daypart
      synonyms: ["daypart", "original daypart", "spot daypart"]
    - name: Offered Daypart
      expr: offered_daypart
      comment: "Daypart of the replacement avail that was offered. Use only when the user asks about the offered or replacement daypart; otherwise use Preempted Daypart."
      display_name: Offered Daypart
      synonyms: ["replacement daypart"]
    - name: Matching Method
      expr: "CASE matched_by WHEN 'manual' THEN 'Ad-ops expert' WHEN 'rules_engine' THEN 'Rules engine' WHEN 'gpt_assist' THEN 'GPT assist' ELSE matched_by END"
      comment: "Who chose the offer: Ad-ops expert, Rules engine, or GPT assist (the earlier GPT-based attempt)."
      display_name: Matching Method
      synonyms: ["matcher", "method", "GPT", "rules engine"]
    - name: Offer Status
      expr: offer_status
      comment: "Outcome of the offer: Accepted, Rejected, Expired, Pending."
      display_name: Offer Status
      synonyms: ["outcome"]
    - name: Is Downgrade
      expr: "CASE WHEN is_downgrade THEN 'Downgrade' ELSE 'Same or better daypart' END"
      comment: "Whether the offer was in a weaker daypart than the preempted spot."
      display_name: Downgrade
      synonyms: ["downgraded"]
    - name: Preemption Reason
      expr: preemption_reason
      comment: "Why the original spot did not air."
      display_name: Preemption Reason
      synonyms: ["reason", "cause"]
    - name: Offer Date
      expr: "DATE(offered_ts)"
      comment: "Date the offer was made."
      display_name: Offer Date
    - name: Offer Week
      expr: "DATE_TRUNC('WEEK', offered_ts)"
      comment: "Week (Monday start) the offer was made."
      display_name: Offer Week
  measures:
    - name: Offers
      expr: COUNT(1)
      comment: "Number of make-good offers."
      display_name: Offers
    - name: Accepted Offers
      expr: COUNT(1) FILTER (WHERE offer_status = 'Accepted')
      comment: "Offers the advertiser accepted."
      display_name: Accepted Offers
    - name: Acceptance Rate
      expr: MEASURE(`Accepted Offers`) / MEASURE(`Offers`)
      comment: "Share of offers accepted by the advertiser."
      display_name: Acceptance Rate
      format:
        type: percentage
        decimal_places:
          type: exact
          places: 0
      synonyms: ["accept rate", "win rate"]
    - name: Refunds Issued
      expr: COUNT(1) FILTER (WHERE refund_issued)
      comment: "Offers that ended in a refund (rejected or expired)."
      display_name: Refunds Issued
    - name: Refunded Amount
      expr: SUM(refund_amount_usd)
      comment: "Dollars refunded to advertisers because the make-good offer was rejected or expired."
      display_name: Refunded Amount
      format:
        type: currency
        currency_code: USD
        decimal_places:
          type: exact
          places: 0
      synonyms: ["refunds", "refund dollars", "money refunded"]
    - name: Downgrade Offers
      expr: COUNT(1) FILTER (WHERE is_downgrade)
      comment: "Offers in a weaker daypart than the preempted spot."
      display_name: Downgrade Offers
    - name: Downgrade Rate
      expr: MEASURE(`Downgrade Offers`) / MEASURE(`Offers`)
      comment: "Share of offers that were downgrades."
      display_name: Downgrade Rate
      format:
        type: percentage
        decimal_places:
          type: exact
          places: 0
    - name: Avg GPT Tokens per Offer
      expr: AVG(gpt_total_tokens) FILTER (WHERE matched_by = 'gpt_assist')
      comment: "Average LLM tokens the earlier GPT-assist matcher used per offer (the cost baseline the new agent is measured against)."
      display_name: Avg GPT Tokens per Offer
      synonyms: ["GPT cost", "tokens per offer"]
$$
