"""End-to-end test runs against the deployed make-goods app, as the signed-in CLI user (demo mail only).

Each scenario takes one open spot through the whole lifecycle with the app's own API:
  find options -> offer Option A -> manager approves -> send -> client replies (demo inbox) -> the reply is read
  -> the inbox recommends the next step -> the seller takes it (or leaves it for the demo).

  python tools/e2e_app.py --profile <profile> [--app makegoods-v2] [--scenarios accept,decline,counter,question]

The app must run with demo mail (MG_MAIL_MODE=sandbox): replies are posted to the demo client inbox, nothing is emailed.
Prints one line per step and a summary table; exit code 1 when a scenario does not end where it should.
"""
import argparse
import json
import sys
import time
from uuid import uuid4

import requests
from databricks.sdk import WorkspaceClient

SCENARIOS = {  # client's reply, the reading expected, the recommended step, what the seller does next
    "accept": ("Thanks, Option A works for us. Please go ahead and book it.", "accept", "book", "book"),
    "decline": ("None of this works for our campaign this month. Please just credit the spot.", "decline", "refund", "refund"),
    "counter": ("Could you do Tuesdays or Thursdays instead? Our phones are busiest then.", "counter", "counter", "counter"),
    "question": ("What are the ratings on that slot for adults 35 plus? I want to check before we say yes.", "question", "open", None),
}


class App:
    def __init__(self, url: str, w: WorkspaceClient):
        self.url, self.w = url.rstrip("/"), w

    def _h(self) -> dict:
        return {**self.w.config.authenticate(), "Content-Type": "application/json"}  # token stays in memory

    def get(self, path: str):
        r = requests.get(self.url + path, headers=self._h(), timeout=120)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict):
        r = requests.post(self.url + path, headers=self._h(), data=json.dumps(body), timeout=600)
        r.raise_for_status()
        return r.json()

    def chat(self, body: dict) -> tuple[dict, float, list[str]]:
        """Run one agent turn; returns the final outputs, seconds, and any errors streamed."""
        t0, final, errors = time.time(), {}, []
        with requests.post(self.url + "/api/chat", headers=self._h(), data=json.dumps(body), stream=True, timeout=900) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                if ev["type"] == "final":
                    final = ev["data"]
                elif ev["type"] == "error":
                    errors.append(str(ev["data"]))
        return final, time.time() - t0, errors


def paused(out: dict) -> str:
    return (out.get("pending") or {}).get("type") or "-"


def run(app: App, name: str, spot: dict) -> dict:
    reply, want_read, want_rec, then = SCENARIOS[name]
    tid, res = str(uuid4()), {"scenario": name, "spot": spot["spot_id"], "client": spot["advertiser_name"], "steps": [], "ok": False}

    def step(label, out, secs, errors, expect):
        ok = paused(out) == expect and not errors
        res["steps"].append({"step": label, "paused_at": paused(out), "seconds": round(secs, 1), "tokens": out.get("llm_tokens"),
                             "ok": ok, "errors": errors})
        print(f"  {name:<9} {label:<28} -> {paused(out):<17} {secs:6.1f}s  tokens={out.get('llm_tokens')}"
              + ("" if ok else f"  EXPECTED {expect} {errors}"))
        return ok

    out, s, e = app.chat({"message": "", "thread_id": tid, "spot_id": spot["spot_id"], "mode": "find"})
    if not step("find options", out, s, e, "make_good_review"):
        return res
    res["best"] = {k: (out.get("packages") or [{}])[0].get(k) for k in ("package_label", "rules_passed", "rules_total", "dollar_match")}
    for label, review, expect in [("offer Option A", {"action": "offer", "labels": ["A"]}, "manager_review"),
                                  ("manager approves", {"action": "approve"}, "confirm_send"),
                                  ("send to the client", {"action": "send"}, "await_client")]:
        out, s, e = app.chat({"message": "", "thread_id": tid, "review": review})
        if not step(label, out, s, e, expect):
            return res
    offer = next((o for o in app.get("/api/offers") if o["thread_id"] == tid), None)
    if not offer:
        print(f"  {name:<9} no offer found for the thread")
        return res
    t0 = time.time()
    picked = app.post(f"/api/inbox/{offer['offer_id']}/reply", {"text": reply})["picked_up"]
    res["reply_seconds"] = round(time.time() - t0, 1)
    th = next((t for t in app.get("/api/inbox/threads") if t["thread_id"] == tid), {})
    read = (th.get("reply") or {}).get("decision")
    rec = (th.get("recommended") or {}).get("action")
    stored = [r.get("intent") for r in th.get("replies") or []]
    res.update(read=read, read_by=(th.get("reply") or {}).get("read_by"), stored_reading=stored, recommended=rec,
               read_ok=read == want_read, rec_ok=rec == want_rec)
    print(f"  {name:<9} client replies                -> read as {read} ({res['read_by']}, stored {stored}), "
          f"recommended {rec}  {res['reply_seconds']}s" + ("" if picked else "  NOT PICKED UP"))
    if not (picked and res["read_ok"] and res["rec_ok"]):
        return res
    if then is None:
        res["ok"] = True  # left at the seller's confirmation, for the demo inbox
        res["end"] = "waiting for the seller"
        return res
    review = {"action": "book", "label": "A", "labels": ["A"]} if then == "book" else {"action": then}
    out, s, e = app.chat({"message": "", "thread_id": tid, "review": review})
    expect = "make_good_review" if then == "counter" else "-"
    res["ok"] = step(f"seller: {then}", out, s, e, expect)
    res["end"] = out.get("stage") or out.get("outcome")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--app", default="makegoods-v2")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    a = ap.parse_args()
    w = WorkspaceClient(profile=a.profile)
    app = App(w.apps.get(a.app).url, w)
    cfg = app.get("/api/config")
    if not cfg.get("sandbox_inbox"):
        print("The app is not on demo mail; these runs post client replies to the demo inbox only. Stopping.")
        return 1
    print(f"As {cfg.get('display_name')} ({cfg.get('role')}), data as of {cfg.get('as_of')}")
    queue = [q for q in app.get("/api/queue") if not q.get("thread_id")]
    names = a.scenarios.split(",")
    if len(queue) < len(names):
        print(f"Only {len(queue)} open spots without a conversation; need {len(names)}.")
        return 1
    seen, picks = set(), []
    for q in queue:  # a different client per scenario
        if q["advertiser_id"] not in seen:
            seen.add(q["advertiser_id"])
            picks.append(q)
        if len(picks) == len(names):
            break
    results = [run(app, n, q) for n, q in zip(names, picks)]
    print("\nscenario   spot          client                        best option     read as     recommended  end                     result")
    for r in results:
        b = r.get("best") or {}
        best = f"{b.get('package_label')} {b.get('rules_passed')}/{b.get('rules_total')} {'$ok' if b.get('dollar_match') else '$off'}" if b else "-"
        print(f"{r['scenario']:<10} {r['spot']:<13} {r['client'][:29]:<29} {best:<15} {str(r.get('read')):<11} "
              f"{str(r.get('recommended')):<12} {str(r.get('end')):<23} {'PASS' if r['ok'] else 'FAIL'}")
    secs = [s["seconds"] for r in results for s in r["steps"]]
    toks = [s["tokens"] for r in results for s in r["steps"] if s["step"] == "find options" and s["tokens"]]
    if secs:
        print(f"\nagent turns: {len(secs)}, median {sorted(secs)[len(secs) // 2]}s, slowest {max(secs)}s;"
              f" tokens per recommendation: {', '.join(str(t) for t in toks)}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
