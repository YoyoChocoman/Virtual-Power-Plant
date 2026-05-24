import json
import random
from pulp import *

with open("../output/task_set.json", "r", encoding="utf-8") as f:
    data = json.load(f)

with open("../input/processor_settings.json", "r", encoding="utf-8") as f:
    processors = json.load(f)

with open("../input/price_72hr.json", "r", encoding="utf-8") as f:
    prices = json.load(f)

SHIFT = 0

jobs_periodic_dict = {}
for task, info in data["periodic"].items():
    for i in range(72 // info["p"]):
        if info["r"] + info["p"] * i + info["d"] > 72: break

        jobs_periodic_dict[f"{task}_{i + 1}"] = {
            "r": info["r"] + info["p"] * i + SHIFT,
            "d": info["r"] + info["p"] * i + info["d"] + SHIFT,
            "e": info["e"],
            "w": info["w"],
            "preempt": info["preempt"]
        }

jobs_aperiodic_dict = data.get("aperiodic", {})
for task, info in data.get("aperiodic", {}).items():
    jobs_aperiodic_dict[task] = {
        "r": info["r"] + SHIFT,
        "d": info["r"] + info["d"] + SHIFT, # 轉為 absolute deadline
        "e": info["e"],
        "w": info["w"],
        "preempt": info["preempt"]
    }

jobs_sporadic_dict = {}
for task, info in data.get("sporadic", {}).items():
    jobs_sporadic_dict[task] = {
        "r": info["r"] + SHIFT,
        "d": info["r"] + info["d"] + SHIFT, # 轉為 absolute deadline
        "e": info["e"],
        "w": info["w"],
        "preempt": info["preempt"]
    }

#合併字典
jobs_all_dict = {**jobs_periodic_dict, **jobs_aperiodic_dict, **jobs_sporadic_dict}

BIG_LP = LpProblem("VPP_Scheduling", LpMinimize)

T = range(1, 73)

I_g = [g["generator_id"] for g in processors["generator"]]
I_r = [r["renewable_id"] for r in processors["renewable_capacity"]]
I_b = [b["storage_id"] for b in processors["storage"]]
I_all = I_g + I_r + I_b

J_periodic = list(jobs_periodic_dict.keys())
J_aperiodic = list(jobs_aperiodic_dict.keys())
J_sporadic = list(jobs_sporadic_dict.keys())
J = J_periodic + J_aperiodic

J_charging = [c["job_id"] for c in processors["charging_jobs"]]
J_all = J + J_charging

J_hard = J_periodic

# Miss[j]: Aperiodic task j 是否逾期 (Binary 1 = 逾期, 0 = 準時)
Miss = LpVariable.dicts("Miss", J_aperiodic, cat="Binary")

# x[j][t]: Job j 在時間 t 是否執行 (Binary 0 或 1)
x = LpVariable.dicts("x", (J, T), cat="Binary")

# P[i][t]: 發電設備 i 在時間 t 的總供電量 (連續變數，大於等於 0)
P = LpVariable.dicts("P", (I_all, T), lowBound=0, cat="Continuous")

# k[j][i][t]: 設備 i 在時間 t 供應給 Job j 的電能量 (連續變數)
k = LpVariable.dicts("k", (J_all, I_all, T), lowBound=0, cat="Continuous")

# Sell[t]: 在時間 t 賣給市場的電量 (連續變數)
Sell = LpVariable.dicts("Sell", T, lowBound=0, cat="Continuous")

# SOC[i][t]: 電池 i 在時間 t 的剩餘電量 (連續變數)
SOC = LpVariable.dicts("SOC", (I_b, T), lowBound=0, cat="Continuous")

# is_chg[i][t]: 電池 i 在時間 t 是否正在充電 (Binary 1 充電, 0 放電或閒置)
is_chg = LpVariable.dicts("is_chg", (I_b, T), cat="Binary")

# ON[i][t]: 設備 i 在時間 t 是否啟動 (Binary 0 或 1)
is_generator_on = LpVariable.dicts("ON", (I_g, T), cat="Binary")

def Constraint_1():
    global BIG_LP
    for j in J:
        for t in T:
            total_k = lpSum(k[j][i][t] for i in I_all)
            BIG_LP += (total_k == jobs_all_dict[j]["w"] * x[j][t], f"Demand_Match_{j}_t{t}")

def Constraint_2():
    global BIG_LP
    for j in J:
        for t in T:
            if t < jobs_all_dict[j]["r"]:
                BIG_LP += (x[j][t] == 0, f"Release_Time_{j}_t{t}")
                for i in I_all:
                    BIG_LP += (k[j][i][t] == 0, f"No_Power_Before_Release_{j}_{i}_t{t}")

def Constraint_3():
    global BIG_LP
    for j in J_hard:
        total_x = lpSum(x[j][t] for t in range(jobs_all_dict[j]["r"], jobs_all_dict[j]["d"]))
        BIG_LP += (total_x == jobs_all_dict[j]["e"], f"Execution_Match_{j}")
        for t in T:
            if t >= jobs_all_dict[j]["d"]:
                BIG_LP += (x[j][t] == 0, f"Deadline_{j}_t{t}")
                for i in I_all:
                    BIG_LP += (k[j][i][t] == 0, f"No_Power_After_Deadline_{j}_{i}_t{t}")

def Constraint_4():
    global BIG_LP
    for j in J_aperiodic:
        r = jobs_all_dict[j]["r"]
        e = jobs_all_dict[j]["e"]
        d = jobs_all_dict[j]["d"]

        valid_end = min(d, 73)
        sum_x_before_dl = lpSum(x[j][t] for t in range(r, valid_end))

        BIG_LP += (sum_x_before_dl >= e * (1 - Miss[j]), f"Aperiodic_Miss_LB_{j}")
        BIG_LP += (sum_x_before_dl <= (e - 1) + e * (1 - Miss[j]), f"Aperiodic_Miss_UB_{j}")

        total_x = lpSum(x[j][t] for t in range(r, 73))
        BIG_LP += (total_x == e, f"Aperiodic_Total_Exec_{j}")

def Constraint_5():
    global BIG_LP

    s = LpVariable.dicts("start", (J, T), cat="Binary")
    for j in J:
        for t in T:
            prev_x = -1
            if t == 1:
                prev_x = 0
            else:
                prev_x = x[j][t - 1]

            if t == jobs_all_dict[j]["r"]:
                BIG_LP += (s[j][t] == x[j][t], f"Initial_Start_{j}_t{t}")
            elif t > jobs_all_dict[j]["r"]:
                BIG_LP += (s[j][t] >= x[j][t] - prev_x, f"Capture_Start_{j}_t{t}")

        if jobs_all_dict[j]["preempt"] == 0:
            end_t = 73 if j in J_aperiodic else jobs_all_dict[j]["d"]
            total_u = lpSum(s[j][t] for t in range(jobs_all_dict[j]["r"], end_t))
            BIG_LP += (total_u == 1, f"Non-preemptive_{j}")

def Constraint_6_to_10():
    global BIG_LP

    gen_data = {g["generator_id"]: g for g in processors["generator"]}
    for i in I_g:
        out_min = gen_data[i]["output_min"]
        out_max = gen_data[i]["output_max"]
        ru = gen_data[i]["ramp_up_rate"]
        rd = gen_data[i]["ramp_down_rate"]
        ut = gen_data[i]["min_up_time"]
        dt = gen_data[i]["min_down_time"]

        init_on = 1 if gen_data[i]["initial_on_time"] > 0 else 0
        init_p = gen_data[i]["initial_energy"]

        for t in T:
            BIG_LP += (P[i][t] >= out_min * is_generator_on[i][t], f"Power_Min_Limit_{i}_t{t}")
            BIG_LP += (P[i][t] <= out_max * is_generator_on[i][t], f"Power_Max_Limit_{i}_t{t}")

            if t == 1:
                prev_on = init_on
                prev_p = init_p
            else:
                prev_on = is_generator_on[i][t - 1]
                prev_p = P[i][t - 1]

            BIG_LP += (P[i][t] - prev_p <= ru, f"Ramp_Up_Limit_{i}_t{t}")
            BIG_LP += (prev_p - P[i][t] <= rd, f"Ramp_Down_Limit_{i}_t{t}")

            for tmp in range(t, min(73, t + ut)):
                BIG_LP += (is_generator_on[i][tmp] >= is_generator_on[i][t] - prev_on, f"Min_Up_Time_Enforce_{i}_t{t}_to_t{tmp}")

            for tmp in range(t, min(73, t + dt)):
                BIG_LP += (1 - is_generator_on[i][tmp] >= prev_on - is_generator_on[i][t], f"Min_Down_Time_Enforce_{i}_t{t}_to_t{tmp}")

def Constraint_11_12():
    global BIG_LP

    for g in processors["generator"]:
        i = g["generator_id"]
        ut = g["min_up_time"]       # 最小開機時間
        dt = g["min_down_time"]     # 最小關機時間
        tn = g["initial_on_time"]   # 排程前已連續開機多久
        tf = g["initial_off_time"]  # 排程前已連續關機多久

        if tn > 0 and tn < ut:
            remain_up = ut - tn
            for t in range(1, remain_up + 1):
                BIG_LP += (is_generator_on[i][t] == 1, f"Force_Initial_Up_{i}_t{t}")

        if tf > 0 and tf < dt:
            remain_down = dt - tf
            for t in range(1, remain_down + 1):
                BIG_LP += (is_generator_on[i][t] == 0, f"Force_Initial_Down_{i}_t{t}")

def Constraint_13():
    global BIG_LP

    cap_dict = {}
    for r in processors["renewable_capacity"]:
        key = r["renewable_id"]
        value = r["capacity"]
        cap_dict[key] = value

    forecast_dict = {}
    for r_data in processors["renewable_forecast"]:
        for r_id, f_list in r_data.items():
            forecast_dict[r_id] = f_list

    # 針對天氣ㄐ加入隨機性變化
    #烏雲為隨機事件
    realized_pv = {}
    for i in I_r:
        realized_pv[i] = {}

        cloud = 0

        for t in T:
            base_forecast = forecast_dict[i][t-1]["pv_forecast"]
            #有烏雲就減產70%~90%
            #沒烏雲有機會在未來出現烏雲並持續1到3小時
            #同時加入為小波動值 即使沒烏雲也會有+-15%的波動
            if cloud > 0:
                actual_pv = base_forecast * random.uniform(0.1, 0.3)
                cloud -= 1
            else:
                actual_pv = base_forecast * random.unimform(0.85, 1.15)
                if random.random() < 0.05:
                    cloud += random.randint(1, 3)
                    actual_pv = base_forecast * random.uniform(0.1, 0.3)

            actual_pv =  max(0.0, min(1.0, actual_pv))
            realized_pv[i][t] = actual_pv

            max_output = cap_dict[i] * actual_pv
            BIG_LP += (P[i][t] <= max_output, f"Renewable_Max_Limit_{i}_t{t}")

    return realized_pv

def Constraint_14_15():
    global BIG_LP
    dis_max = {b["storage_id"]: b["discharge_max"] for b in processors["storage"]}
    chg_max = {b["storage_id"]: b["charge_max"] for b in processors["storage"]}

    chg_job_map = {c["target_storage"]: c["job_id"] for c in processors["charging_jobs"]}
    for i in I_b:
        for t in T:
            BIG_LP += (P[i][t] <= dis_max[i], f"Battery_Max_Discharge_{i}_t{t}")

            j = chg_job_map[i]
            total_charge_received = lpSum(k[j][src][t] for src in I_g + I_r)
            BIG_LP += (total_charge_received <= chg_max[i], f"Battery_Max_Charge_{i}_t{t}")

def Constraint_16_to_19():
    global BIG_LP
    storage_data = {b["storage_id"]: b for b in processors["storage"]}
    chg_job_map = {c["target_storage"]: c["job_id"] for c in processors["charging_jobs"]} #和14_15一樣

    # --- 新增的真實世界電池參數 ---
    eta_chg = 0.93      # 充電效率 93%
    eta_dis = 0.93      # 放電效率 93%
    # 0.93*0.93 = 0.865 > 0.85的台電標準
    leak_rate = 0.005   # 每小時自放電漏掉 0.5%
    # --------------------------------------------------------

    for i in I_b:
        soc_init = storage_data[i]["soc_init"]       # 初始電量 (t=0)
        soc_min = storage_data[i]["soc_min"]         # 電池容量下限
        soc_max = storage_data[i]["soc_max"]         # 電池容量上限
        dis_max = storage_data[i]["discharge_max"]   # 最大放電功率
        chg_max = storage_data[i]["charge_max"]      # 最大充電功率
        j = chg_job_map[i]                           # 充這顆電池的充電任務ID

        for t in T:
            total_charge = lpSum(k[j][src][t] for src in I_g + I_r)

            if t == 1:
                prev_SOC = soc_init
            else:
                prev_SOC = SOC[i][t-1]

            # 修改加入自體放電、充電效率、放電效率的影響
            BIG_LP += (SOC[i][t] == prev_SOC * (1 - leak_rate) + total_charge * eta_chg - P[i][t] / eta_dis, f"SOC_Tracking_{i}_t{t}")

            BIG_LP += (SOC[i][t] >= soc_min, f"SOC_Minimum_Limit_{i}_t{t}")
            BIG_LP += (SOC[i][t] <= soc_max, f"SOC_Maximum_Limit_{i}_t{t}")

            BIG_LP += (P[i][t] <= prev_SOC - soc_min, f"Discharge_Safety_Limit_{i}_t{t}")

            BIG_LP += (total_charge <= chg_max * is_chg[i][t], f"Exclusivity_Charge_{i}_t{t}")
            BIG_LP += (P[i][t] <= dis_max * (1 - is_chg[i][t]), f"Exclusivity_Discharge_{i}_t{t}")

def Constraint_20():
    global BIG_LP

    for i in I_g + I_r:
        for t in T:
            total = lpSum(k[j][i][t] for j in J_all)
            BIG_LP += (total <= P[i][t], f"Power_Distribution_Limit_{i}_t{t}")

    for i in I_b:
        for t in T:
            total= lpSum(k[j][i][t] for j in J) #只能分配給一般用電任務 J
            BIG_LP += (total <= P[i][t], f"Battery_Discharge_Limit_{i}_t{t}")

def Constraint_21():
    global BIG_LP

    for j in J_charging:
        for i in I_b:
            for t in T:
                BIG_LP += (k[j][i][t] == 0, f"No_Battery_To_Battery_Charge_{j}_{i}_t{t}")

def Constraint_22_23():
    global BIG_LP

    for t in T:
        total_generated = lpSum(P[i][t] for i in I_all)

        total_consumed = lpSum(k[j][i][t] for j in J_all for i in I_all)

        BIG_LP += (total_generated == total_consumed + Sell[t], f"System_Energy_Balance_t{t}")

Constraint_1()
Constraint_2()
Constraint_3()
Constraint_4()
Constraint_5()
Constraint_6_to_10()
Constraint_11_12()
#接收新數據來重新抓取排程
actual_weather = Constraint_13()
Constraint_14_15()
Constraint_16_to_19()
Constraint_20()
Constraint_21()
Constraint_22_23()

gen_costs = {g["generator_id"]: {"fixed": g["cost_fixed"], "var": g["cost_variable"]} for g in processors["generator"]}
price_dict = {p["hour"]: p["market_price"] for p in prices["price"]}

#將電價也加入不確定性因素
#小機率市場飽和要倒貼
#小機率市場稀缺會翻倍
#其餘時候都小幅波動 +-10%
rt_price_dict = {}
for t in T:
    if random.random() < 0.05:
        rt_price_dict[t] = price_dict[t] * random.uniform(-2.0, -0.5)
    elif random.random() < 0.10:
        rt_price_dict[t] = price_dict[t] * random.uniform(3.0, 5.0)
    else:
        rt_price_dict[t] = price_dict[t] * random.uniform(0.9, 1.1)

#新增常數，每次充放電的成本
aging_cost = 5.0

f1 = lpSum(Miss[j] for j in J_aperiodic) * 10000
f2 = lpSum(gen_costs[i]["fixed"] * is_generator_on[i][t] + gen_costs[i]["var"] * P[i][t] for i in I_g for t in T)
f3 = -lpSum(rt_price_dict[t] * Sell[t] for t in T)

#新增新目標 避免電池老化
chg_job_map = {c["target_storage"]: c["job_id"] for c in processors["charging_jobs"]}
f4 = lpSum(aging_cost * (lpSum(k[chg_job_map[i]][src][t] for src in I_g + I_r) + P[i][t]) for i in I_b for t in T)

BIG_LP += f1 + f2 + f3 + f4, "Total_Objective"

print("模型建置完成，開始求解...")
BIG_LP.solve()

print(f"求解狀態: {LpStatus[BIG_LP.status]}")


if BIG_LP.status == 1:
    print(f"\n總目標函數值: {value(BIG_LP.objective)}")

    actual_f2 = sum(gen_costs[i]["fixed"] * is_generator_on[i][t].varValue + gen_costs[i]["var"] * P[i][t].varValue for i in I_g for t in T)
    actual_f3 = sum(price_dict[t] * Sell[t].varValue for t in T)

    print(f"傳統機組發電總成本: {actual_f2}")
    print(f"市場售電總收益: {actual_f3}")

    print("\n--- Aperiodic 任務逾期結算 ---")
    miss_count = 0
    for j in J_aperiodic:
        if Miss[j].varValue == 1:
            print(f"[警告] 任務 {j} 逾期了！")
            miss_count += 1
        else:
            print(f"[成功] 任務 {j} 準時完成！")

    print(f"\n總共 Miss 了 {miss_count} 個 Aperiodic 任務。")
else:
    print("模型無解，請檢查限制式或測資是否有衝突！")


print("\n--- 開始執行 Sporadic 任務 Acceptance Test ---")

slack = {t: Sell[t].varValue for t in T}

accepted_sporadic = []
rejected_sporadic = []
sporadic_allocations = {}

for j in J_sporadic:
    r = jobs_all_dict[j]["r"]
    d = jobs_all_dict[j]["d"]
    e = jobs_all_dict[j]["e"]
    w = jobs_all_dict[j]["w"]
    is_preempt = jobs_all_dict[j]["preempt"]

    valid_end = min(d, 73)
    available_hours = []

    if is_preempt == 1:
        for t in range(r, valid_end):
            if slack[t] >= w:
                available_hours.append(t)
            if len(available_hours) == e:
                break
    else:
        consecutive_count = 0
        temp_hours = []
        for t in range(r, valid_end):
            if slack[t] >= w:
                consecutive_count += 1
                temp_hours.append(t)
                if consecutive_count == e:
                    available_hours = temp_hours.copy()
                    break
            else:
                consecutive_count = 0
                temp_hours = []

    if len(available_hours) == e:
        accepted_sporadic.append(j)
        sporadic_allocations[j] = available_hours
        for t in available_hours:
            slack[t] -= w
        print(f"[Accept] 接受任務 {j}，安排於時段 {available_hours}")
    else:
        rejected_sporadic.append(j)
        print(f"[Reject] 拒絕任務 {j}")

print(f"\n總結:接受了 {len(accepted_sporadic)} 個，拒絕了 {len(rejected_sporadic)} 個 Sporadic 任務。")

lost_revenue = 0
for j in accepted_sporadic:
    for t in sporadic_allocations[j]:
        lost_revenue += jobs_all_dict[j]["w"] * price_dict[t]

final_revenue = actual_f3 - lost_revenue
final_objective = value(BIG_LP.objective) + lost_revenue

print("\n--- 最終財報結算 (包含 Sporadic 影響) ---")
print(f"原本預期售電收益: {actual_f3}")
print(f"因接受 Sporadic 犧牲的售電收益: {lost_revenue}")
print(f"最終實際售電收益: {final_revenue}")
print(f"最終總目標函數值: {final_objective}")

schedule_result = []

for t in T:
    P_dict = {i: round(P[i][t].varValue, 2) for i in I_all}

    k_dict = {}
    for j in J_all:
        allocations = {i: round(k[j][i][t].varValue, 2) for i in I_all if k[j][i][t].varValue > 0}
        if allocations:
            k_dict[j] = allocations

    for j in accepted_sporadic:
        if t in sporadic_allocations[j]:
            k_dict[j] = {"slack_reserve": jobs_all_dict[j]["w"]}

    sell_val = round(slack[t], 2)
    soc_dict = {i: round(SOC[i][t].varValue, 2) for i in I_b}

    time_slot = {
        "t": t,
        "P": P_dict,
        "k": k_dict,
        "sell": sell_val,
        "soc": soc_dict,
        "missed_aperiodic": [j for j in J_aperiodic if Miss[j].varValue == 1],
        "rejected_sporadic": rejected_sporadic
    }
    schedule_result.append(time_slot)

output_data = {"schedule_result": schedule_result}
with open("../output/schedule_result.json", "w", encoding="utf-8") as f:
    json.dump(output_data, f, ensure_ascii=False, indent=4)

print("\n[成功]排程結果已儲存至 ../output/schedule_result.json")