"""Evaluate the portal's fail-closed gate against a labelled prompt set.

For each prompt in ``prompts.csv`` the script runs the same two layers the
Voice_Service runs before any AWS DevOps Agent call:

1. the deterministic lexical check (``app.domain.mutation_guard``), and
2. ``ApplyGuardrail`` with the deployed guardrail, decided by the service's
   own fail-closed policy (``app.domain.guardrail_policy.decide``).

The gate passes a prompt only when both layers pass. Both layers are always
evaluated, so the results show which layer caught each change request.

Usage (from the repository root)::

    python3 evaluation/run_eval.py --guardrail-id <id> --guardrail-version <n>
    python3 evaluation/run_eval.py --lexical-only      # no AWS calls

``ApplyGuardrail`` evaluates text only; it changes nothing in the account.
Each call is billed as a guardrail text-unit evaluation.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend" / "voice_service")]

from app.domain.guardrail_policy import DecisionOutcome, GuardrailEvaluation, decide  # noqa: E402
from app.domain.mutation_guard import has_mutation_intent  # noqa: E402

HERE = Path(__file__).resolve().parent


def guardrail_decision(client, guardrail_id: str, version: str, text: str) -> tuple[str, str]:
    """Return (raw action, gate decision) for one prompt."""
    try:
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=version,
            source="INPUT",
            content=[{"text": {"text": text}}],
        )
    except Exception as exc:  # the service treats every SDK error as a block
        return f"ERROR:{type(exc).__name__}", "BLOCK"
    evaluation = GuardrailEvaluation.from_response(dict(response))
    outcome = decide(evaluation).outcome
    return str(evaluation.action), "PASS" if outcome == DecisionOutcome.PASS else "BLOCK"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--guardrail-id")
    parser.add_argument("--guardrail-version")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile")
    parser.add_argument("--lexical-only", action="store_true")
    args = parser.parse_args()

    client = None
    if not args.lexical_only:
        if not (args.guardrail_id and args.guardrail_version):
            parser.error("--guardrail-id and --guardrail-version are required unless --lexical-only")
        import boto3

        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        client = session.client("bedrock-runtime")

    rows = list(csv.DictReader((HERE / "prompts.csv").open(encoding="utf-8")))
    results = []
    for row in rows:
        lexical = "BLOCK" if has_mutation_intent(row["prompt"]) else "PASS"
        action, guard = ("n/a", "n/a")
        if client is not None:
            action, guard = guardrail_decision(client, args.guardrail_id, args.guardrail_version, row["prompt"])
            time.sleep(0.2)
        gate = "BLOCK" if "BLOCK" in (lexical, guard) else "PASS"
        results.append({**row, "lexical": lexical, "guardrail_action": action,
                         "guardrail": guard, "gate": gate, "match": gate == row["expected"]})

    out_csv = HERE / "results.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    total = len(results)
    matched = sum(r["match"] for r in results)
    lines = [
        "# Gate evaluation results",
        "",
        f"Run: {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC. "
        + ("Lexical check only." if client is None
           else f"Deployed guardrail version {args.guardrail_version}, Region {args.region}."),
        "",
        f"Gate outcome matched the expected outcome for {matched} of {total} prompts.",
        "",
        "| Category | Prompts | Matched |",
        "|---|---|---|",
    ]
    for cat in dict.fromkeys(r["category"] for r in results):
        sub = [r for r in results if r["category"] == cat]
        lines.append(f"| {cat} | {len(sub)} | {sum(r['match'] for r in sub)} |")
    lines += ["", "| ID | Expected | Lexical | Guardrail | Gate | Match | Prompt |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['expected']} | {r['lexical']} | {r['guardrail']} | {r['gate']} | "
                     f"{'yes' if r['match'] else '**no**'} | {r['prompt']} |")
    (HERE / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{matched}/{total} matched; wrote {out_csv.name} and results.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
