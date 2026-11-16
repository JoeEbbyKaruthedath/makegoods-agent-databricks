-- Client replies to make-good offers, read with AI Functions in SQL (run AFTER 50_lifecycle_tables.sql).
--
--   mg_client_emails        every reply as received (the app writes it as itself; people read mg_client_emails_mine)
--   read_client_email(body) ai_classify: does the client say yes, no, or need more information?
--                           ai_extract: which option, what they ask to change, what they ask
--   mg_client_email_reads   the reading of each email (one row per read; the latest counts)
--   mg_client_emails_mine   emails + their reading, limited to the clients the person may see
--
-- The app reads each new reply with the same statement as the batch at the end of this file, for that one email,
-- and the seller confirms before anything is booked.
USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

CREATE TABLE IF NOT EXISTS mg_client_emails (
  email_id STRING NOT NULL,
  received_at TIMESTAMP,
  offer_id STRING,
  thread_id STRING,
  spot_id STRING,
  advertiser_id STRING,
  advertiser_name STRING,
  market STRING,
  seller_id STRING,
  from_addr STRING,
  subject STRING,
  body STRING COMMENT 'The client’s own words (quoted history removed)',
  offered_options ARRAY<STRING> COMMENT 'Option letters the offer listed',
  source STRING COMMENT 'mailbox | client inbox (demo) | entered by the seller | sample'
)
COMMENT 'Client replies to make-good offers, as received. Written by the app; read with AI Functions (read_client_email).';

CREATE TABLE IF NOT EXISTS mg_client_email_reads (
  email_id STRING NOT NULL,
  read_at TIMESTAMP NOT NULL,
  intent STRING COMMENT 'yes | no | needs more information',
  option_label STRING COMMENT 'The option letter the client picks, if any',
  requested_change STRING,
  question STRING
)
COMMENT 'What AI Functions read from each client email: yes, no or needs more information, the option picked, the change or question.';

CREATE OR REPLACE FUNCTION read_client_email(p_body STRING COMMENT 'The client’s reply')
RETURNS STRUCT<intent: STRING, option_label: STRING, requested_change: STRING, question: STRING>
COMMENT 'Reads a client reply to a make-good offer with AI Functions: ai_classify decides whether the client accepts an offered option (yes), declines or asks for a refund (no), or asks a question or for something different (needs more information); ai_extract finds the option letter, the requested change and the question.'
RETURN (
  SELECT named_struct(
    'intent', CASE c WHEN 'accepts one of the offered options' THEN 'yes'
                     WHEN 'declines the offer, or asks for a refund or a credit' THEN 'no'
                     ELSE 'needs more information' END,
    'option_label', nullif(regexp_extract(upper(coalesce(x.chosen_option, '')), '(^|[^A-Z])([A-C])([^A-Z]|$)', 2), ''),
    'requested_change', x.requested_change,
    'question', x.question)
  FROM (SELECT ai_classify(p_body, ARRAY('accepts one of the offered options', 'declines the offer, or asks for a refund or a credit',
                                         'asks a question or for something different')) AS c,
               ai_extract(p_body, ARRAY('chosen_option', 'requested_change', 'question')) AS x)
);

CREATE OR REPLACE VIEW mg_client_emails_mine
COMMENT 'Client replies with their AI reading, limited to the clients the person may see.'
AS SELECT e.*, r.intent, r.option_label, r.requested_change, r.question, r.read_at
FROM mg_client_emails e
LEFT JOIN (SELECT * FROM mg_client_email_reads QUALIFY ROW_NUMBER() OVER (PARTITION BY email_id ORDER BY read_at DESC) = 1) r
  USING (email_id)
WHERE YOUR_CATALOG.YOUR_SCHEMA_security.mg_row_visible(e.seller_id, e.market);

-- read every email that has not been read yet
INSERT INTO mg_client_email_reads
SELECT email_id, current_timestamp(), r.intent, r.option_label, r.requested_change, r.question
FROM (SELECT email_id, read_client_email(body) AS r FROM mg_client_emails
      WHERE email_id NOT IN (SELECT email_id FROM mg_client_email_reads));
