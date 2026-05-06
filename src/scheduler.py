import json
from pulp import *

with open("../output/task_set.json", "r", encoding="utf-8") as f:
    data = json.load(f)

with open("../input/processor_settings.json", "r", encoding="utf-8") as f:
    processors = json.load(f)

with open("../input/price_72hr.json", "r", encoding="utf-8") as f:
    prices = json.load(f)

jobs = {}
for task, info in data["periodic"].items():
    for i in range(72 // info["p"]):
        if info["r"] + info["p"] * i + info["d"] > 72: break

        jobs[f"{task}_{i+1}"] = {
            "r": info["r"] + info["p"] * i,
            "d": info["r"] + info["p"] * i + info["d"],
            "e": info["e"],
            "w": info["w"],
            "preempt": info["preempt"]
        }

prob = LpProblem("VPP_Scheduling", LpMinimize)

T = range(1, 73)

I_g = [g["generator_id"] for g in processors["generator"]]
I_r = [r["renewable_id"] for r in processors["renewable_ capacity"]]
I_b = [b["storage_id"] for b in processors["storage"]]
I_all = I_g + I_r + I_b

J = list(jobs.keys())
J_chg = [c["job_id"] for c in processors["charging_jobs"]]
J_all = J + J_chg

# x[j][t]: Job j 在時間 t 是否執行 (Binary 0或1)
x = LpVariable.dicts("x", (J, T), cat="Binary")

# P[i][t]: 發電設備 i 在時間 t 的總供電量 (連續變數，大於等於0)
P = LpVariable.dicts("P", (I_all, T), lowBound=0, cat="Continuous")

# k[j][i][t]: 設備 i 在時間 t 供應給 Job j 的電能量 (連續變數)
k = LpVariable.dicts("k", (J_all, I_all, T), lowBound=0, cat="Continuous")

# Sell[t]: 在時間 t 賣給市場的電量 (連續變數)
Sell = LpVariable.dicts("Sell", T, lowBound=0, cat="Continuous")

# SOC[i][t]: 電池 i 在時間 t 的剩餘電量 (連續變數)
SOC = LpVariable.dicts("SOC", (I_b, T), lowBound=0, cat="Continuous")

def Constraint_1():
    for j in J:
        for t in T:
            total_k = lpSum(k[j][i][t] for i in I_all)
            prob += (total_k == jobs[j]["w"] * x[j][t], f"Demand_Match_{j}_t{t}")

def Constraint_2():
    for j in J:
        for t in T:
            if t < jobs[j]["r"]:
                prob += (x[j][t] == 0, f"Release_Time_{j}_t{t}")
                for i in I_all:
                    prob += (k[j][i][t] == 0, f"No_Power_Before_Release_{j}_{i}_t{t}")

def Constraint_3():
    for j in J:
        total_x = lpSum(x[j][t] for t in range(jobs[j]["r"], jobs[j]["d"]))
        prob += (total_x == jobs[j]["e"], f"Execution_Match_{j}")
        for t in T:
            if t >= jobs[j]["d"]:
                prob += (x[j][t] == 0, f"Deadline_{j}_t{t}")
                for i in I_all:
                    prob += (k[j][i][t] == 0, f"No_Power_After_Deadline_{j}_{i}_t{t}")

def Constraint_4():
    pass

def Constraint_5():
    # s[j][t]: Job j 在時間 t 是否啟動 (Binary 0或1)
    s = LpVariable.dicts("start", (J, T), cat="Binary")
    for j in J:
        for t in T:
            if t == jobs[j]["r"]:
                prob += (s[j][t] == x[j][t], f"Initial_Start_{j}_t{t}")
            elif t > jobs[j]["r"]:
                prob += (s[j][t] >= x[j][t] - x[j][t-1], f"Capture_Start_{j}_t{t}")

        if jobs[j]["preempt"] == 0:
            total_u = lpSum(s[j][t] for t in range(jobs[j]["r"], jobs[j]["d"]))
            prob += (total_u == 1, f"Non-preemptive_{j}")

def Constraint_6_and_7():
    gen_data = {g["generator_id"]: g for g in processors["generator"]}
    for i in I_g:
        out_min = gen_data[i]["output_min"]
        out_max = gen_data[i]["output_max"]
        ru = gen_data[i]["ramp_up_rate"]
        rd = gen_data[i]["ramp_down_rate"]
        for t in T:
            prob += (P[i][t] >= out_min, f"Power_Minium_Limit_{i}_t{t}")
            prob += (P[i][t] <= out_max, f"Power_Maxium_Limit_{i}_t{t}")
            if t == 1: prob += (P[i][t] <= ru, f"Power_Ramp_Up_Limit_{i}_t{t}")
            else:
                prob += (P[i][t] - P[i][t-1] <= ru, f"Power_Ramp_Up_Limit_{i}_t{t}")
                prob += (P[i][t-1] - P[i][t] <= ru, f"Power_Ramp_Down_Limit_{i}_t{t}")

def Constraint_7():
    pass

def Constraint_8():
    pass