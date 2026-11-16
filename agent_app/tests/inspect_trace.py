"""Print a trace's span tree with types and latency: python tests/inspect_trace.py <trace_id>"""
import sys

from dotenv import load_dotenv

load_dotenv(".env", override=True)
import mlflow  # noqa: E402

trace = mlflow.get_trace(sys.argv[1])
spans = trace.data.spans
by_parent = {}
for s in spans:
    by_parent.setdefault(s.parent_id, []).append(s)


def show(parent, depth):
    for s in sorted(by_parent.get(parent, []), key=lambda x: x.start_time_ns):
        ms = (s.end_time_ns - s.start_time_ns) / 1e6
        extra = ""
        if s.span_type in ("CHAT_MODEL", "LLM"):
            u = (s.attributes or {}).get("mlflow.chat.tokenUsage")
            extra = f" tokens={u}" if u else ""
        print(f"{'  ' * depth}{s.name} [{s.span_type}] {ms:,.0f} ms{extra}")
        show(s.span_id, depth + 1)


show(None, 0)
print("trace tags:", {k: v for k, v in trace.info.tags.items() if not k.startswith("mlflow.")})
print("token usage:", trace.info.token_usage)
