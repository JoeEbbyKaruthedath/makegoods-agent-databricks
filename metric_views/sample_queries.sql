USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;
SELECT `Market`, MEASURE(`Open Spots`) AS open_spots, MEASURE(`Revenue at Risk`) AS revenue_at_risk, MEASURE(`Spots Due Within 3 Days`) AS due_3d FROM makegoods_open_queue_metrics GROUP BY ALL ORDER BY revenue_at_risk DESC LIMIT 5;
SELECT `Matching Method`, MEASURE(`Offers`) AS offers, MEASURE(`Acceptance Rate`) AS acceptance_rate, MEASURE(`Refunded Amount`) AS refunded, MEASURE(`Avg GPT Tokens per Offer`) AS gpt_tokens FROM makegoods_offer_outcome_metrics GROUP BY ALL ORDER BY acceptance_rate DESC;
SELECT `Broadcast Week`, `Preemption Reason`, MEASURE(`Preempted or Missed Spots`) AS preempted, MEASURE(`Preemption Rate`) AS rate FROM makegoods_preemption_metrics WHERE `Preemption Reason` <> 'Aired as scheduled' GROUP BY ALL ORDER BY `Broadcast Week`, preempted DESC LIMIT 8;
SELECT `Pacing Status`, MEASURE(`Contracts`) AS contracts, MEASURE(`Pacing Index`) AS pacing_index, MEASURE(`Impression Shortfall to Date`) AS shortfall FROM makegoods_contract_delivery_metrics GROUP BY ALL ORDER BY contracts DESC;
SELECT `Preempted Daypart`, MEASURE(`Refunded Amount`) AS refunded, MEASURE(`Downgrade Rate`) AS downgrade_rate FROM makegoods_offer_outcome_metrics GROUP BY ALL ORDER BY refunded DESC LIMIT 3;
