//! Headless speed, memory, and correctness benchmark harness for `usd-rs`.
//!
//! Opens a USD stage (with optional `InitialLoadSet::LoadNone`, `StagePopulationMask`,
//! incremental payload loading, and RSS memory guardrails) and emits structured JSON
//! metrics covering open/traverse timings, prim/mesh/payload/layer counts, composition
//! errors, and memory delta.

use serde::Serialize;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::process;
use std::time::Instant;
use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System};
use usd_core::{InitialLoadSet, LoadPolicy, Stage, StagePopulationMask};
use usd_geom::Mesh;
use usd_sdf::{Path as SdfPath, TimeCode};

#[derive(Debug, Serialize)]
struct BenchReport {
    scene: String,
    load_mode: String,
    masked_paths: Vec<String>,
    loaded_paths: Vec<String>,
    avail_ram_mb_start: u64,
    rss_mb_before: f64,
    rss_mb_after_open: f64,
    rss_mb_after_load: f64,
    rss_mb_after_traverse: f64,
    rss_delta_mb: f64,
    open_ms: f64,
    load_ms: f64,
    traverse_ms: f64,
    eval_ms: f64,
    used_layers: usize,
    prim_count: usize,
    mesh_count: usize,
    skel_count: usize,
    material_count: usize,
    instance_count: usize,
    payload_prim_count: usize,
    unloaded_payload_count: usize,
    total_faces: u64,
    time_sampled_meshes: usize,
    prim_types: BTreeMap<String, usize>,
    composition_error_count: usize,
    composition_errors_sample: Vec<String>,
    aborted_rss_limit: bool,
}

struct CliArgs {
    scene: String,
    load_none: bool,
    masks: Vec<String>,
    load_paths: Vec<String>,
    eval_meshes: bool,
    sample_time: Option<f64>,
    max_rss_mb: Option<f64>,
    out_file: Option<String>,
}

fn print_help() {
    eprintln!(
        "usd-bench — headless usd-rs benchmark & correctness harness\n\
         Usage: usd-bench <scene.usd*> [OPTIONS]\n\
         Options:\n\
           --load-none            Open stage with InitialLoadSet::LoadNone (defer payloads)\n\
           --mask <prim_path>     Restrict stage population to <prim_path> (repeatable)\n\
           --load-path <path>     Incrementally load payload at <path> after open (repeatable)\n\
           --eval-meshes          Evaluate Mesh face counts & timeSamples during traverse\n\
           --sample-time <t>      TimeCode for evaluation (default: default TimeCode)\n\
           --max-rss-mb <mb>      Abort traversal/eval if process RSS exceeds <mb> MiB\n\
           --out <file.json>      Write JSON report to file instead of stdout\n\
           -h, --help             Show this help"
    );
}

fn parse_args() -> Result<CliArgs, String> {
    let mut args = env::args().skip(1);
    let mut scene = None;
    let mut load_none = false;
    let mut masks = Vec::new();
    let mut load_paths = Vec::new();
    let mut eval_meshes = false;
    let mut sample_time = None;
    let mut max_rss_mb = None;
    let mut out_file = None;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_help();
                process::exit(0);
            }
            "--load-none" => load_none = true,
            "--eval-meshes" => eval_meshes = true,
            "--mask" => {
                let v = args.next().ok_or("--mask requires a prim path")?;
                masks.push(v);
            }
            "--load-path" => {
                let v = args.next().ok_or("--load-path requires a prim path")?;
                load_paths.push(v);
            }
            "--sample-time" => {
                let v = args.next().ok_or("--sample-time requires a float")?;
                let t: f64 = v.parse().map_err(|_| format!("invalid --sample-time: {v}"))?;
                sample_time = Some(t);
            }
            "--max-rss-mb" => {
                let v = args.next().ok_or("--max-rss-mb requires a number")?;
                let mb: f64 = v.parse().map_err(|_| format!("invalid --max-rss-mb: {v}"))?;
                max_rss_mb = Some(mb);
            }
            "--out" => {
                let v = args.next().ok_or("--out requires a file path")?;
                out_file = Some(v);
            }
            s if s.starts_with('-') => return Err(format!("unknown flag: {s}")),
            s => {
                if scene.is_some() {
                    return Err(format!("unexpected extra positional arg: {s}"));
                }
                scene = Some(s.to_string());
            }
        }
    }

    let scene = scene.ok_or_else(|| {
        print_help();
        "missing <scene.usd*> argument".to_string()
    })?;

    Ok(CliArgs {
        scene,
        load_none,
        masks,
        load_paths,
        eval_meshes,
        sample_time,
        max_rss_mb,
        out_file,
    })
}

fn current_rss_mb(sys: &mut System, pid: Pid) -> f64 {
    sys.refresh_processes_specifics(
        ProcessesToUpdate::Some(&[pid]),
        false,
        ProcessRefreshKind::nothing().with_memory(),
    );
    sys.process(pid)
        .map(|p| p.memory() as f64 / (1024.0 * 1024.0))
        .unwrap_or(0.0)
}

fn main() {
    let cli = match parse_args() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("error: {e}");
            process::exit(2);
        }
    };

    let mut sys = System::new();
    sys.refresh_memory();
    let avail_ram_mb_start = sys.available_memory() / (1024 * 1024);
    let pid = Pid::from_u32(process::id());
    let rss_before = current_rss_mb(&mut sys, pid);

    let load_set = if cli.load_none {
        InitialLoadSet::LoadNone
    } else {
        InitialLoadSet::LoadAll
    };

    let t_open = Instant::now();
    let stage = if cli.masks.is_empty() {
        Stage::open(&cli.scene, load_set)
    } else {
        let mut mask = StagePopulationMask::new();
        for m in &cli.masks {
            match SdfPath::from_string(m) {
                Some(p) => {
                    mask.add(p);
                }
                None => {
                    eprintln!("invalid --mask path '{m}'");
                    process::exit(2);
                }
            }
        }
        Stage::open_masked(&cli.scene, mask, load_set)
    };

    let stage = match stage {
        Ok(s) => s,
        Err(e) => {
            eprintln!("failed to open '{}': {e}", cli.scene);
            process::exit(1);
        }
    };
    let open_ms = t_open.elapsed().as_secs_f64() * 1000.0;
    let rss_after_open = current_rss_mb(&mut sys, pid);

    let t_load = Instant::now();
    for lp in &cli.load_paths {
        match SdfPath::from_string(lp) {
            Some(p) => {
                let _ = stage.load(&p, Some(LoadPolicy::LoadWithDescendants));
            }
            None => {
                eprintln!("invalid --load-path '{lp}'");
                process::exit(2);
            }
        }
    }
    let load_ms = if cli.load_paths.is_empty() {
        0.0
    } else {
        t_load.elapsed().as_secs_f64() * 1000.0
    };
    let rss_after_load = current_rss_mb(&mut sys, pid);

    let time_code = cli
        .sample_time
        .map(TimeCode::new)
        .unwrap_or_else(TimeCode::default);

    let t_trav = Instant::now();
    let mut prim_count = 0usize;
    let mut mesh_count = 0usize;
    let mut skel_count = 0usize;
    let mut material_count = 0usize;
    let mut instance_count = 0usize;
    let mut payload_prim_count = 0usize;
    let mut unloaded_payload_count = 0usize;
    let mut total_faces = 0u64;
    let mut time_sampled_meshes = 0usize;
    let mut prim_types: BTreeMap<String, usize> = BTreeMap::new();
    let mut eval_elapsed_secs = 0.0f64;
    let mut aborted_rss_limit = false;

    for prim in stage.traverse() {
        prim_count += 1;
        let tname = prim.type_name().as_str().to_string();
        if !tname.is_empty() {
            *prim_types.entry(tname.clone()).or_insert(0) += 1;
        }
        if prim.is_instance() {
            instance_count += 1;
        }
        if prim.has_payload() {
            payload_prim_count += 1;
            if !prim.is_loaded() {
                unloaded_payload_count += 1;
            }
        }
        match tname.as_str() {
            "Mesh" => {
                mesh_count += 1;
                if cli.eval_meshes {
                    let t_ev = Instant::now();
                    let mesh = Mesh::get(&stage, prim.path());
                    total_faces += mesh.get_face_count(time_code) as u64;
                    if mesh.get_face_vertex_counts_attr().get_num_time_samples() > 0 {
                        time_sampled_meshes += 1;
                    }
                    eval_elapsed_secs += t_ev.elapsed().as_secs_f64();
                }
            }
            "SkelRoot" | "Skeleton" => skel_count += 1,
            "Material" => material_count += 1,
            _ => {}
        }

        // Check RSS periodically so huge stages stop cleanly before starving the OS.
        if let Some(limit) = cli.max_rss_mb
            && prim_count % 2000 == 0
        {
            let rss = current_rss_mb(&mut sys, pid);
            if rss > limit {
                aborted_rss_limit = true;
                break;
            }
        }
    }
    let traverse_ms = t_trav.elapsed().as_secs_f64() * 1000.0;
    let rss_after_traverse = current_rss_mb(&mut sys, pid);

    let used_layers = stage.used_layers().len();
    let comp_errors = stage.get_composition_errors();
    let composition_error_count = comp_errors.len();
    let composition_errors_sample = comp_errors.into_iter().take(20).collect();

    let report = BenchReport {
        scene: cli.scene,
        load_mode: if cli.load_none {
            "LoadNone".to_string()
        } else {
            "LoadAll".to_string()
        },
        masked_paths: cli.masks,
        loaded_paths: cli.load_paths,
        avail_ram_mb_start,
        rss_mb_before: (rss_before * 100.0).round() / 100.0,
        rss_mb_after_open: (rss_after_open * 100.0).round() / 100.0,
        rss_mb_after_load: (rss_after_load * 100.0).round() / 100.0,
        rss_mb_after_traverse: (rss_after_traverse * 100.0).round() / 100.0,
        rss_delta_mb: ((rss_after_traverse - rss_before) * 100.0).round() / 100.0,
        open_ms: (open_ms * 100.0).round() / 100.0,
        load_ms: (load_ms * 100.0).round() / 100.0,
        traverse_ms: (traverse_ms * 100.0).round() / 100.0,
        eval_ms: (eval_elapsed_secs * 1000.0 * 100.0).round() / 100.0,
        used_layers,
        prim_count,
        mesh_count,
        skel_count,
        material_count,
        instance_count,
        payload_prim_count,
        unloaded_payload_count,
        total_faces,
        time_sampled_meshes,
        prim_types,
        composition_error_count,
        composition_errors_sample,
        aborted_rss_limit,
    };

    let json = match serde_json::to_string_pretty(&report) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("failed to serialize report: {e}");
            process::exit(1);
        }
    };

    if let Some(out) = cli.out_file {
        if let Err(e) = fs::write(&out, &json) {
            eprintln!("failed to write '{out}': {e}");
            process::exit(1);
        }
    } else {
        println!("{json}");
    }
}
