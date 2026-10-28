import argparse
import json
from pathlib import Path
import sys
from .config import load_config, build_plan
from .io import read_json, write_json
from .pipeline import initialize, doctor, resume
from .stages import ORDER, selected_stages


def main(argv=None):
    parser = argparse.ArgumentParser(prog="antigen-design")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "doctor", "run"):
        p = subs.add_parser(name)
        p.add_argument("config")
        p.add_argument("--server", help="Server profile YAML; scientific configuration overrides it")
        if name == "doctor":
            p.add_argument("--remote", action="store_true")
            p.add_argument("--report")
            p.add_argument("--only", choices=ORDER)
        if name == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--wait", action="store_true")
            p.add_argument("--until", choices=ORDER)
    for name in ("status", "resume", "collect", "report"):
        p = subs.add_parser(name)
        p.add_argument("directory")
        if name == "report":
            p.add_argument("--policy", help="Explicit versioned JSON structural screening policy")
        if name == "resume":
            p.add_argument("--wait", action="store_true")
            p.add_argument("--retry-failed", action="store_true")
            g = p.add_mutually_exclusive_group()
            g.add_argument("--until", choices=ORDER)
            g.add_argument("--only", choices=ORDER)
    p = subs.add_parser("reconcile")
    p.add_argument("directory")
    p.add_argument("--task", required=True)
    p.add_argument("--job-id", required=True)
    p = subs.add_parser("import-results")
    p.add_argument("manifest")
    p.add_argument("directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "reconcile":
            from .remote import reconcile
            print(json.dumps(reconcile(args.directory, args.task, args.job_id), indent=2))
        elif args.command == "import-results":
            from .legacy_import import import_results
            print(json.dumps(import_results(args.manifest, args.directory), indent=2))
        elif args.command == "report":
            from .report import report
            if args.policy:
                from .screening import screen
                print(json.dumps(screen(args.directory, args.policy), indent=2))
            else:
                print(json.dumps(report(args.directory), indent=2))
        elif args.command == "status":
            print(json.dumps(read_json(Path(args.directory) / "manifest.json"), indent=2))
        elif args.command == "collect":
            from .remote import collect
            print(collect(args.directory))
        elif args.command == "resume":
            state = resume(args.directory, args.wait, args.retry_failed, until=args.until, only=args.only)
            return 1 if any(t["state"] in ("failed", "blocked") for t in state["tasks"].values()) else 0
        else:
            c = load_config(args.config, args.server)
            if args.command == "validate":
                p = build_plan(c)
                print(json.dumps({"residues": len(p["reference_mapping"]), "selected": len(p["selected"]),
                    "selected_original_ids": [p["reference_mapping"][i]["original_id"] for i in p["selected"]],
                    "backbone_lengths": [len(b["mapping"]) for b in p["backbones"]],
                    "expected_predictions": p["expected_predictions"], "warnings": p["warnings"]}, indent=2))
            elif args.command == "doctor":
                if args.remote and c["execution"]["transport"] in ("openssh", "ssh-skill") and not c["execution"].get("source_dir"):
                    from .remote import deploy_code
                    c["execution"]["source_dir"] = deploy_code(c)
                report = doctor(c, args.remote, stages=selected_stages(only=args.only))
                if args.report:
                    write_json(args.report, report)
                print(json.dumps(report, indent=2))
                return 0 if report["ready"] else 1
            else:
                plan = initialize(c, args.dry_run, stages=selected_stages(args.until))
                print(f"Plan: {c['output']}/plan.json; expected predictions: {plan['expected_predictions']}")
                if not args.dry_run:
                    state = resume(c["output"], args.wait, until=args.until)
                    return 1 if any(t["state"] in ("failed", "blocked") for t in state["tasks"].values()) else 0
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
