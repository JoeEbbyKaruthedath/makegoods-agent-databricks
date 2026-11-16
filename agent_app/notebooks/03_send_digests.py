# Databricks notebook source
# MAGIC %md
# MAGIC # Morning make-good digest
# MAGIC
# MAGIC One email per seller: their open make-goods by deadline, the dollars at risk, and the option the rules engine
# MAGIC prepared overnight (rules passed, value against the missed spot), with a link into the app for each spot.
# MAGIC Sent through the same mail policy as client offers: synthetic addresses go to the demo inbox, anything not
# MAGIC allowed is refused. With demo mail (sandbox) the digests are only logged and shown below.

# COMMAND ----------

dbutils.widgets.text("catalog", "YOUR_CATALOG", "Catalog")
dbutils.widgets.text("schema", "makegoods", "Schema")
dbutils.widgets.text("mail_mode", "sandbox", "sandbox | gmail")
dbutils.widgets.text("mail_redirect_to", "", "Demo inbox")
dbutils.widgets.text("mail_allowed", "", "Allowed recipients")
dbutils.widgets.text("mail_secret_scope", "makegoods-mail", "Mail secret scope")
dbutils.widgets.text("app_url", "", "App URL for links")
CATALOG, SCHEMA = dbutils.widgets.get("catalog").strip(), dbutils.widgets.get("schema").strip()
APP_URL = dbutils.widgets.get("app_url").strip().rstrip("/")

import html
import os
import sys

os.environ.update({"MG_CATALOG": CATALOG, "MG_SCHEMA": SCHEMA, "MG_MAIL_MODE": dbutils.widgets.get("mail_mode").strip(),
                   "MG_MAIL_REDIRECT_TO": dbutils.widgets.get("mail_redirect_to").strip(),
                   "MG_MAIL_ALLOWED": dbutils.widgets.get("mail_allowed").strip(),
                   "MG_MAIL_SECRET_SCOPE": dbutils.widgets.get("mail_secret_scope").strip()})
sys.path.insert(0, os.path.abspath(".."))
from agent_server.mail import OutboundMessage, mail_mode  # noqa: E402
from agent_server.mail.policy import MailRefused, deliver_to  # noqa: E402

fq = lambda n: f"{CATALOG}.{SCHEMA}.{n}"  # noqa: E731

# COMMAND ----------

rows = spark.sql(f"""
  SELECT q.seller_id, s.seller_name, s.seller_email, q.spot_id, q.advertiser_name, q.call_letters, q.market, q.daypart,
         CAST(q.broadcast_date AS STRING) AS broadcast_date, q.preemption_reason, q.revenue_at_risk_usd, q.days_to_deadline,
         p.package_label, p.rules_passed, p.rules_total, p.dollar_match, p.dollar_label, p.is_full
  FROM {fq('gold_make_good_candidates')} q
  JOIN {fq('ref_sellers')} s ON s.seller_id = q.seller_id
  LEFT JOIN (SELECT * FROM {fq('mg_proposals')} WHERE package_label = 'A'
             QUALIFY ROW_NUMBER() OVER (PARTITION BY spot_id ORDER BY computed_at DESC) = 1) p ON p.spot_id = q.spot_id
  ORDER BY q.seller_id, q.days_to_deadline, q.revenue_at_risk_usd DESC""").collect()
by_seller: dict = {}
for r in rows:
    by_seller.setdefault(r.seller_id, []).append(r)
print(f"{len(rows)} open spots for {len(by_seller)} sellers; mail mode {mail_mode()}")


def link(spot_id: str) -> str:
    return f"{APP_URL}/#spot={spot_id}" if APP_URL else spot_id


def digest(seller_spots) -> tuple[str, str, str]:
    s = seller_spots[0]
    first = s.seller_name.split(" ")[0]
    at_risk = sum(x.revenue_at_risk_usd or 0 for x in seller_spots)
    urgent = [x for x in seller_spots if x.days_to_deadline <= 2]
    subject = f"Make-goods today: {len(seller_spots)} open, ${at_risk:,.0f} at risk" + (f", {len(urgent)} due within 2 days" if urgent else "")

    def line(x):
        opt = (f"Option {x.package_label} ready: {x.rules_passed}/{x.rules_total} rules, {x.dollar_label}"
               if x.package_label else "options not prepared yet")
        return (f"{x.spot_id} {x.advertiser_name}, {x.call_letters} {x.daypart} {x.broadcast_date} ({x.preemption_reason}), "
                f"${x.revenue_at_risk_usd:,.0f}, {x.days_to_deadline} day(s) left. {opt}. {link(x.spot_id)}")
    groups = [("Due within 2 days", [x for x in seller_spots if x.days_to_deadline <= 2]),
              ("Due in 3-5 days", [x for x in seller_spots if 2 < x.days_to_deadline <= 5]),
              ("Later", [x for x in seller_spots if x.days_to_deadline > 5])]
    text = "\n".join([f"Good morning {first},", "",
                      f"You have {len(seller_spots)} make-goods open, ${at_risk:,.0f} at risk if they are refunded.", ""]
                     + [part for title, xs in groups if xs for part in [title + ":", *[" - " + line(x) for x in xs], ""]]
                     + ["Open the app to review the options and send offers."])
    body = "".join(f"<h4>{html.escape(t)}</h4><ul>" + "".join(f"<li>{html.escape(line(x))}</li>" for x in xs) + "</ul>"
                   for t, xs in groups if xs)
    page = (f"<div style='font-family:Arial,sans-serif;font-size:14px'><p>Good morning {html.escape(first)},</p>"
            f"<p>You have <b>{len(seller_spots)}</b> make-goods open, <b>${at_risk:,.0f}</b> at risk.</p>{body}</div>")
    return subject, text, page


# COMMAND ----------

import asyncio  # noqa: E402
import uuid  # noqa: E402

adapter = None
if mail_mode() == "gmail":
    from agent_server.mail.gmail_api import GmailAdapter

    adapter = GmailAdapter.from_env()
log = []
for seller_id, spots in by_seller.items():
    subject, text, page = digest(spots)
    s = spots[0]
    try:
        to = deliver_to(s.seller_email, tag=s.seller_name)
        if adapter:
            sent = asyncio.run(adapter.send(OutboundMessage(to=to, subject=subject, text=text, html=page, ref="DIGEST")))
            message_id = sent.message_id
        else:
            message_id = f"sbx-digest-{uuid.uuid4().hex[:12]}"
        log.append((seller_id, to, len(spots), float(sum(x.revenue_at_risk_usd or 0 for x in spots)), message_id, mail_mode()))
        print(f"--- {seller_id} -> {to}: {subject}")
    except MailRefused as e:
        print(f"--- {seller_id}: not sent ({e})")
if by_seller:
    print("\n" + digest(next(iter(by_seller.values())))[1])  # one digest in full

if log:
    spark.createDataFrame(log, "seller_id STRING, to_addr STRING, spots INT, at_risk_usd DOUBLE, message_id STRING, mail_mode STRING") \
        .createOrReplaceTempView("digests")
    spark.sql(f"INSERT INTO {fq('mg_digest_log')} SELECT current_timestamp(), seller_id, to_addr, spots, at_risk_usd, message_id, "
              f"mail_mode FROM digests")
print(f"{len(log)} digests")
