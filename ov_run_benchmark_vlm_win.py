import os
import subprocess
import csv
import re
import argparse
import time
import threading
from pathlib import Path
from collections import defaultdict
import argparse
import csv
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

def get_system_info() -> dict:
    """Collect GPU and system memory information on Windows."""
    info = {
        "os": f"{platform.system()} {platform.version()}",
        "cpu": "Unknown",
        "system_ram_total_gb": 0,
        "system_ram_used_gb": 0,
        "gpus": [],  # list of {name, vram_mb, driver_version, gpu_class}
    }

    # --- CPU name ---
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             '(Get-CimInstance Win32_Processor).Name'],
            text=True, timeout=10, creationflags=0x08000000,
        ).strip()
        if out:
            info["cpu"] = out
    except Exception:
        pass

    # --- System RAM ---
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             '$os = Get-CimInstance Win32_OperatingSystem;'
             'Write-Output "$($os.TotalVisibleMemorySize) $($os.FreePhysicalMemory)"'],
            text=True, timeout=10, creationflags=0x08000000,
        ).strip()
        parts = out.split()
        if len(parts) >= 2:
            total_kb = int(parts[0])
            free_kb = int(parts[1])
            info["system_ram_total_gb"] = round(total_kb / 1048576, 2)
            info["system_ram_used_gb"] = round((total_kb - free_kb) / 1048576, 2)
    except Exception:
        pass

    # --- Step 1: GPU names + drivers via WMI ---
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             'Get-CimInstance Win32_VideoController | '
             'ForEach-Object { "$($_.Name)|$($_.DriverVersion)|$($_.AdapterRAM)" }'],
            text=True, timeout=10, creationflags=0x08000000,
        ).strip()
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                name = parts[0].strip()
                if _is_virtual_gpu(name):
                    continue
                adapter_ram_mb = 0
                if len(parts) >= 3:
                    try:
                        ar = int(parts[2].strip())
                        if ar > 0:
                            adapter_ram_mb = ar // (1024 * 1024)
                    except (ValueError, TypeError):
                        pass
                info["gpus"].append({
                    "name": name,
                    "vram_mb": 0,
                    "_adapter_ram_mb": adapter_ram_mb,
                    "driver_version": parts[1].strip(),
                    "gpu_class": _detect_gpu_class(name),
                })
    except Exception as e:
        print(f"  Warning: GPU WMI query failed: {e}")

    # --- Step 2: Total VRAM ---
    # Note: VRAM *usage* is collected separately after warmup inference
    # via update_vram_usage() to capture actual model memory consumption.
    #
    # Try fast registry approach first (~1s), fall back to slow vulkaninfo (~30s+).
    vram_found = False
    if info["gpus"]:
        # Fast path: Windows registry qwMemorySize (REG_QWORD, bypasses 4GB WMI cap)
        # Also tries MemorySize (REG_DWORD) as fallback for some Intel GPUs.
        print("  Detecting GPU VRAM via registry...")
        try:
            out = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command',
                 "$base = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class"
                 "\\{4d36e968-e325-11ce-bfc1-08002be10318}';"
                 "Get-ChildItem $base -EA SilentlyContinue | ForEach-Object {"
                 "  $p = Get-ItemProperty $_.PSPath -EA SilentlyContinue;"
                 "  $n = $p.DriverDesc;"
                 "  $m = $p.'HardwareInformation.qwMemorySize';"
                 "  if ($m -is [byte[]]) {"
                 "    if ($m.Length -ge 8) { $m = [BitConverter]::ToInt64($m,0) }"
                 "    elseif ($m.Length -ge 4) { $m = [BitConverter]::ToUInt32($m,0) }"
                 "  };"
                 "  if (-not $m -or $m -le 0) {"
                 "    $m = $p.'HardwareInformation.MemorySize';"
                 "    if ($m -is [byte[]]) {"
                 "      if ($m.Length -ge 8) { $m = [BitConverter]::ToInt64($m,0) }"
                 "      elseif ($m.Length -ge 4) { $m = [BitConverter]::ToUInt32($m,0) }"
                 "    }"
                 "  };"
                 "  if ($n -and $m -and $m -gt 0) { \"$n|$m\" }"
                 "}"],
                text=True, timeout=10, creationflags=0x08000000,
            ).strip()
            for line in out.splitlines():
                parts = line.split("|")
                if len(parts) >= 2:
                    reg_name = parts[0].strip()
                    try:
                        reg_bytes = int(parts[1].strip())
                    except (ValueError, TypeError):
                        continue
                    if reg_bytes > 0:
                        reg_mb = reg_bytes // (1024 * 1024)
                        # Match to existing GPU entry
                        matched = _match_gpu(info["gpus"], reg_name)
                        if matched:
                            matched["vram_mb"] = reg_mb
                            vram_found = True
        except Exception:
            pass

    # Fast fallback: GPU Local Adapter Memory performance counter
    # (~1s vs vulkaninfo's 30-120s)
    if info["gpus"] and not vram_found:
        try:
            import re as _re

            def _query_budget(counter_path: str) -> dict:
                budget = {}
                try:
                    raw = subprocess.check_output(
                        ['powershell', '-NoProfile', '-Command',
                         f"(Get-Counter '{counter_path}'"
                         ").CounterSamples | "
                         "ForEach-Object { \"$($_.InstanceName)|$($_.CookedValue)\" }"],
                        text=True, timeout=10, creationflags=0x08000000,
                    ).strip()
                except Exception:
                    return budget
                for ln in raw.splitlines():
                    p = ln.split("|")
                    if len(p) >= 2:
                        inst = p[0].strip()
                        try:
                            v = float(p[1].strip())
                        except (ValueError, TypeError):
                            continue
                        mm = _re.search(r'phys_(\d+)', inst)
                        if mm:
                            ix = int(mm.group(1))
                            if ix not in budget or v > budget[ix]:
                                budget[ix] = v
                return budget

            # Try dedicated (local) budget first
            budget_by_phys = _query_budget(
                '\\GPU Local Adapter Memory(*)\\Local Budget')
            max_budget = max(budget_by_phys.values(), default=0)

            # iGPU has no dedicated VRAM; try shared (non-local) budget
            if max_budget == 0:
                shared_budget = _query_budget(
                    '\\GPU Non Local Adapter Memory(*)\\Non-Local Budget')
                if shared_budget and max(shared_budget.values(), default=0) > 0:
                    budget_by_phys = shared_budget

            # Assign budgets: sort descending, assign largest to inference GPU
            if budget_by_phys:
                sorted_budgets = sorted(budget_by_phys.values(), reverse=True)
                inf_idx = _find_inference_gpu_index(info["gpus"])
                if sorted_budgets and sorted_budgets[0] > 0:
                    info["gpus"][inf_idx]["vram_mb"] = round(
                        sorted_budgets[0] / (1024 * 1024))
                    vram_found = True
                # Assign remaining budgets to other GPUs
                other_idxs = [i for i in range(len(info["gpus"])) if i != inf_idx]
                for i, budget in enumerate(sorted_budgets[1:]):
                    if i < len(other_idxs) and budget > 0:
                        info["gpus"][other_idxs[i]]["vram_mb"] = round(
                            budget / (1024 * 1024))
        except Exception:
            pass

    # Last resort: WMI AdapterRAM (uint32, capped at ~4 GB but better than 0)
    for gpu in info["gpus"]:
        ar = gpu.pop("_adapter_ram_mb", 0)
        if gpu["vram_mb"] == 0 and ar > 0:
            gpu["vram_mb"] = ar
            print(f"  VRAM (WMI fallback): {gpu['name']} = {ar} MB")

    return info



def _query_gpu_usage(counter_name: str) -> dict:
    """Query a GPU Adapter Memory performance counter, return {phys_idx: max_bytes}."""
    import re as _re
    usage_by_phys: dict[int, float] = {}
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             f"(Get-Counter '\\GPU Adapter Memory(*)\\{counter_name}'"
             ").CounterSamples | "
             "ForEach-Object { \"$($_.InstanceName)|$($_.CookedValue)\" }"],
            text=True, timeout=10, creationflags=0x08000000,
        ).strip()
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                instance = parts[0].strip()
                try:
                    val = float(parts[1].strip())
                except (ValueError, TypeError):
                    continue
                m = _re.search(r'phys_(\d+)', instance)
                if m:
                    idx = int(m.group(1))
                    if idx not in usage_by_phys or val > usage_by_phys[idx]:
                        usage_by_phys[idx] = val
    except Exception:
        pass
    return usage_by_phys

def format_system_info(sys_info: dict) -> str:
    """Format system info as a human-readable string."""
    lines = [
        f"OS     : {sys_info['os']}",
        f"CPU    : {sys_info['cpu']}",
        f"RAM    : {sys_info['system_ram_total_gb']:.1f} GB total, "
        f"{sys_info['system_ram_used_gb']:.1f} GB used",
    ]
    for i, gpu in enumerate(sys_info.get("gpus", [])):
        vram_str = f"{gpu['vram_mb']} MB ({gpu['vram_mb'] / 1024:.1f} GB)" if gpu['vram_mb'] > 0 else "N/A"
        vram_used = gpu.get('vram_used_mb', 0)
        vram_used_str = f" | Used: {vram_used:.0f} MB ({vram_used / 1024:.1f} GB)" if vram_used > 0 else ""
        vk = gpu.get('vulkan_device', '')
        vk_str = f" [Vulkan: {vk}]" if vk else ""
        lines.append(
            f"GPU {i} : {gpu['name']} | VRAM: {vram_str}{vram_used_str} | "
            f"Driver: {gpu['driver_version']}{vk_str}"
        )
    if not sys_info.get("gpus"):
        lines.append("GPU    : Not detected")
    return "\n".join(lines)



def update_vram_usage(sys_info: dict) -> None:
    """Collect current GPU VRAM usage via Get-Counter.

    Should be called while the model is actively loaded (e.g. after warmup)
    to capture actual inference VRAM usage.

    Queries Dedicated Usage first.  If the inference GPU shows 0 dedicated
    usage (typical for integrated GPUs that use shared system memory),
    falls back to Shared Usage.
    """
    if not sys_info.get("gpus"):
        return
    try:
        usage_by_phys = _query_gpu_usage('Dedicated Usage')

        # For iGPU-only systems dedicated usage is 0; use shared usage instead.
        # Do NOT fall back for discrete GPUs — shared usage includes all system
        # shared GPU memory and gives misleading multi-GB values.
        max_dedicated = max(usage_by_phys.values(), default=0)
        if max_dedicated == 0:
            inf_idx = _find_inference_gpu_index(sys_info["gpus"])
            if sys_info["gpus"][inf_idx].get("gpu_class") != "独显":
                shared = _query_gpu_usage('Shared Usage')
                if shared:
                    usage_by_phys = shared

        # Assign highest VRAM usage to the inference (discrete) GPU.
        # perf counter phys_N indices may not match WMI GPU order.
        if usage_by_phys:
            sorted_usage = sorted(usage_by_phys.values(), reverse=True)
            inf_idx = _find_inference_gpu_index(sys_info["gpus"])
            if sorted_usage[0] > 0:
                sys_info["gpus"][inf_idx]["vram_used_mb"] = round(
                    sorted_usage[0] / (1024 * 1024), 1)
            other_idxs = [i for i in range(len(sys_info["gpus"])) if i != inf_idx]
            for i, val in enumerate(sorted_usage[1:]):
                if i < len(other_idxs) and val > 0:
                    sys_info["gpus"][other_idxs[i]]["vram_used_mb"] = round(
                        val / (1024 * 1024), 1)

        # Sanity: if VRAM used exceeds detected total, the total was wrong
        # (e.g. WMI uint32 overflow).  Re-query budget counters — they may
        # now report values after the GPU started using memory post-warmup.
        import re as _re2
        needs_budget = any(
            g.get("vram_used_mb", 0) > 0 and g.get("vram_used_mb", 0) > g.get("vram_mb", 0)
            for g in sys_info["gpus"]
        )
        if needs_budget:
            budget_mb = 0
            for ctr in [
                '\\GPU Local Adapter Memory(*)\\Local Budget',
                '\\GPU Non Local Adapter Memory(*)\\Non-Local Budget',
            ]:
                try:
                    raw = subprocess.check_output(
                        ['powershell', '-NoProfile', '-Command',
                         f"(Get-Counter '{ctr}'"
                         ").CounterSamples | "
                         "ForEach-Object { \"$($_.InstanceName)|$($_.CookedValue)\" }"],
                        text=True, timeout=10, creationflags=0x08000000,
                    ).strip()
                    for ln in raw.splitlines():
                        p = ln.split("|")
                        if len(p) >= 2:
                            try:
                                v = float(p[1].strip())
                                mb = round(v / (1024 * 1024))
                                if mb > budget_mb:
                                    budget_mb = mb
                            except (ValueError, TypeError):
                                continue
                except Exception:
                    pass
                if budget_mb > 0:
                    break
            for gpu in sys_info["gpus"]:
                used = gpu.get("vram_used_mb", 0)
                if used > 0 and used > gpu.get("vram_mb", 0):
                    gpu["vram_mb"] = budget_mb if budget_mb > 0 else round(used)
    except Exception as e:
        print(f"  Warning: GPU VRAM usage query failed: {e}")



def get_memory_snapshot(sys_info: dict | None = None) -> dict:
    """Get a snapshot of current system RAM and GPU VRAM usage.

    Returns a dict with 'system_ram_used_gb' and 'gpu_vram_used_mb' (first GPU).
    Designed to be called before and after each benchmark test so the average
    reflects memory consumption during the test.

    Pass *sys_info* so the function can avoid falling back to Shared Usage
    for discrete GPUs (which gives misleading values).
    """
    snapshot = {"system_ram_used_gb": 0.0, "gpu_vram_used_mb": 0.0}

    # System RAM
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             '$os = Get-CimInstance Win32_OperatingSystem;'
             'Write-Output "$($os.TotalVisibleMemorySize) $($os.FreePhysicalMemory)"'],
            text=True, timeout=10, creationflags=0x08000000,
        ).strip()
        parts = out.split()
        if len(parts) >= 2:
            total_kb = int(parts[0])
            free_kb = int(parts[1])
            snapshot["system_ram_used_gb"] = round((total_kb - free_kb) / 1048576, 2)
    except Exception:
        pass

    # GPU VRAM — use max usage across all physical GPUs
    # (inference GPU always has highest VRAM usage; phys_N indices
    #  may not match WMI GPU order so we take the max)
    try:
        usage_by_phys = _query_gpu_usage('Dedicated Usage')
        max_dedicated = max(usage_by_phys.values(), default=0)
        # iGPU uses shared memory; fall back when dedicated is 0.
        # Do NOT fall back for discrete GPUs (shared usage is misleading).
        if max_dedicated == 0:
            use_shared = True
            if sys_info and sys_info.get("gpus"):
                inf_idx = _find_inference_gpu_index(sys_info["gpus"])
                if sys_info["gpus"][inf_idx].get("gpu_class") == "独显":
                    use_shared = False
            if use_shared:
                shared = _query_gpu_usage('Shared Usage')
                if shared:
                    usage_by_phys = shared
        if usage_by_phys:
            max_usage = max(usage_by_phys.values())
            if max_usage > 0:
                snapshot["gpu_vram_used_mb"] = round(
                    max_usage / (1024 * 1024), 1)
    except Exception:
        pass

    return snapshot

# 峰值监控线程
class MemoryPeakMonitor:
    def __init__(self):
        self.peak_system = 0.0
        self.peak_igpu = 0.0
        self.peak_dgpu = 0.0
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.running:
            snap = get_memory_snapshot()
            if snap["system_ram_used_gb"] > self.peak_system:
                self.peak_system = snap["system_ram_used_gb"]
            if snap["gpu_vram_used_mb"] > self.peak_igpu:
                self.peak_igpu = snap["gpu_vram_used_mb"] 
            #if snap["dgpu_used_gb"] > self.peak_dgpu:
            #    self.peak_dgpu = snap["dgpu_used_gb"]
            time.sleep(0.1)

    def stop(self):
        self.running = False

# ==============================================================================
# ==============================================================================
def get_group_name(filename):
    base = Path(filename).stem
    parts = base.split('_')
    if len(parts) >= 2:
        return '_'.join(parts[:-1])
    return base

def run_benchmark(prompt_file, fixed_args):
    cmd = ["python", "benchmark_vlm_new.py", *fixed_args, "-pf", prompt_file]
    print(f"\n{'='*60}")
    print(f"运行：{prompt_file}")
    print(f"{'='*60}\n")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output = result.stdout + result.stderr
    print(output)
    return output

def parse_metrics(output, prompt_file, model_path):
    fname = os.path.basename(prompt_file)
    group = get_group_name(fname)
    data = {
        "file name": fname,
        "target input name": group,
        "model name": model_path,
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
        data[key] = float(match.group(1)) if match else 0.0
    return data

def save_raw_data(data_list, raw_csv_path):
    headers = [
        "file name", "target input name", "model name",
        "Prompt token", "Output token", "TTFT(ms)", "TPOT(ms)", "Throughput tokens/s",
        "system_memory_max_gb", "igpu_shared_memory_max_gb"#, "dgpu_memory_max_gb"
    ]
    with open(raw_csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for d in data_list:
            w.writerow([
                d["file name"], d["target input name"], d["model name"],
                d["prompt_tokens"], d["output_tokens"],
                f"{d['ttft']:.2f}", f"{d['tpot']:.2f}", f"{d['throughput']:.2f}",
                f"{d['system_memory_max_gb']:.2f}",
                f"{d['igpu_shared_memory_max_gb']:.2f}",
              #  f"{d['dgpu_memory_max_gb']:.2f}"
            ])

def save_average_data(group_data, avg_csv_path):
    headers = [
        "target input name", "model name",
        "actual Prompt token size", "actual Output token size",
        "TTFT avg(ms)", "TPOT avg(ms)", "Throughput avg(tokens/s)",
        "system_memory_max_gb", "igpu_shared_memory_max_gb", #"dgpu_memory_max_gb",
        "run times"
    ]
    with open(avg_csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for g, m in sorted(group_data.items()):
            cnt = m["count"]
            w.writerow([
                g, m["model"],
                f"{m['prompt_total']/cnt:.0f}", f"{m['output_total']/cnt:.0f}",
                f"{m['ttft_total']/cnt:.2f}", f"{m['tpot_total']/cnt:.2f}", f"{m['throughput_total']/cnt:.2f}",
                f"{m['system_memory_max_gb']:.2f}",
                f"{m['igpu_shared_memory_max_gb']:.2f}",
              #  f"{m['dgpu_memory_max_gb']:.2f}",
                str(cnt)
            ])

# ==============================================================================
# ============================== 主函数 ==============================
# ==============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-pf", "--prompts_folder", required=True)
    parser.add_argument("-m", "--model_path", required=True)
    parser.add_argument("-raw", "--raw_csv_name", required=True)
    parser.add_argument("-n", default="1")
    parser.add_argument("-d", default="GPU")
    parser.add_argument("-mt", default="512")
    args = parser.parse_args()

    os.makedirs("reports-ov", exist_ok=True)
    raw_csv = os.path.join("reports-ov", args.raw_csv_name)
    avg_csv = os.path.join("reports-ov", "avg_" + args.raw_csv_name)

    fixed_args = ["-n", args.n, "-d", args.d, "-m", args.model_path, "-mt", args.mt]
    txt_files = sorted([
        str(Path(args.prompts_folder) / f)
        for f in os.listdir(args.prompts_folder)
        if f.endswith(".txt")
    ])

    # ========== 启动原版正确的峰值监控 ==========
    mon = MemoryPeakMonitor()
    mon.start()

    print("\nCollecting system information...")
    sys_info = get_system_info()
    print(format_system_info(sys_info))

    raw_data = []
    for pf in txt_files:
        output = run_benchmark(pf, fixed_args)
        data = parse_metrics(output, pf, args.model_path)

        mem_snap = get_memory_snapshot(sys_info)
        data["system_memory_max_gb"] = mon.peak_system #mem_snap['system_ram_used_gb']
        data["igpu_shared_memory_max_gb"] = mon.peak_igpu # mem_snap['gpu_vram_used_mb'] / 1024
        #data["dgpu_memory_max_gb"] = mon.peak_dgpu
        print("###### system Peak GB: ",mon.peak_system)
        print("###### VRAM Peak GB: ",mon.peak_igpu / 1024)
        raw_data.append(data)

    mon.stop()

    # 分组统计
    group = defaultdict(lambda: {
        "count": 0, "model": "",
        "prompt_total": 0, "output_total": 0,
        "ttft_total": 0, "tpot_total": 0, "throughput_total": 0,
        "system_memory_max_gb": 0, "igpu_shared_memory_max_gb": 0 #, "dgpu_memory_max_gb":0
    })
    for d in raw_data:
        g = d["target input name"]
        group[g]["count"] += 1
        group[g]["model"] = d["model name"]
        group[g]["prompt_total"] += d["prompt_tokens"]
        group[g]["output_total"] += d["output_tokens"]
        group[g]["ttft_total"] += d["ttft"]
        group[g]["tpot_total"] += d["tpot"]
        group[g]["throughput_total"] += d["throughput"]
        group[g]["system_memory_max_gb"] = d["system_memory_max_gb"]
        group[g]["igpu_shared_memory_max_gb"] = d["igpu_shared_memory_max_gb"] / 1024
        #group[g]["dgpu_memory_max_gb"] = d["dgpu_memory_max_gb"]

    save_raw_data(raw_data, raw_csv)
    save_average_data(group, avg_csv)

    print(f"\n✅ 运行完成！")
    print(f"📄 原始数据：{raw_csv}")
    print(f"📊 平均值：{avg_csv}")
    print(f"💾 系统内存峰值：{mon.peak_system:.2f} GB")
    print(f"🎮 集成显卡共享内存峰值：{mon.peak_igpu/1024:.2f} GB")
  #  print(f"🖥 独立显卡显存峰值：{mon.peak_dgpu:.2f} GB")

if __name__ == "__main__":
    main()
