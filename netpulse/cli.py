"""The ``netpulse`` command line.

Three kinds of command live here and the difference matters:

* **Local commands** (``check``, ``doctor``, ``eval``, ``replay``, ``train``,
  ``config``) work entirely on their own and never need an agent running.
  ``check`` in particular is the installer smoke test: it proves the whole
  path works on this machine and leaves no process behind.
* **Store commands** (``status``, ``export``, ``label``, ``incidents``) read
  and write the local database. They prefer a running agent when one is
  there, because it has warmer state, and fall back to the file when it is
  not.
* **Control commands** (``pause``, ``resume``, ``learning``, ``wipe``) change
  what a running agent is doing, so they go through the loopback API. When no
  agent is running they record the intent in the store instead, and the next
  agent to start picks it up.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, paths
from .config import NetPulseConfig, load_config, write_default_config
from .logging_setup import set_level, setup_logging

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_FAILED = 2

SEVERITY_WORDS = {
    "info": "healthy",
    "watch": "watch",
    "risk": "elevated risk",
    "critical": "trouble now",
}


# --------------------------------------------------------------- utilities


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, default=str))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, default=str))


def _open_store():
    from .store.db import Database
    from .store.repository import Repository

    db = Database(paths.database_file())
    return db, Repository(db)


def _api_call(config: NetPulseConfig, method: str, path: str, payload: Any = None) -> Any | None:
    """Call the local API. Returns None when no agent is listening."""
    import urllib.error
    import urllib.request

    from .api.server import TOKEN_HEADER

    try:
        token = paths.api_token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None

    url = f"http://{config.api.host}:{config.api.port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header(TOKEN_HEADER, token)
    request.add_header("Host", f"{config.api.host}:{config.api.port}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _severity_word(severity: str | None) -> str:
    return SEVERITY_WORDS.get(severity or "", "unknown")


def _format_status(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    health = summary.get("health")
    lines.append(
        f"Health      {health if health is not None else '--'}/100  "
        f"({_severity_word(summary.get('severity'))})"
    )
    if summary.get("warming_up"):
        lines.append("            still learning what normal looks like here")
    risk5 = summary.get("risk_5m")
    risk15 = summary.get("risk_15m")
    lines.append(f"Risk        {_pct(risk5)} in 5 min, {_pct(risk15)} in 15 min")
    lines.append(f"Coverage    {_pct(summary.get('coverage'))} of known signals")
    if summary.get("paused"):
        lines.append("Probing     paused")
    incident = summary.get("incident")
    if incident:
        lines.append("")
        lines.append(f"Incident    {incident['title']}")
        lines.append(f"            {incident['summary']}")
        for item in incident.get("remediation", [])[:3]:
            lines.append(f"            - {item}")
    else:
        lines.append("Incident    none")
    updated = summary.get("updated_at")
    if updated:
        lines.append("")
        lines.append(f"Updated     {time.strftime('%H:%M:%S', time.localtime(updated))}")
    return "\n".join(lines)


def _pct(value: Any) -> str:
    if value is None:
        return "--"
    return f"{float(value) * 100:.0f}%"


# ---------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    """Start the agent, and the local web UI unless asked not to."""
    from .api.server import serve, ui_url
    from .core.agent import Agent

    config = load_config(args.config)
    if args.headless:
        config = config.model_copy(update={"headless": True})
    write_default_config()

    agent = Agent(config)
    serve_ui = config.api.enabled and not args.no_api

    print(f"NetPulse Local {__version__}")
    print(f"  data      {paths.data_dir()}")
    print(f"  config    {paths.config_file()}")
    print(f"  collectors {', '.join(c.name for c in agent.collectors)}")
    if serve_ui:
        print(f"  web UI    {ui_url(config)}")
    print("  press Ctrl+C to stop")

    try:
        if serve_ui:
            agent.start(background=True)
            if config.api.open_browser:
                import webbrowser

                webbrowser.open(ui_url(config))
            serve(agent, config)
        else:
            agent.start(background=False)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        agent.stop()
        agent.db.close()
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    live = _api_call(config, "GET", "/api/health")
    if live is not None:
        _print(live if args.json else _format_status(live), args.json)
        return EXIT_OK

    db, repo = _open_store()
    try:
        score = repo.latest_score()
        if score is None:
            print("No readings yet. Start the agent with: netpulse run")
            return EXIT_OK
        incident = repo.active_incident()
        summary = {
            "health": round(score.health, 1),
            "severity": score.severity,
            "risk_5m": score.risk_5m,
            "risk_15m": score.risk_15m,
            "coverage": score.coverage,
            "warming_up": score.warming_up,
            "paused": db.kv_get("paused") == "1",
            "incident": incident.to_dict() if incident else None,
            "updated_at": score.ts,
            "source": "stored readings; no agent is running",
        }
        _print(summary if args.json else _format_status(summary), args.json)
        if not args.json:
            print("\n(no agent running; this is the last stored reading)")
    finally:
        db.close()
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """One collection round through the whole path. The smoke test."""
    from .core.agent import run_once

    config = load_config(args.config)
    started = time.perf_counter()
    summary = run_once(config)
    elapsed = time.perf_counter() - started
    if args.json:
        _print({**summary, "elapsed_s": round(elapsed, 2)}, True)
    else:
        print(_format_status(summary))
        print(f"\nOne full round took {elapsed:.1f}s")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what this machine can measure, and what it cannot."""
    from .collectors.netinfo import default_route
    from .collectors.registry import capability_report

    config = load_config(args.config)
    route = default_route()
    report = capability_report(config)
    payload = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "paths": {
            "config": str(paths.config_file()),
            "data": str(paths.data_dir()),
            "state": str(paths.state_dir()),
        },
        "route": route.as_dict(),
        "collectors": report,
    }
    if args.json:
        _print(payload, True)
        return EXIT_OK

    print(f"NetPulse Local {__version__} on {sys.platform}, Python {sys.version.split()[0]}")
    print(f"  config   {paths.config_file()}")
    print(f"  data     {paths.data_dir()}")
    print()
    print("Network")
    print(f"  interface  {route.interface or 'unknown'}")
    print(f"  gateway    {route.gateway or 'not found'}")
    print(f"  VPN        {'yes, ' + str(route.vpn_interface) if route.is_vpn else 'no'}")
    print()
    print("Collectors")
    unsupported = 0
    for item in report:
        capability = item["capability"]
        if not item["enabled"]:
            mark, note = "off ", "switched off in config"
        elif capability["supported"]:
            mark, note = "ok  ", capability["reason"] or f"every {item['interval_s']:.0f}s"
        else:
            mark, note = "none", capability["reason"]
            unsupported += 1
        print(f"  [{mark}] {item['name']:<9} {item['layer']:<13} {note}")
    if unsupported:
        print(f"\n{unsupported} collector(s) unavailable. The rest still work.")
    return EXIT_OK


def cmd_pause(args: argparse.Namespace) -> int:
    return _toggle_probing(args, pause=True)


def cmd_resume(args: argparse.Namespace) -> int:
    return _toggle_probing(args, pause=False)


def _toggle_probing(args: argparse.Namespace, *, pause: bool) -> int:
    config = load_config(args.config)
    result = _api_call(config, "POST", "/api/pause" if pause else "/api/resume", {})
    if result is not None:
        print("Probing paused." if pause else "Probing resumed.")
        return EXIT_OK
    db, _ = _open_store()
    try:
        db.kv_set("paused", "1" if pause else "0")
    finally:
        db.close()
    print(
        ("Probing will be paused" if pause else "Probing will resume")
        + " when the agent next starts (no agent is running now)."
    )
    return EXIT_OK


def cmd_learning(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    enabled = args.state == "on"
    result = _api_call(config, "POST", "/api/learning", {"enabled": enabled})
    if result is None:
        db, _ = _open_store()
        try:
            db.kv_set("learning", "1" if enabled else "0")
        finally:
            db.close()
    print(f"Learning {'enabled' if enabled else 'paused'}.")
    return EXIT_OK


def cmd_label(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    payload = {
        "verdict": args.verdict,
        "note": args.note or "",
        "window_minutes": args.minutes,
    }
    result = _api_call(config, "POST", "/api/label", payload)
    if result is None:
        from .store.models import Label

        db, repo = _open_store()
        try:
            now = time.time()
            active = repo.active_incident()
            repo.add_label(
                Label(
                    ts=now,
                    verdict=args.verdict,
                    window_start=now - args.minutes * 60.0,
                    window_end=now,
                    note=args.note or "",
                    incident_id=active.id if active else None,
                )
            )
        finally:
            db.close()
    print(f"Recorded: the last {args.minutes:.0f} minutes were '{args.verdict}'.")
    return EXIT_OK


def cmd_incidents(args: argparse.Namespace) -> int:
    db, repo = _open_store()
    try:
        incidents = repo.recent_incidents(limit=args.limit)
        if args.json:
            _print([incident.to_dict() for incident in incidents], True)
            return EXIT_OK
        if not incidents:
            print("No incidents recorded.")
            return EXIT_OK
        for incident in incidents:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(incident.started_at))
            state = "ongoing" if incident.ended_at is None else "resolved"
            print(f"{when}  {incident.severity:<8} {incident.primary_layer:<13} {incident.title}")
            print(f"{'':18}{incident.summary}")
            print(f"{'':18}{state}, confidence {incident.confidence:.0%}")
            if incident.user_label:
                print(f"{'':18}you said: {incident.user_label}")
            print()
    finally:
        db.close()
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    from .export.bundle import audit_bundle, build_bundle

    config = load_config(args.config)
    target = (
        Path(args.output)
        if args.output
        else paths.data_dir() / "exports" / f"netpulse-bundle-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    )
    db, repo = _open_store()
    try:
        path = build_bundle(
            repo, config, target, window_s=args.hours * 3600.0, include_logs=not args.no_logs
        )
    finally:
        db.close()
    audit = audit_bundle(path)
    if args.json:
        _print({"path": str(path), "bytes": path.stat().st_size, "audit": audit}, True)
        return EXIT_OK
    print(f"Wrote {path} ({path.stat().st_size / 1024:.0f} KB)")
    print("Privacy audit:", "clean" if audit["ok"] else f"issues: {audit['findings']}")
    print("This contains no packet contents. Read README.txt inside before sharing it.")
    return EXIT_OK if audit["ok"] else EXIT_ERROR


def cmd_wipe(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input("Delete every observation stored on this machine? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing was deleted.")
            return EXIT_OK
    config = load_config(args.config)
    if _api_call(config, "POST", "/api/wipe", {}) is None:
        db, _ = _open_store()
        try:
            db.wipe()
        finally:
            db.close()
    print("All stored observations deleted.")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    if args.init:
        path = write_default_config()
        print(f"Wrote a starter config to {path}")
        return EXIT_OK
    config = load_config(args.config)
    if args.json:
        _print(config.redacted(), True)
        return EXIT_OK
    print(f"Config file: {paths.config_file()}")
    print(f"  exists: {paths.config_file().exists()}")
    print()
    print(json.dumps(config.redacted(), indent=2))
    return EXIT_OK


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval.metrics import format_report
    from .eval.replay import replay_corpus

    config = load_config(args.config)
    report = replay_corpus(
        config=config,
        seeds=tuple(args.seeds),
        include_soak=not args.no_soak,
        soak_hours=args.soak_hours,
    )
    if args.json:
        _print(report.to_dict(), True)
    else:
        print(format_report(report))
    if args.gate and not report.passed():
        print("\nGate failed: one or more PRD targets regressed.", file=sys.stderr)
        return EXIT_GATE_FAILED
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay a captured JSONL of samples through the live scoring path."""
    from .eval.replay import load_jsonl
    from .ml.scorer import Scorer

    config = load_config(args.config)
    samples = load_jsonl(Path(args.path))
    if not samples:
        print("No samples found in that file.", file=sys.stderr)
        return EXIT_ERROR

    scorer = Scorer(config)
    incidents: list[dict[str, Any]] = []
    lowest = 100.0
    for sample in samples:
        result = scorer.observe(sample)
        lowest = min(lowest, result.score.health)
        if result.opened is not None:
            incidents.append(
                {
                    "at": result.opened.started_at,
                    "layer": result.opened.primary_layer,
                    "title": result.opened.title,
                    "severity": result.opened.severity,
                }
            )
    payload = {
        "samples": len(samples),
        "span_hours": round((samples[-1].ts - samples[0].ts) / 3600.0, 2),
        "lowest_health": round(lowest, 1),
        "incidents": incidents,
    }
    if args.json:
        _print(payload, True)
        return EXIT_OK
    print(f"Replayed {len(samples)} samples over {payload['span_hours']} hours")
    print(f"Lowest health: {payload['lowest_health']}")
    if not incidents:
        print("No incidents would have been raised.")
    for incident in incidents:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(incident["at"]))
        print(f"  {when}  {incident['severity']:<8} {incident['layer']:<13} {incident['title']}")
    return EXIT_OK


def cmd_train(args: argparse.Namespace) -> int:
    from .eval.train import train_and_save

    target = Path(args.output) if args.output else None
    path, metrics = train_and_save(path=target, epochs=args.epochs)
    if args.json:
        _print({"path": str(path), "metrics": metrics}, True)
        return EXIT_OK
    print(f"Wrote {path}")
    for horizon in ("h5", "h15"):
        entry = metrics.get(horizon)
        if not entry:
            continue
        print(
            f"  {horizon}: brier {entry['brier']:.4f}  calibration error {entry['ece']:.4f}  "
            f"precision@0.5 {entry['at_0.5']['precision']:.2f}  "
            f"recall@0.5 {entry['at_0.5']['recall']:.2f}"
        )
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    """Replay a recorded fault against a live agent and its web UI.

    Waiting for a real network to break is a poor way to find out whether
    this works. The collectors are switched off and their measurements come
    from the corpus; everything downstream of that is the product.
    """
    import os
    import tempfile

    from .eval.demo import DemoDriver, demo_config, resolve_scenario, scenario_names

    if args.list:
        print("Scenarios you can replay:")
        for name in scenario_names():
            print(f"  {name}")
        return EXIT_OK

    # A scratch home by default, so a demo never mixes recorded faults into
    # the real history of the machine it runs on.
    if not args.keep:
        os.environ["NETPULSE_HOME"] = tempfile.mkdtemp(prefix="netpulse-demo-")

    try:
        run = resolve_scenario(args.scenario)
    except ValueError as exc:
        print(f"netpulse: {exc}", file=sys.stderr)
        return EXIT_ERROR

    from .api.server import serve, ui_url
    from .core.agent import Agent

    config = demo_config(load_config(args.config))
    agent = Agent(config)
    serve_ui = config.api.enabled and not args.no_api
    # The demo narrates itself; agent log lines would interleave with the
    # progress line and duplicate the incident card.
    set_level("WARNING")
    interactive = sys.stdout.isatty()

    print(f"NetPulse Local {__version__}   demo: {run.name}")
    print(f"  {run.description}")
    print(
        f"  {len(run.samples)} samples, {len(run.samples) * 15 / 60:.0f} minutes "
        f"of recording at {args.speed:g}x"
    )
    if serve_ui:
        print(f"  web UI    {ui_url(config)}")
    print("  collectors are off; every measurement below comes from the recording")
    print("  press Ctrl+C to stop")
    print()

    def on_frame(index: int, result: Any) -> None:
        score = result.score
        if result.opened is not None:
            stamp = time.strftime("%H:%M:%S", time.localtime())
            print()
            print(f"  [{stamp}] {score.severity.upper()}  {result.opened.title}")
            print(f"             {result.opened.summary}")
            for item in result.opened.remediation[:2]:
                print(f"             - {item}")
            if run.impact_start is not None:
                lead = (run.impact_start - run.samples[index].ts) / 60.0
                if lead > 0:
                    print(f"             that is {lead:.1f} minutes before it actually breaks")
            print()
        elif index % (6 if interactive else 40) == 0:
            line = (
                f"  health {score.health:5.1f} {_sparkbar(score.health)}  "
                f"risk 15m {score.risk_15m * 100:3.0f}%   {score.severity:<9}"
            )
            # A carriage return repaints one line in a terminal and produces
            # an unreadable smear once the output is piped or captured.
            sys.stdout.write(("\r" + line) if interactive else (line + "\n"))
            sys.stdout.flush()

    driver = DemoDriver(agent, run, speed=args.speed, loop=args.loop, on_frame=on_frame)

    try:
        driver.start()
        if serve_ui:
            serve(agent, config)
        else:
            driver.wait()
            print()
    except KeyboardInterrupt:
        print()
        print("stopping")
    finally:
        driver.stop()
        agent.stop()
        agent.db.close()
    return EXIT_OK


def _sparkbar(health: float, width: int = 22) -> str:
    filled = int(max(0.0, min(100.0, health)) / 100 * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        _print({"version": __version__, "python": sys.version.split()[0]}, True)
    else:
        print(f"netpulse {__version__} (Python {sys.version.split()[0]})")
    return EXIT_OK


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netpulse",
        description=(
            "NetPulse Local: predicts network trouble minutes ahead and explains "
            "which layer is failing, without sending anything off this machine."
        ),
    )
    parser.add_argument("--config", help="path to a config file", default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--log-level", default="INFO", help="console log level (DEBUG, INFO, WARNING)"
    )

    # The same three flags are accepted after the subcommand as well, because
    # `netpulse check --json` is what people actually type and being told it
    # is an unrecognised argument is a poor answer. SUPPRESS is what makes
    # both positions work: without it the subparser would write its own
    # default back over a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output"
    )
    common.add_argument("--log-level", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the agent and the local web UI", parents=[common])
    run.add_argument("--headless", action="store_true", help="server mode, no browser prompt")
    run.add_argument("--no-api", action="store_true", help="do not serve the web UI")
    run.set_defaults(func=cmd_run)

    status = subparsers.add_parser(
        "status", help="show the current health reading", parents=[common]
    )
    status.set_defaults(func=cmd_status)

    check = subparsers.add_parser(
        "check", help="run one collection round and print the result, then exit", parents=[common]
    )
    check.set_defaults(func=cmd_check)

    doctor = subparsers.add_parser(
        "doctor", help="report what this machine can measure", parents=[common]
    )
    doctor.set_defaults(func=cmd_doctor)

    pause = subparsers.add_parser("pause", help="stop sending probes", parents=[common])
    pause.set_defaults(func=cmd_pause)

    resume = subparsers.add_parser("resume", help="start sending probes again", parents=[common])
    resume.set_defaults(func=cmd_resume)

    learning = subparsers.add_parser(
        "learning", help="pause or resume model learning", parents=[common]
    )
    learning.add_argument("state", choices=("on", "off"))
    learning.set_defaults(func=cmd_learning)

    label = subparsers.add_parser(
        "label", help="tell the agent whether it was right", parents=[common]
    )
    label.add_argument("verdict", choices=("bad", "ok", "unsure"))
    label.add_argument("note", nargs="?", default="", help="optional note")
    label.add_argument("--minutes", type=float, default=15.0, help="window this covers")
    label.set_defaults(func=cmd_label)

    incidents = subparsers.add_parser("incidents", help="list recent incidents", parents=[common])
    incidents.add_argument("--limit", type=int, default=20)
    incidents.set_defaults(func=cmd_incidents)

    export = subparsers.add_parser(
        "export", help="write a redacted diagnostic bundle", parents=[common]
    )
    export.add_argument("--output", "-o", help="where to write the zip")
    export.add_argument("--hours", type=float, default=24.0)
    export.add_argument("--no-logs", action="store_true", help="leave agent logs out")
    export.set_defaults(func=cmd_export)

    wipe = subparsers.add_parser("wipe", help="delete every stored observation", parents=[common])
    wipe.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    wipe.set_defaults(func=cmd_wipe)

    config_cmd = subparsers.add_parser(
        "config", help="show or create the configuration", parents=[common]
    )
    config_cmd.add_argument("--init", action="store_true", help="write a starter config file")
    config_cmd.set_defaults(func=cmd_config)

    evaluate = subparsers.add_parser(
        "eval", help="replay the fault-injection corpus and report the metrics", parents=[common]
    )
    evaluate.add_argument("--gate", action="store_true", help="exit non-zero if a target regressed")
    evaluate.add_argument("--seeds", type=int, nargs="+", default=[11])
    evaluate.add_argument("--no-soak", action="store_true", help="skip the long quiet run")
    evaluate.add_argument("--soak-hours", type=float, default=8.0)
    evaluate.set_defaults(func=cmd_eval)

    replay = subparsers.add_parser(
        "replay", help="replay a captured samples.jsonl", parents=[common]
    )
    replay.add_argument("path")
    replay.set_defaults(func=cmd_replay)

    train = subparsers.add_parser("train", help="retrain the predictive head", parents=[common])
    train.add_argument("--output", "-o", help="where to write the model bundle")
    train.add_argument("--epochs", type=int, default=600)
    train.set_defaults(func=cmd_train)

    demo = subparsers.add_parser(
        "demo",
        help="replay a recorded fault against a live agent and the web UI",
        parents=[common],
    )
    demo.add_argument(
        "scenario", nargs="?", default=None, help="which fault to replay (default: wifi_fade)"
    )
    demo.add_argument("--speed", type=float, default=30.0, help="playback speed, default 30x")
    demo.add_argument("--loop", action="store_true", help="replay continuously")
    demo.add_argument("--no-api", action="store_true", help="terminal only, no web UI")
    demo.add_argument("--list", action="store_true", help="list the scenarios and exit")
    demo.add_argument(
        "--keep",
        action="store_true",
        help="use the real data directory instead of a scratch one",
    )
    demo.set_defaults(func=cmd_demo)

    version = subparsers.add_parser("version", help="print the version", parents=[common])
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Commands that print a report keep the console quiet; the agent itself
    # logs normally because its output is the point.
    quiet = args.command in (
        "status",
        "check",
        "doctor",
        "incidents",
        "config",
        "version",
        "export",
        "replay",
    )
    setup_logging(
        "WARNING" if (quiet or args.json) else args.log_level,
        to_file=args.command == "run",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:  # a CLI should fail with a sentence, not a traceback
        if args.log_level.upper() == "DEBUG":
            raise
        print(f"netpulse: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
