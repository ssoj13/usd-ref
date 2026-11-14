#!/usr/bin/env python3
"""
Orchestrator for usd-rs (usd-bench) and vfx-view (appbus::http REST API) benchmarks.

Runs tiered scenes from scenes.json safely (with --load-none, --mask, --max-rss-mb,
and per-scene timeouts), checks correctness against optional golden snapshots, and
writes JSON + Markdown reports into bench/results/.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = BENCH_DIR / "scenes.json"
DEFAULT_USD_BENCH = BENCH_DIR / "usd-bench" / "target" / "release" / (
    "usd-bench.exe" if os.name == "nt" else "usd-bench"
)
DEFAULT_VFX_EXE = Path(
    r"C:\projects\projects.rust.cg\cglibs\vfx-view\target\release\vfx.exe"
)
RESULTS_DIR = BENCH_DIR / "results"


def http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_usd_bench(scene: dict, usd_bench_exe: Path) -> dict:
    scene_path = ROOT / scene["path"]
    if not scene_path.exists():
        return {"id": scene["id"], "status": "missing", "error": f"file not found: {scene_path}"}

    cmd = [str(usd_bench_exe), str(scene_path)]
    if scene.get("load_none"):
        cmd.append("--load-none")
    if scene.get("mask"):
        cmd.extend(["--mask", scene["mask"]])
    if scene.get("eval_meshes"):
        cmd.append("--eval-meshes")
    if scene.get("max_rss_mb"):
        cmd.extend(["--max-rss-mb", str(scene["max_rss_mb"])])

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=scene.get("timeout_sec", 120),
        )
    except subprocess.TimeoutExpired:
        return {"id": scene["id"], "status": "timeout", "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2)}

    if proc.returncode != 0:
        return {
            "id": scene["id"],
            "status": "error",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[-1000:],
        }

    report = json.loads(proc.stdout)
    report["id"] = scene["id"]
    report["tier"] = scene.get("tier", 1)
    report["status"] = "aborted_rss" if report.get("aborted_rss_limit") else "ok"
    return report


def run_vfx_view_scene(scene: dict, vfx_exe: Path, port: int, renderer: str | None) -> dict:
    scene_path = ROOT / scene["path"]
    if not scene_path.exists():
        return {"id": scene["id"], "status": "missing", "error": f"file not found: {scene_path}"}

    cmd = [str(vfx_exe), "--clearsettings", "--api-port", str(port)]
    if scene.get("load_none"):
        cmd.append("--unloaded")
    if scene.get("mask"):
        cmd.extend(["--mask", scene["mask"]])
    if renderer:
        cmd.extend(["--renderer", renderer])
    cmd.append(str(scene_path))

    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    timeout = scene.get("timeout_sec", 120)
    t0 = time.perf_counter()
    try:
        # Wait for API server readiness
        ready = False
        while time.perf_counter() - t0 < 20.0:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                return {"id": scene["id"], "status": "crashed_startup", "stderr": err[-1000:]}
            try:
                if http_json(f"{base}/api/health", timeout=1.0).get("ok"):
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)

        if not ready:
            return {"id": scene["id"], "status": "api_timeout"}

        # Poll /api/stats until scene finishes loading and first frame is presented
        stats = {}
        while time.perf_counter() - t0 < timeout:
            if proc.poll() is not None:
                return {"id": scene["id"], "status": "crashed_during_load"}
            stats = http_json(f"{base}/api/stats", timeout=2.0)
            if stats.get("last_error"):
                return {"id": scene["id"], "status": "load_error", "error": stats["last_error"]}
            if not stats.get("loading") and stats.get("has_presented_frame"):
                break
            time.sleep(0.25)

        # Let a few frames settle for FPS measurement
        time.sleep(1.0)
        stats = http_json(f"{base}/api/stats", timeout=2.0)

        shot_path = RESULTS_DIR / f"{scene['id']}.png"
        try:
            http_json(f"{base}/api/screenshot", {"path": str(shot_path)}, timeout=15.0)
            stats["screenshot"] = str(shot_path)
        except Exception as e:
            stats["screenshot_error"] = str(e)

        try:
            http_json(f"{base}/api/quit", {}, timeout=2.0)
        except Exception:
            pass
        proc.wait(timeout=5)
        stats["id"] = scene["id"]
        stats["status"] = "ok"
        return stats
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def check_golden(results: list[dict], golden_path: Path) -> list[str]:
    if not golden_path.exists():
        return []
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    failures = []
    for got in results:
        sid = got["id"]
        exp = golden.get(sid)
        if not exp:
            continue
        if got.get("status") != "ok":
            failures.append(f"{sid}: non-ok status ({got.get('status')})")
            continue
        for key in ("prim_count", "mesh_count", "used_layers", "composition_error_count", "total_faces"):
            if key in exp and got.get(key) != exp[key]:
                failures.append(f"{sid}: {key} expected {exp[key]}, got {got.get(key)}")
    return failures


def format_markdown(usd_results: list[dict], vfx_results: list[dict]) -> str:
    lines = ["# USD Benchmark Report", ""]
    if usd_results:
        lines.extend([
            "## usd-rs (`usd-bench`)",
            "",
            "| ID | Mode | Status | Open (ms) | Traverse (ms) | Prims | Meshes | Layers | CompErr | RSS Δ (MB) |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for r in usd_results:
            lines.append(
                f"| {r.get('id')} | {r.get('load_mode', '-')} | {r.get('status')} | "
                f"{r.get('open_ms', '-')} | {r.get('traverse_ms', '-')} | "
                f"{r.get('prim_count', '-')} | {r.get('mesh_count', '-')} | "
                f"{r.get('used_layers', '-')} | {r.get('composition_error_count', '-')} | "
                f"{r.get('rss_delta_mb', '-')} |"
            )
        lines.append("")

    if vfx_results:
        lines.extend([
            "## vfx-view (`appbus::http` REST API)",
            "",
            "| ID | Status | Open (ms) | First Frame (ms) | FPS | Render (ms) | VRAM (MB) |",
            "|---|---|---:|---:|---:|---:|---:|",
        ])
        for r in vfx_results:
            vram = r.get("vram_used_mb")
            vram_str = f"{vram:.1f}" if isinstance(vram, (int, float)) else "-"
            lines.append(
                f"| {r.get('id')} | {r.get('status')} | "
                f"{r.get('open_ms', '-')} | {r.get('first_frame_ms', '-')} | "
                f"{round(r.get('fps', 0.0), 1) if 'fps' in r else '-'} | "
                f"{round(r.get('render_ms', 0.0), 2) if 'render_ms' in r else '-'} | {vram_str} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run usd-rs and vfx-view benchmarks on usd-ref.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--max-tier", type=int, default=2, help="Max scene tier to run (1=small, 2=ALab, 3=caldera)")
    parser.add_argument("--only", type=str, default=None, help="Run only scenes whose id contains this substring")
    parser.add_argument("--usd-bench", type=Path, default=DEFAULT_USD_BENCH)
    parser.add_argument("--vfx-exe", type=Path, default=DEFAULT_VFX_EXE)
    parser.add_argument("--with-vfx", action="store_true", help="Also launch vfx-view via --api-port")
    parser.add_argument("--api-port", type=int, default=18923)
    parser.add_argument("--renderer", type=str, default=None, help="Optional renderer override for vfx-view (storm|pt)")
    parser.add_argument("--update-golden", action="store_true", help="Save current usd-bench counts as golden.json")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    scenes = [
        s for s in manifest["scenes"]
        if s.get("tier", 1) <= args.max_tier and (args.only is None or args.only in s["id"])
    ]
    if not scenes:
        print("No matching scenes selected.", file=sys.stderr)
        return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    usd_results = []
    vfx_results = []

    if args.usd_bench.exists():
        for sc in scenes:
            print(f"[usd-bench] {sc['id']} ...", flush=True)
            usd_results.append(run_usd_bench(sc, args.usd_bench))
    else:
        print(f"[warn] usd-bench binary not found at {args.usd_bench}", file=sys.stderr)

    if args.with_vfx:
        if not args.vfx_exe.exists():
            print(f"[error] vfx.exe not found at {args.vfx_exe}", file=sys.stderr)
            return 1
        for sc in scenes:
            print(f"[vfx-view] {sc['id']} ...", flush=True)
            vfx_results.append(run_vfx_view_scene(sc, args.vfx_exe, args.api_port, args.renderer))

    golden_path = BENCH_DIR / "golden.json"
    if args.update_golden and usd_results:
        golden = {
            r["id"]: {
                k: r[k] for k in ("prim_count", "mesh_count", "used_layers", "composition_error_count", "total_faces")
                if k in r
            }
            for r in usd_results if r.get("status") == "ok"
        }
        golden_path.write_text(json.dumps(golden, indent=2), encoding="utf-8")

    failures = check_golden(usd_results, golden_path) if not args.update_golden else []

    out_json = RESULTS_DIR / "latest.json"
    out_md = RESULTS_DIR / "latest.md"
    out_json.write_text(json.dumps({"usd_rs": usd_results, "vfx_view": vfx_results}, indent=2), encoding="utf-8")
    md = format_markdown(usd_results, vfx_results)
    out_md.write_text(md, encoding="utf-8")
    print(md)

    if failures:
        print("GOLDEN MISMATCHES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
