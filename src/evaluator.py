import json



with open("../output/task_set.json", "r", encoding="utf-8") as f:
    tasks_data = json.load(f)
with open("../output/schedule_result.json", "r", encoding="utf-8") as f:
    schedule_data = json.load(f)["schedule_result"]
with open("../input/processor_settings.json", "r", encoding="utf-8") as f:
    processors = json.load(f)
with open("../input/price_72hr.json", "r", encoding="utf-8") as f:
    prices = json.load(f)

jobs_info = {}
periodic_groups = {}

for task, info in tasks_data.get("periodic", {}).items():
    periodic_groups[task] = []
    for i in range(72 // info["p"]):
        if info["r"] + info["p"] * i + info["d"] > 72: break
        job_id = f"{task}_{i + 1}"
        periodic_groups[task].append(job_id)
        jobs_info[job_id] = {
            "type": "periodic",
            "r": info["r"] + info["p"] * i,
            "d": info["r"] + info["p"] * i + info["d"],
            "e": info["e"],
            "w": info["w"]
        }

for task, info in tasks_data.get("aperiodic", {}).items():
    jobs_info[task] = {
        "type": "aperiodic", "r": info["r"], "d": info["r"] + info["d"],
        "e": info["e"], "w": info["w"]
    }

for task, info in tasks_data.get("sporadic", {}).items():
    jobs_info[task] = {
        "type": "sporadic", "r": info["r"], "d": info["r"] + info["d"],
        "e": info["e"], "w": info["w"]
    }



# 列出所有任務完成時間
complete_times = {}
for j in jobs_info.keys():
    complete_times[j] = -1

for data in schedule_data:
    t = data["t"]
    k = data.get("k", {})
    for j in k.keys():
        # 1. 如果是 aperiodic 或 sporadic，名稱完全對應 (如 "a1", "s1")
        if j in complete_times:
            complete_times[j] = max(complete_times[j], t)

        # 2. 如果是基礎週期性任務名稱 (如 "p1")
        elif j in periodic_groups:
            # 遍歷該任務旗下的所有週期實例 (如 p1_1, p1_2, p1_3)
            for job_id in periodic_groups[j]:
                # 透過時間點 t 是否落在該實例的相對時間區間 [r, d] 內來精準對位
                if jobs_info[job_id]["r"] <= t <= jobs_info[job_id]["d"]:
                    complete_times[job_id] = max(complete_times[job_id], t)

# 以下做評估
# 1. Hard Deadline Miss Rate
hard_jobs = [j for j, info in jobs_info.items() if info["type"] == "periodic"]
hard_miss = 0
for j in hard_jobs:
    if complete_times[j] == -1 or complete_times[j] > jobs_info[j]["d"]:
        hard_miss += 1

hard_miss_rate = hard_miss / len(hard_jobs) if hard_jobs else 0.0

# 2. Soft Deadline Miss Rate
aperiodic_jobs = [j for j, info in jobs_info.items() if info["type"] == "aperiodic"]
soft_miss = 0
for j in aperiodic_jobs:
    if complete_times[j] > jobs_info[j]["d"]:
        soft_miss += 1

soft_miss_rate = soft_miss / len(aperiodic_jobs) if aperiodic_jobs else 0.0

# 3. Tardiness & Response tine
tardiness = []
response_time = []

for j, complete_time in complete_times.items():
    if complete_time != -1:
        response_time.append(complete_time - jobs_info[j]["r"])
        tardiness.append(max(0, complete_time - jobs_info[j]["d"]))

avg_tardiness = sum(tardiness) / len(tardiness) if tardiness else 0.0
max_tardiness = max(tardiness) if tardiness else 0.0
avg_response_time = sum(response_time) / len(response_time) if response_time else 0.0
max_response_time = max(response_time) if response_time else 0.0

# 4. Completion-time Jitter
jitter = []
for task, jobs in periodic_groups.items():
    rt = []
    for j in jobs:
        if complete_times[j] != -1:
            rt.append(complete_times[j] - jobs_info[j]["r"])

    if len(rt) > 1:
        jitter.append(max(rt) - min(rt))

avg_jitter = sum(jitter) / len(jitter) if jitter else 0.0

# 5. Acceptance Test
sporadic_jobs = [j for j, info in jobs_info.items() if info["type"] == "sporadic"]
total_exec = sum(jobs_info[j]["e"] for j in sporadic_jobs)

# 要排除被拒絕的
rejected = schedule_data[-1].get("rejected_sporadic", [])
accepted_exec = sum(jobs_info[j]["e"] for j in sporadic_jobs if j not in rejected)

value_rate = accepted_exec / total_exec if total_exec > 0 else 0.0
violation_rate = 0.0 # 一定不會違約

# 6. Cost & Revenue
price_dict = {p["hour"]: p["market_price"] for p in prices["price"]}
gen_costs = {g["generator_id"]: {"fixed": g["cost_fixed"], "var": g["cost_variable"]} for g in processors["generator"]}

total_gen_cost = 0.0
total_revenue = 0.0

for data in schedule_data:
    t = data["t"]
    total_revenue += data["sell"] * price_dict[t]
    # 是傳統機組並且有開機才加
    for g_id, p_val in data["P"].items():
        if g_id in gen_costs:
            if p_val > 0.01:
                total_gen_cost += gen_costs[g_id]["fixed"] + gen_costs[g_id]["var"] * p_val

objective_value = (soft_miss * 10000) + total_gen_cost - total_revenue

# 輸出
eval_result = {
    "hard_deadline_miss_rate": round(hard_miss_rate, 4),
    "soft_deadline_miss_rate": round(soft_miss_rate, 4),
    "average_tardiness": round(avg_tardiness, 2),
    "max_tardiness": max_tardiness,
    "average_response_time": round(avg_response_time, 2),
    "max_response_time": max_response_time,
    "completion_time_jitter": round(avg_jitter, 2),
    "acceptance_test": {
        "sporadic_value_rate": round(value_rate, 4),
        "post_acceptance_violation_rate": round(violation_rate, 4)
    },
    "generator_cost": round(total_gen_cost, 2),
    "market_revenue": round(total_revenue, 2),
    "objective_value": round(objective_value, 2)
}

with open("../output/evaluation_results.json", "w", encoding="utf-8") as f:
    json.dump(eval_result, f, indent=4, ensure_ascii=False)

for k, v in eval_result.items():
        print(f"{k}: {v}")