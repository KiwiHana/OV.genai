import os
import subprocess
import csv
import re
import argparse
import time
import threading
from pathlib import Path
from collections import defaultdict

# ===================== Memory Monitoring (Your Shell Formula) =====================
peak_system_memory_gb = 0.0
monitor_stop = False

def get_system_used_memory():
    cmd = """grep -E '^MemTotal|^MemFree|^Buffers|^Cached|^SReclaimable' /proc/meminfo | awk '{a[$1]=$2}
END{
    used = a["MemTotal:"] - a["MemFree:"] - a["Buffers:"] - a["Cached:"] - a["SReclaimable:"]
    printf("%.2f", used/1024/1024)
}'"""
    
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        used_gb = float(result.stdout.strip())
        return used_gb
    except Exception:
        return 0.0

def memory_monitor_thread(interval=0.2):
    global peak_system_memory_gb, monitor_stop
    peak_system_memory_gb = 0.0
    monitor_stop = False
    
    while not monitor_stop:
        current_used = get_system_used_memory()
        if current_used > peak_system_memory_gb:
            peak_system_memory_gb = current_used
        time.sleep(interval)

def start_system_memory_monitor():
    thread = threading.Thread(target=memory_monitor_thread, daemon=True)
    thread.start()

def stop_system_memory_monitor():
    global monitor_stop, peak_system_memory_gb
    monitor_stop = True
    time.sleep(0.3)
    return round(peak_system_memory_gb, 2)

# ===================== Original Functions =====================
def get_group_name(filename):
    base = Path(filename).stem
    parts = base.split('_')
    if len(parts) >= 2:
        return '_'.join(parts[:-1])
    return base

def run_benchmark(prompt_file, fixed_args):
    init_mem = get_system_used_memory()
    print("********* INIT get_system_used_memory GB: ", init_mem)
    cmd = ["python", "benchmark_vlm_new.py", *fixed_args, "-pf", prompt_file]
    
    print(f"\n{'='*60}")
    print(f"Running: {prompt_file}")
    print(f"{'='*60}\n")

    start_system_memory_monitor()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )
    stdout, stderr = process.communicate()
    output = stdout + stderr

    peak_mem = stop_system_memory_monitor()

    print(output)
    print(f" Peak System Memory: {peak_mem} GB\n")
    return output, peak_mem, init_mem

def parse_metrics(output, prompt_file, model_path, peak_memory,init_memory):
    # FIX 1: use Path instead of os.basename
    fname = Path(prompt_file).name
    group = get_group_name(prompt_file)
    data = {
        "file name": fname,
        "target input name": group,
        "model name": model_path,
        "peak_memory_gb": peak_memory,
        "init_memory_gb": init_memory,
    }

    patterns = {
        "prompt_tokens": r"Prompt token size:\s*(\d+)",
        "output_tokens": r"Output token size:\s*(\d+)",
        "ttft": r"TTFT:\s*([\d.]+)",
        "tpot": r"TPOT:\s*([\d.]+)",
        "throughput": r"Throughput\s*:\s*([\d.]+)"
    }

    for key, pat in patterns.items():
        match = re.search(pat, output)
        val = float(match.group(1)) if match else 0.0
        data[key] = val

    return data

def save_raw_data(data_list, raw_csv_path):
    headers = [
        "file name",
        "target input name",
        "model name",
        "Prompt token",
        "Output token",
        "TTFT(ms)",
        "TPOT(ms)",
        "Throughput tokens/s",
        "Peak System Memory (GB)",
        "Init System Memory (GB)"
    ]
    with open(raw_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for d in data_list:
            writer.writerow([
                d["file name"], d["target input name"], d["model name"],
                d["prompt_tokens"], d["output_tokens"],
                f"{d['ttft']:.2f}", f"{d['tpot']:.2f}", f"{d['throughput']:.2f}",
                f"{d['peak_memory_gb']:.2f}",f"{d['init_memory_gb']:.2f}"
            ])

def save_average_data(group_data, avg_csv_path):
    headers = [
        "target input name",
        "model name",
        "actual Prompt token size",
        "actual Output token size",
        "TTFT avg(ms)",
        "TPOT avg(ms)",
        "Throughput avg(tokens/s)",
        "Peak System Memory Max (GB)",
        "Init System Memory (GB)",
        "run times"
    ]

    with open(avg_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for group, metrics in sorted(group_data.items()):
            count = metrics["count"]
            model = metrics["model"]
            prompt_avg = metrics["prompt_total"] / count
            output_avg = metrics["output_total"] / count
            ttft_avg = metrics["ttft_total"] / count
            tpot_avg = metrics["tpot_total"] / count
            thr_avg = metrics["throughput_total"] / count
            peak_mem_max = metrics["peak_memory_max"]
            init_mem_max = metrics["init_memory_max"]

            writer.writerow([
                group, model,
                f"{prompt_avg:.0f}", f"{output_avg:.0f}",
                f"{ttft_avg:.2f}", f"{tpot_avg:.2f}", f"{thr_avg:.2f}",
                f"{peak_mem_max:.2f}",f"{init_mem_max:.2f}",
                str(count)
            ])

def main():
    parser = argparse.ArgumentParser(description="Batch run VLM benchmark")
    parser.add_argument("-pf", "--prompts_folder", required=True, help="Prompts folder")
    parser.add_argument("-m", "--model_path", required=True, help="Model path")
    parser.add_argument("-raw", "--raw_csv_name", required=True, help="Raw CSV name")
    parser.add_argument("-n", default="1", help="Number of runs")
    parser.add_argument("-d", default="GPU", help="Device")
    parser.add_argument("-mt", default="512", help="Max tokens")
    args = parser.parse_args()

    report_dir = "reports-ov"
    os.makedirs(report_dir, exist_ok=True)

    raw_csv_path = os.path.join(report_dir, args.raw_csv_name)
    avg_csv_path = os.path.join(report_dir, f"avg_{args.raw_csv_name}")

    fixed_args = [
        "-n", args.n,
        "-d", args.d,
        "-m", args.model_path,
        "-mt", args.mt
    ]

    if not os.path.exists(args.prompts_folder):
        print(f"Error: folder {args.prompts_folder} not found")
        return

    txt_files = sorted([
        str(Path(args.prompts_folder) / f)
        for f in os.listdir(args.prompts_folder)
        if f.endswith(".txt")
    ])

    if not txt_files:
        print("No txt files found")
        return

    print(f" Found {len(txt_files)} files")

    raw_data = []
    for pf in txt_files:
        output, peak_mem, init_mem = run_benchmark(pf, fixed_args)
        data = parse_metrics(output, pf, args.model_path, peak_mem, init_mem)
        raw_data.append(data)

    group_stats = defaultdict(lambda: {
        "count": 0,
        "model": "",
        "prompt_total": 0,
        "output_total": 0,
        "ttft_total": 0,
        "tpot_total": 0,
        "throughput_total": 0,
        "peak_memory_max": 0.0
    })

    for d in raw_data:
        g = d["target input name"]
        gs = group_stats[g]
        gs["count"] += 1
        gs["model"] = d["model name"]
        gs["prompt_total"] += d["prompt_tokens"]
        gs["output_total"] += d["output_tokens"]
        gs["ttft_total"] += d["ttft"]
        gs["tpot_total"] += d["tpot"]
        gs["throughput_total"] += d["throughput"]
        if d["peak_memory_gb"] > gs["peak_memory_max"]:
            gs["peak_memory_max"] = d["peak_memory_gb"]
            gs["init_memory_max"] = d["init_memory_gb"]

    save_raw_data(raw_data, raw_csv_path)
    save_average_data(group_stats, avg_csv_path)

    print(f"\n All tasks completed!")
    print(f" Raw data: {os.path.abspath(raw_csv_path)}")
    print(f" Average report: {os.path.abspath(avg_csv_path)}")

if __name__ == "__main__":
    main()
