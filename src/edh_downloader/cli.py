"""Command line: ``edh-dl serve`` for the web UI, plus headless commands for scripting and servers."""

import argparse
import asyncio
import getpass
import json
import sys

from .util import human_bytes, human_duration


def _p(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_worker(a):
    from .runner import run_foreground

    sys.exit(0 if run_foreground(a.job) in ("completed", "paused", "paused_quota", "cancelled") else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edh-dl", description="Earth Data Hub dataset downloader")
    from . import __version__

    ap.add_argument("--version", action="version", version=f"edh-dl {__version__}")
    ap.add_argument("--no-update", action="store_true", help="do not check for a newer release at start")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("update", help="check for a newer release and install it (packaged executable only)")
    x = sub.add_parser("app", help="on your computer: connect to a server over SSH and use it from here "
                                   "(the default when run without a command)")
    x.add_argument("--port", type=int, default=0); x.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("serve", help="start the web UI (opens the browser; over SSH see `connect`)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-token", action="store_true", help="disable the access token (not recommended)")
    s.add_argument("--no-browser", action="store_true", help="do not open a browser (when run on your own computer)")
    s.add_argument("--stdio", action="store_true", help=argparse.SUPPRESS)  # used by the launcher
    x = sub.add_parser("connect", help="from your computer: open the UI of edh-dl on a server (SSH tunnel + browser)")
    x.add_argument("host", help="user@server, as for ssh"); x.add_argument("--cmd", dest="cmd_", default="edh-dl",
                                                                       help="path of edh-dl on the server")
    x = sub.add_parser("launcher", help="print a launcher script for your computer (needs only ssh)")
    x.add_argument("kind", choices=["sh", "ps1"], help="sh: macOS/Linux, ps1: Windows")
    x.add_argument("--host", help="user@server (default: as seen from this SSH session)")

    k = sub.add_parser("keys", help="manage API keys")
    ks = k.add_subparsers(dest="kcmd", required=True)
    ks.add_parser("list")
    x = ks.add_parser("add"); x.add_argument("name"); x.add_argument("--quota", type=int, default=500_000)
    x.add_argument("--default", action="store_true"); x.add_argument("--no-check", action="store_true")
    x = ks.add_parser("remove"); x.add_argument("name")
    x = ks.add_parser("default"); x.add_argument("name")
    x = ks.add_parser("quota", help="set the monthly request quota of a key"); x.add_argument("name"); x.add_argument("quota", type=int)
    x = ks.add_parser("calibrate", help="set this month's usage as shown on the EDH quota page")
    x.add_argument("name"); x.add_argument("used", type=int)
    x = ks.add_parser("test", help="check a key (1 request)"); x.add_argument("name", nargs="?")

    x = sub.add_parser("datasets", help="list datasets of a collection"); x.add_argument("--collection", default="era5")
    x = sub.add_parser("variables", help="list variables of a dataset (a few requests, cached)")
    x.add_argument("dataset"); x.add_argument("--collection", default="era5")
    x = sub.add_parser("plan", help="show what a job file would download"); x.add_argument("job_file")
    x.add_argument("--sample", type=int, nargs="?", const=12, metavar="N",
                   help="measure sizes and speed on N real chunks of the job (USES N REQUESTS of your quota)")
    x.add_argument("--yes", action="store_true", help="do not ask before using quota")
    x = sub.add_parser("run", help="create a job from a job file and run it")
    x.add_argument("job_file"); x.add_argument("--detach", action="store_true", help="run in the background")
    sub.add_parser("jobs", help="list jobs")
    for name in ("status", "pause", "resume", "cancel", "log"):
        x = sub.add_parser(name); x.add_argument("job")
        if name == "resume":
            x.add_argument("--foreground", action="store_true")
            x.add_argument("--connections", type=int)
    x = sub.add_parser("export", help="export a job's Icechunk repository to plain Zarr"); x.add_argument("job")
    x = sub.add_parser("_worker"); x.add_argument("job")
    sub.add_parser("agent", help=argparse.SUPPRESS)  # used by the app over SSH
    a = ap.parse_args(argv)

    if a.cmd == "update":
        from . import update

        if not update.can_self_update():
            sys.exit("self-update works only in the packaged executable (from source: git pull / reinstall)")
        if update.check_and_update(force_notice=True):
            print("updated; run edh-dl again")
        return
    if a.cmd in (None, "app"):
        if not a.no_update:
            from . import update

            update.auto_update(sys.argv[1:] if argv is None else list(argv))
        from .gui.connect import serve_local

        return serve_local(getattr(a, "port", 0), open_browser=not getattr(a, "no_browser", False))
    if a.cmd == "_worker":
        return cmd_worker(a)
    if a.cmd == "agent":
        from .agent import main as agent_main

        return agent_main()
    if a.cmd == "serve":
        from .gui.app import serve

        return serve(a.host, a.port, token=not a.no_token, stdio=a.stdio, open_browser=not a.no_browser)
    if a.cmd == "connect":
        from .launcher import connect

        sys.exit(connect(a.host, a.cmd_))
    if a.cmd == "launcher":
        from .launcher import script

        sys.stdout.write(script(a.kind, a.host))
        return
    if a.cmd == "keys":
        from . import keys, quota

        if a.kcmd == "list":
            d = keys.default_name()
            for key in keys.list_keys():
                q = quota.info(key.fingerprint)
                print(f"{'*' if key.name == d else ' '} {key.name:<16} {key.source:<7} id {key.fingerprint}  "
                      f"used {q['used']}/{key.quota} this month")
        elif a.kcmd == "add":
            secret = getpass.getpass("API key: ")
            if not a.no_check:
                ok, msg = asyncio.run(keys.validate(secret))
                print(msg)
                if not ok:
                    sys.exit(1)
            keys.add_key(a.name, secret, a.quota, a.default)
            print(f"saved key {a.name}")
        elif a.kcmd == "remove":
            keys.remove_key(a.name)
        elif a.kcmd == "default":
            keys.set_default(a.name)
        elif a.kcmd == "quota":
            keys.set_quota(a.name, a.quota)
        elif a.kcmd == "calibrate":
            quota.calibrate(keys.get_key(a.name).fingerprint, a.used)
        elif a.kcmd == "test":
            key = keys.get_key(a.name)
            ok, msg = asyncio.run(keys.validate(key.secret))
            quota.add(key.fingerprint, 1)
            print(msg)
            sys.exit(0 if ok else 1)
        return
    if a.cmd == "datasets":
        from . import catalog

        for d in catalog.datasets(a.collection):
            print(f"{d['id']:<48} {d['size']:>9}  {d['title']}")
        return
    if a.cmd == "variables":
        from . import catalog, keys
        from .http import RequestCounter

        key = keys.get_key()
        info = catalog.find_dataset(a.dataset, a.collection)
        ds = catalog.load_dataset(info["url"], key.secret, counter=RequestCounter(key.fingerprint), info=info)
        _p(ds.summary())
        for v in ds.variables.values():
            print(f"{v.name:<10} {v.kind:<11} {v.units:<14} {v.long_name}")
        return
    if a.cmd == "plan":
        from . import catalog, keys
        from .http import RequestCounter
        from .job import JobSpec
        from .planner import Plan

        spec = JobSpec.load(a.job_file)
        key = keys.get_key(spec.download.key)
        info = catalog.dataset_info(spec)
        ds = catalog.load_dataset(info["url"], key.secret, counter=RequestCounter(key.fingerprint), info=info)
        plan = Plan(spec, ds)
        s = plan.summary()
        s.pop("download_bytes_est"); s.pop("output_bytes_uncompressed"); s.pop("raw_bytes"); s.pop("eta_s")
        from . import chunking, sample

        if a.sample:
            n = min(a.sample, plan.n_requests)
            if not a.yes and input(f"This uses {n} requests of your quota (key '{key.name}'). Continue? [y/N] ").strip().lower() != "y":
                return
            r = asyncio.run(sample.run(plan, key.secret, RequestCounter(key.fingerprint), n=n))
            print(f"suggested connections: {r['suggested']}")
        est = sample.estimate(plan, sample.last_result(ds.url), spec.download.connections, chunking.compression_ratio(plan))
        s["download"] = human_bytes(est["download"]) + ("" if est["download_measured"] else " (estimate)")
        s["zarr_size"] = human_bytes(est["zarr"]) + ("" if est["zarr_measured"] else " (estimate)")
        s["uncompressed"] = human_bytes(est["raw"])
        if est["eta_s"]:
            s["time"] = human_duration(est["eta_s"])
        _p(s)
        return

    from . import runner

    if a.cmd == "run":
        from .job import JobSpec

        job = runner.create_job(JobSpec.load(a.job_file))
        print(f"job {job.name}")
        if a.detach:
            print(f"started in the background (pid {runner.launch(job)}); follow with: edh-dl log {job.name}")
        else:
            sys.exit(0 if runner.run_foreground(job, verbose=True) == "completed" else 1)
    elif a.cmd == "jobs":
        for s in runner.list_jobs():
            u = f"{len(s.get('units_done', []))}/{s.get('units_total', '?')}"
            print(f"{s['id']:<40} {s['status']:<13} units {u:<10} eta {human_duration(s.get('eta_s'))}")
    elif a.cmd == "status":
        s = runner.status(runner.resolve(a.job))
        s.pop("units_done", None)
        _p(s)
    elif a.cmd == "pause":
        print("stopping after the current unit" if runner.stop(runner.resolve(a.job), "pause") else "not running")
    elif a.cmd == "cancel":
        runner.stop(runner.resolve(a.job), "cancel")
        print("cancelled")
    elif a.cmd == "resume":
        job = runner.resolve(a.job)
        if a.connections:
            runner.set_control(job, connections=a.connections)
        if a.foreground:
            sys.exit(0 if runner.run_foreground(job, verbose=True) == "completed" else 1)
        print(f"resumed in the background (pid {runner.launch(job)})")
    elif a.cmd == "log":
        print((runner.resolve(a.job) / "log.txt").read_text())
    elif a.cmd == "export":
        from . import store
        from .job import JobSpec

        s = runner.status(runner.resolve(a.job))
        spec = JobSpec.from_dict(s["spec"])
        repo = store.open_repo(spec.icechunk_path)
        if repo is None:
            sys.exit(f"no Icechunk repository at {spec.icechunk_path}")
        print(f"exported {store.export_zarr(repo, spec.zarr_path)} objects to {spec.zarr_path}")


if __name__ == "__main__":
    main()
