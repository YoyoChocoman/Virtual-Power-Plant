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

T = range(1, 73)
I_g = [g["generator_id"] for g in processors["generator"]]
I_r = [r["renewable_id"] for r in processors["renewable_capacity"]]
I_b = [b["storage_id"] for b in processors["storage"]]
I_all = I_g + I_r + I_b

J_periodic = list(jobs_periodic_dict.keys())
J_aperiodic = list(jobs_aperiodic_dict.keys())
J_sporadic = list(jobs_sporadic_dict.keys())
J_charging = [c["job_id"] for c in processors["charging_jobs"]]
J_all = J_periodic + J_aperiodic + J_sporadic + J_charging

# -------------------- 為了後續動態規劃先把浮動值拉出來 ---------------------------------
cap_dict = {r["renewable_id"]: r["capacity"] for r in processors["renewable_capacity"]}
forecast_dict = {r_id: f_list for r_data in processors["renewable_forecast"] for r_id, f_list in r_data.items()}
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
            actual_pv = base_forecast * random.uniform(0.85, 1.15)
            if random.random() < 0.05:
                cloud += random.randint(1, 3)
                actual_pv = base_forecast * random.uniform(0.1, 0.3)

        actual_pv =  max(0.0, min(1.0, actual_pv))
        realized_pv[i][t] = actual_pv

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
# ------------------------------------------------------------------------------------

#動態規劃
def reschedule(current_time, active_sporadics, history):
    BIG_LP = LpProblem("VPP_Scheduling", LpMinimize)

    J_current = J_periodic + J_aperiodic + active_sporadics
    J_all_current = J_current + J_charging
    J_hard_current = J_periodic + active_sporadics

    #變數宣告
    Miss = LpVariable.dicts("Miss", J_aperiodic, cat="Binary")
    X = LpVariable.dicts("x", (J_periodic + J_aperiodic, T), cat="Binary")
    P = LpVariable.dicts("P", (I_all, T), lowBound=0, cat="Continuous")
    K = LpVariable.dicts("k", (J_all, I_all, T), lowBound=0, cat="Continuous")
    Sell = LpVariable.dicts("Sell", T, lowBound=0, cat="Continuous")
    SOC = LpVariable.dicts("SOC", (I_b, T), lowBound=0, cat="Continuous")
    is_chg = LpVariable.dicts("is_chg", (I_b, T), cat="Binary")
    is_generator_on = LpVariable.dicts("ON", (I_g, T), cat="Binary")
    s = LpVariable.dicts("start", (J_current, T), cat="Binary")

    #已經過去的時間段保持不變
    for past in range(1, current_time):
        record = history[past]
        BIG_LP += (P[i][past] == record["Sell"])
        for i in I_g:
            BIG_LP += (is_generator_on[i][past] == record["ON"][i])
            BIG_LP += (P[i][past] == record["P"][i])
        for i in I_b:
            BIG_LP += (SOC[i][past] == record["SOC"][i])
            BIG_LP += (is_chg[i][past] == record["is_chg"][i])
        for j in J_current:
            # 有些 sporadic 未來才來則不可能執行過
            past_x = record["x"].get(j, 0)
            BIG_LP += (X[j][past] == past_x)

    gen_data = {g["generator_id"]: g for g in processors["generator"]}
    storage_data = {b["storage_id"]: b for b in processors["storage"]}
    chg_job_map = {c["target_storage"]: c["job_id"] for c in processors["charging_jobs"]}

    eta_chg, eta_dis, leak_rate = 0.93, 0.93, 0.005

    for t in T:
        # C22_23
        total_gen = lpSum(P[i][t] for i in I_all)
        total_con = lpSum(K[j][i][t] for j in J_all_current for i in I_all)
        BIG_LP += (total_gen == total_con + Sell[t])

        # C1, C2
        for j in J_current:
            BIG_LP += (lpSum(K[j][i][t] for i in I_all) == jobs_all_dict[j]["w"] * X[j][t])
            if t < jobs_all_dict[j]["r"]:
                BIG_LP += (X[j][t] == 0)

        # C13
        for i in I_r:
            # 未來的時間用預測值 過去以支用實際值！
            if t <= current_time:
                actual_pv = realized_pv[i][t]
            else:
                actual_pv = forecast_dict[i][t-1]["pv_forecast"]
            BIG_LP += (P[i][t] <= cap_dict[i] * actual_pv)

        # C6~C12
        for i in I_g:
            BIG_LP += (P[i][t] >= gen_data[i]["output_min"] * is_generator_on[i][t])
            BIG_LP += (P[i][t] <= gen_data[i]["output_max"] * is_generator_on[i][t])

            prev_on = is_generator_on[i][t-1] if t > 1 else (1 if gen_data[i]["initial_on_time"] > 0 else 0)
            prev_p = P[i][t-1] if t > 1 else gen_data[i]["initial_energy"]

            BIG_LP += (P[i][t] - prev_p <= gen_data[i]["ramp_up_rate"])
            BIG_LP += (prev_p - P[i][t] <= gen_data[i]["ramp_down_rate"])

            for tmp in range(t, min(73, t + gen_data[i]["min_up_time"])):
                BIG_LP += (is_generator_on[i][tmp] >= is_generator_on[i][t] - prev_on)
            for tmp in range(t, min(73, t + gen_data[i]["min_down_time"])):
                BIG_LP += (1 - is_generator_on[i][tmp] >= prev_on - is_generator_on[i][t])

        # C14~C19
        for i in I_b:
            prev_SOC = SOC[i][t-1] if t > 1 else storage_data[i]["soc_init"]
            j_c = chg_job_map[i]
            tot_charge = lpSum(K[j_c][src][t] for src in I_g + I_r)

            BIG_LP += (SOC[i][t] == prev_SOC * (1 - leak_rate) + tot_charge * eta_chg - P[i][t] / eta_dis)
            BIG_LP += (SOC[i][t] >= storage_data[i]["soc_min"])
            BIG_LP += (SOC[i][t] <= storage_data[i]["soc_max"])
            BIG_LP += (P[i][t] <= storage_data[i]["discharge_max"])
            BIG_LP += (tot_charge <= storage_data[i]["charge_max"])
            BIG_LP += (P[i][t] <= prev_SOC - storage_data[i]["soc_min"])
            BIG_LP += (tot_charge <= storage_data[i]["charge_max"] * is_chg[i][t])
            BIG_LP += (P[i][t] <= storage_data[i]["discharge_max"] * (1 - is_chg[i][t]))
            BIG_LP += (lpSum(K[j_c][i_b][t] for i_b in I_b) == 0)

    for j in J_hard_current:
        BIG_LP += (lpSum(X[j][t] for t in range(jobs_all_dict[j]["r"], jobs_all_dict[j]["d"])) == jobs_all_dict[j]["e"])
        for t in T:
            if t >= jobs_all_dict[j]["d"]: BIG_LP += (X[j][t] == 0)

    for j in J_aperiodic:
        r, d, e = jobs_all_dict[j]["r"], jobs_all_dict[j]["d"], jobs_all_dict[j]["e"]
        sum_x_before = lpSum(X[j][t] for t in range(r, min(d, 73)))
        BIG_LP += (sum_x_before >= e * (1 - Miss[j]))
        BIG_LP += (sum_x_before <= (e - 1) + e * (1 - Miss[j]))
        BIG_LP += (lpSum(X[j][t] for t in range(r, 73)) == e)

    aging_cost = 5.0
    f1 = lpSum(Miss[j] for j in J_aperiodic) * 10000
    f2 = lpSum(gen_data[i]["cost_fixed"] * is_gen_on[i][t] + gen_data[i]["cost_variable"] * P[i][t] for i in I_g for t in T)
    f3 = -lpSum(rt_price_dict[t] * Sell[t] for t in T)
    f4 = lpSum(aging_cost * (lpSum(K[chg_job_map[i]][src][t] for src in I_g + I_r) + P[i][t]) for i in I_b for t in T)
    BIG_LP += f1 + f2 + f3 + f4

    BIG_LP.solve(PULP_CBC_CMD(msg=0))

    if BIG_LP.status == 1:
        ans = {
            "x": {j: {t: X[j][t].varValue for t in T} for j in J_current},
            "P": {i: {t: P[i][t].varValue for t in T} for i in I_all},
            "k": {j: {i: {t: K[j][i][t].varValue for t in T} for i in I_all} for j in J_all_current},
            "Sell": {t: Sell[t].varValue for t in T},
            "SOC": {i: {t: SOC[i][t].varValue for t in T} for i in I_b},
            "ON": {i: {t: is_generator_on[i][t].varValue for t in T} for i in I_g},
            "is_chg": {i: {t: is_chg[i][t].varValue for t in T} for i in I_b},
            "Miss": {j: Miss[j].varValue for j in J_aperiodic}
        }
        return 1, ans

    return 0, None


# ------------ 主程式迴圈 ---------------------
history = {}
accepted_sporadic = []
rejected_sporadic = []

status, current_plan = reschedule(1, accepted_sporadic, history)
if status != 1:
    exit()

for current in T:
    need_reschedule = False

    # 檢查是否有突發 Sporadic 任務
    arrived_sporadics = [j for j in J_sporadic if jobs_all_dict[j]["r"] == current]

    # 檢查真實天氣是否比預期差很多 (發電驟降)
    for i in I_r:
        actual = realized_pv[i][current]
        forecast = forecast_dict[i][current-1]["pv_forecast"]
        if actual < forecast * 0.7: # 真實發電不到預測70%
            need_reschedule = True

    # 有新任務或是與預期落差過大才重新排程
    if arrived_sporadics or need_reschedule:
        # 塞新任務
        test_sporadics = accepted_sporadic + arrived_sporadics
        status, test_plan = reschedule(current, test_sporadics, history)

        if status == 1:
            accepted_sporadic.extend(arrived_sporadics)
            current_plan = test_plan
        else:
            rejected_sporadic.extend(arrived_sporadics)
            if need_reschedule:
                status, weather_plan = reschedule(current, accepted_sporadic, history)
                if status == 1:
                    print(" 雖然拒絕了新任務，但已成功針對惡劣天氣調整了後續計畫。")
                    current_plan = weather_plan
                else:
                    print(" 嚴重警告：惡劣天氣導致系統無解 (需拉黑負載)！此模擬繼續使用原定計畫...")

    history[current] = {
        "x": {j: current_plan["x"][j][current] for j in current_plan["x"]},
        "P": {i: current_plan["P"][i][current] for i in I_all},
        "SOC": {i: current_plan["SOC"][i][current] for i in I_b},
        "ON": {i: current_plan["ON"][i][current] for i in I_g},
        "is_chg": {i: current_plan["is_chg"][i][current] for i in I_b},
        "Sell": current_plan["Sell"][current]
    }

# ----------------- 輸出 -----------------------------
schedule_result = []
for t in T:
    P_dict = {i: round(history[t]["P"][i], 2) for i in I_all}
    k_dict = {}
    for j in J_periodic + J_aperiodic + accepted_sporadic + J_charging:
        allocations = {i: round(current_plan["k"][j][i][t], 2) for i in I_all if current_plan["k"][j][i][t] > 0}
        if allocations:
            k_dict[j] = allocations

    time_slot = {
        "t": t,
        "P": P_dict,
        "k": k_dict,
        "sell": round(history[t]["Sell"], 2),
        "soc": {i: round(history[t]["SOC"][i], 2) for i in I_b},
        "missed_aperiodic": [j for j in J_aperiodic if current_plan["Miss"][j] == 1],
        "rejected_sporadic": rejected_sporadic
    }
    schedule_result.append(time_slot)

with open("../output/schedule_result_dynamic.json", "w", encoding="utf-8") as f:
    json.dump({"schedule_result": schedule_result}, f, ensure_ascii=False, indent=4)