import json
from pulp import *

with open("../output/task_set.json", "r", encoding="utf-8") as f:
    task_data = json.load(f)

with open("../input/processor_settings.json", "r", encoding="utf-8") as f:
    processors = json.load(f)

with open("../input/price_72hr.json", "r", encoding="utf-8") as f:
    prices = json.load(f)

SHIFT = 0

jobs_periodic_dict = {}
for task, info in task_data["periodic"].items():
    for i in range(72 // info["p"]):
        if info["r"] + info["p"] * i + info["d"] > 72: break

        jobs_periodic_dict[f"{task}_{i}"] = {
            "r": info["r"] + info["p"] * i + SHIFT,
            "d": info["r"] + info["p"] * i + info["d"] + SHIFT,
            "e": info["e"],
            "w": info["w"],
            "preempt": info["preempt"]
        }

jobs_aperiodic_dict = task_data.get("aperiodic", {})
for task, info in task_data.get("aperiodic", {}).items():
    jobs_aperiodic_dict[task] = {
        "r": info["r"] + SHIFT,
        "d": info["r"] + info["d"] + SHIFT, # 轉為 absolute deadline
        "e": info["e"],
        "w": info["w"],
        "preempt": info["preempt"]
    }

jobs_sporadic_dict = {}
for task, info in task_data.get("sporadic", {}).items():
    jobs_sporadic_dict[task] = {
        "r": info["r"] + SHIFT,
        "d": info["r"] + info["d"] + SHIFT, # 轉為 absolute deadline
        "e": info["e"],
        "w": info["w"],
        "preempt": info["preempt"]
    }

# 合併字典
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

J_charging = [c["job_id"] for c in processors["charging_jobs"]]
J_all = J_periodic + J_aperiodic + J_charging

J_hard = J_periodic

# Miss[j]: Aperiodic task j 是否逾期 (Binary 1 = 逾期, 0 = 準時)
MISS = LpVariable.dicts("Miss", J_aperiodic, cat="Binary")

# x[j][t]: Job j 在時間 t 是否執行 (Binary 0 或 1)
X = LpVariable.dicts("x", (J_periodic + J_aperiodic, T), cat="Binary")

# P[i][t]: 發電設備 i 在時間 t 的總供電量 (連續變數，大於等於 0)
P = LpVariable.dicts("P", (I_all, T), lowBound=0, cat="Continuous")

# k[j][i][t]: 設備 i 在時間 t 供應給 Job j 的電能量 (連續變數)
K = LpVariable.dicts("k", (J_all, I_all, T), lowBound=0, cat="Continuous")

# Sell[t]: 在時間 t 賣給市場的電量 (連續變數)
SELL = LpVariable.dicts("Sell", T, lowBound=0, cat="Continuous")

# SOC[i][t]: 電池 i 在時間 t 的剩餘電量 (連續變數)
SOC = LpVariable.dicts("SOC", (I_b, T), lowBound=0, cat="Continuous")

# is_chg[i][t]: 電池 i 在時間 t 是否正在充電 (Binary 1 充電, 0 放電或閒置)
is_chg = LpVariable.dicts("is_chg", (I_b, T), cat="Binary")

# is_gen_on[i][t]: 設備 i 在時間 t 是否啟動 (Binary 0 或 1)
is_gen_on = LpVariable.dicts("ON", (I_g, T), cat="Binary")

#2 電力保留缺口
Shortfall = LpVariable.dicts("Reserved", T, lowBound=0, cat="Continuous")


def Constraint_1():
    # 若用電需求 𝑗 在時間點 𝑡 用電，則獲得的總電能量必須為 𝑤_j
    global BIG_LP
    for j in J_periodic + J_aperiodic:
        for t in T:
            total_k = lpSum(K[j][i][t] for i in I_all)
            BIG_LP += (total_k == jobs_all_dict[j]["w"] * X[j][t], f"Demand_Match_{j}_t{t}")

def Constraint_2():
    # 用電需求 𝑗 不可在 release time 前執行
    global BIG_LP
    for j in J_periodic + J_aperiodic:
        for t in T:
            if t < jobs_all_dict[j]["r"]:
                BIG_LP += (X[j][t] == 0, f"Release_Time_{j}_t{t}")
                for i in I_all:
                    BIG_LP += (K[j][i][t] == 0, f"No_Power_Before_Release_{j}_{i}_t{t}")

def Constraint_3():
    # 用電需求 𝑗 ∈ 𝐽𝑝 ∪ 𝐽𝑠 必須在 deadline 前做滿所需時間段 𝑒_j
    global BIG_LP
    for j in J_hard:
        total_x = lpSum(X[j][t] for t in range(jobs_all_dict[j]["r"], jobs_all_dict[j]["d"]))
        BIG_LP += (total_x == jobs_all_dict[j]["e"], f"Execution_Match_{j}")
        for t in T:
            if t >= jobs_all_dict[j]["d"]:
                BIG_LP += (X[j][t] == 0, f"Deadline_{j}_t{t}")
                for i in I_all:
                    BIG_LP += (K[j][i][t] == 0, f"No_Power_After_Deadline_{j}_{i}_t{t}")

def Constraint_4():
    # Aperiodic task miss 定義
    # 若 𝑀𝑖𝑠𝑠𝑗 = 0，表示 aperiodic task 𝑗 需在 deadline 前完成
    # 若 𝑀𝑖𝑠𝑠𝑗 = 1，表示 aperiodic task 𝑗 未於 deadline 前完成，並記錄為 deadline miss
    global BIG_LP
    for j in J_aperiodic:
        r = jobs_all_dict[j]["r"]
        e = jobs_all_dict[j]["e"]
        d = jobs_all_dict[j]["d"]

        # 確保上限不超過排程總長度 73
        valid_end = min(d, 73)

        # 計算在 deadline 之前實際做了多少小時
        sum_x_before_dl = lpSum(X[j][t] for t in range(r, valid_end))

        # 若 𝑀𝑖𝑠𝑠𝑗 = 0，表示 𝑗_a 需在 deadline 前完成
        BIG_LP += (sum_x_before_dl >= e * (1 - MISS[j]), f"Aperiodic_Miss_LB_{j}")

        # 若 𝑀𝑖𝑠𝑠𝑗 = 1，表示 j_a 未於 deadline 前完成，記錄為 deadline miss
        BIG_LP += (sum_x_before_dl <= (e - 1) + e * (1 - MISS[j]), f"Aperiodic_Miss_UB_{j}")

        # 用電需求 𝑗 ∈ 𝐽𝑎 必須在最後一個離散時間點 𝑯 前做滿所需時間段 𝑒𝑗。 (就算 miss 也還是要做完的)
        total_x = lpSum(X[j][t] for t in range(r, 73))
        BIG_LP += (total_x == e, f"Aperiodic_Total_Exec_{j}")

def Constraint_5():
    global BIG_LP
    # non-preemptive 任務須連續執行
    # s[j][t]: Job j 在時間 t 是否啟動 (Binary 0 或 1)
    s = LpVariable.dicts("start", (J_periodic + J_aperiodic, T), cat="Binary")
    for j in J_periodic + J_aperiodic:
        for t in T:
            # 強制 s[j][t] 只能在 x[j][t] 從 0 變成 1 的時候設為 1 (啟動)
            prev_x = -1
            if t == 1:
                prev_x = 0
            else:
                prev_x = X[j][t - 1]

            if t == jobs_all_dict[j]["r"]:
                BIG_LP += (s[j][t] == X[j][t], f"Initial_Start_{j}_t{t}")
            elif t > jobs_all_dict[j]["r"]:
                BIG_LP += (s[j][t] >= X[j][t] - prev_x, f"Capture_Start_{j}_t{t}")

        if jobs_all_dict[j]["preempt"] == 0: # 是 non-preemptive
            end_t = 73 if j in J_aperiodic else jobs_all_dict[j]["d"]
            total_u = lpSum(s[j][t] for t in range(jobs_all_dict[j]["r"], end_t))
            BIG_LP += (total_u == 1, f"Non-preemptive_{j}") # 只能啟動 1 次

def Constraint_6_to_10():
    # 傳統機組出力上下限
    # 傳統機組 ramp-up / ramp-down 不可標過上限
    # 傳統機組最小出力不可大於單一時間段可達到之 ramp-up 能力
    # 若傳統機組 𝑖 在時間點 𝑡 由關機狀態轉為開機狀態，則該機組自時間 𝑡 起，必須至少連續維持開機 𝑈𝑇_𝑖 個時間段
    global BIG_LP
    gen_data = {g["generator_id"]: g for g in processors["generator"]}

    for i in I_g:
        out_min = gen_data[i]["output_min"]
        out_max = gen_data[i]["output_max"]
        ru = gen_data[i]["ramp_up_rate"]
        rd = gen_data[i]["ramp_down_rate"]
        ut = gen_data[i]["min_up_time"]
        dt = gen_data[i]["min_down_time"]

        # 取得 t = 0 的狀態
        init_on = 1 if gen_data[i]["initial_on_time"] > 0 else 0
        init_p = gen_data[i]["initial_energy"]

        for t in T:
            # Constraint 6: 傳統機組出力上下限
            BIG_LP += (P[i][t] >= out_min * is_gen_on[i][t], f"Power_Min_Limit_{i}_t{t}")
            BIG_LP += (P[i][t] <= out_max * is_gen_on[i][t], f"Power_Max_Limit_{i}_t{t}")

            if t == 1:
                prev_on = init_on
                prev_p = init_p
            else:
                prev_on = is_gen_on[i][t - 1]
                prev_p = P[i][t - 1]

            # Constraint 7: 傳統機組 ramp-up / ramp-down 不可標過上限 （註：感覺要分情況但由酷酷的數學式可以直接這樣簡化）
            BIG_LP += (P[i][t] - prev_p <= ru, f"Ramp_Up_Limit_{i}_t{t}")
            BIG_LP += (prev_p - P[i][t] <= rd, f"Ramp_Down_Limit_{i}_t{t}")

            # Constraint 8: 傳統機組最小出力不可大於單一時間段可達到之 ramp-up 能力。
            # 這不用管 這在我們這裡恆成立，因為我們的最小出力已經在 Constraint 6 的上下限裡面定義了，而 ramp-up 能力通常會大於等於最小出力，所以不會有衝突的情況

            # Constraint 9, 10 有點難解釋，用註解打不清楚，有問題再問我
            # 還挺清楚的 666

            # Constraint 9: 若傳統機組 𝑖 在時間點 𝑡 由關機狀態轉為開機狀態，則該機組自時間點 𝑡 起，必須至少連續維持開機 𝑈𝑇_i 個時間段。
            # 開機動作：(is_gen_on[i][t] - prev_on) 如果等於 1，代表這一刻開機了
            # 那未來符合最小開機時間的每個小時（未來第 tmp 小時），is_gen_on 都必須等於 1
            for tmp in range(t, min(73, t + ut)):
                BIG_LP += (is_gen_on[i][tmp] >= is_gen_on[i][t] - prev_on, f"Min_Up_Time_Enforce_{i}_t{t}_to_t{tmp}")

            # Constraint 10: 最小關機時間 (Min Down Time)
            # 關機動作：(prev_on - is_gen_on[i][t]) 如果等於 1，代表這一刻關機了
            # 那未來符合最小關機時間的每個小時（未來第 tmp 小時），is_gen_on 都必須等於 0
            for tmp in range(t, min(73, t + dt)):
                BIG_LP += (1 - is_gen_on[i][tmp] >= prev_on - is_gen_on[i][t], f"Min_Down_Time_Enforce_{i}_t{t}_to_t{tmp}")

def Constraint_11_12():
    global BIG_LP
    # 若傳統機組 𝑖 在排程起點前已處於開機狀態，且其已連續開機時間尚未滿足最小開機時間 𝑈𝑇_𝑖，則需補足剩餘的最小開機時間。
    # 若傳統機組 𝑖 在排程起點前已處於關機狀態，且其已連續關機時間尚未滿足最小關機時間 𝐷𝑇_𝑖，則需補足剩餘的最小關機時間。
    for g in processors["generator"]:
        i = g["generator_id"]
        ut = g["min_up_time"]       # 最小開機時間
        dt = g["min_down_time"]     # 最小關機時間
        tn = g["initial_on_time"]   # 排程前已連續開機多久
        tf = g["initial_off_time"]  # 排程前已連續關機多久

        # Constraint 11: 已經開機，但還沒滿足最小開機時間
        if tn > 0 and tn < ut:
            remain_up = ut - tn
            for t in range(1, remain_up + 1):
            # for t in range(1, min(remain_up, 72) + 1):
                BIG_LP += (is_gen_on[i][t] == 1, f"Force_Initial_Up_{i}_t{t}")

        # Constraint 12: 已經關機，但還沒滿足最小關機時間
        if tf > 0 and tf < dt:
            remain_down = dt - tf
            for t in range(1, remain_down + 1):
                BIG_LP += (is_gen_on[i][t] == 0, f"Force_Initial_Down_{i}_t{t}")

def Constraint_13():
    global BIG_LP
    # 再生能源在每一時間點的出力，不可標過該時段預測可用電能量。

    # 建 capacity 字典 {key: 發電機 id, value: 該發電機可提供的電量}
    cap_dict = {}
    for r in processors["renewable_capacity"]:
        key = r["renewable_id"]
        value = r["capacity"]
        cap_dict[key] = value

    # 建 forecast 字典 {key: 發電機 id, value: 存該發電機每個時間能提供的出力百分比}
    forecast_dict = {}
    for r_data in processors["renewable_forecast"]:
        for r_id, f_list in r_data.items():
            forecast_dict[r_id] = f_list

    for i in I_r:
        for t in T:
            # hour 從 1 開始，陣列 index 從 0 開始
            max_output = cap_dict[i] * forecast_dict[i][t - 1]["pv_forecast"]

            # 發電量 P 必須小於等於最大預測值
            BIG_LP += (P[i][t] <= max_output, f"Renewable_Max_Limit_{i}_t{t}")

def Constraint_14_15():
    global BIG_LP
    # 儲能設備 𝑖 在每一時間點的放電量 𝑃𝑖,𝑡，不得標過其單位時間最大放電能力。
    # 儲能設備 𝑖 在時間點 𝑡 接收之總充電量不得標過其單位時間最大充電能力。

    # 建最大充放電字典 {key: 儲能設備 id, value: 最大充放電值}
    dis_max = {battery["storage_id"]: battery["discharge_max"] for battery in processors["storage"]}
    chg_max = {battery["storage_id"]: battery["charge_max"] for battery in processors["storage"]}

    # mapping 這顆電池對應到哪個任務
    chg_job_map = {chg_job["target_storage"]: chg_job["job_id"] for chg_job in processors["charging_jobs"]}
    for i in I_b:
        for t in T:
            # Constraint 14: 電池每小時放電量 P 不能超過 discharge_max
            BIG_LP += (P[i][t] <= dis_max[i], f"Battery_Max_Discharge_{i}_t{t}")

            # Constraint 15: 電池每小時充電量不能超過 charge_max
            j = chg_job_map[i] # 充這顆電池的充電任務 ID

            # source = 傳統機組 + 再生能源 (I_g + I_r)
            total_charge_received = lpSum(K[j][src][t] for src in I_g + I_r)
            BIG_LP += (total_charge_received <= chg_max[i], f"Battery_Max_Charge_{i}_t{t}")

def Constraint_16_to_19():
    global BIG_LP
    storage_data = {battery["storage_id"]: battery for battery in processors["storage"]}
    chg_job_map = {chg_job["target_storage"]: chg_job["job_id"] for chg_job in processors["charging_jobs"]} # 和 14_15 一樣

    for i in I_b:
        soc_init = storage_data[i]["soc_init"]       # 初始電量 (t = 0)
        soc_min = storage_data[i]["soc_min"]         # 電池容量下限
        soc_max = storage_data[i]["soc_max"]         # 電池容量上限
        dis_max = storage_data[i]["discharge_max"]   # 最大放電功率
        chg_max = storage_data[i]["charge_max"]      # 最大充電功率
        j = chg_job_map[i]                           # 充這顆電池的充電任務ID

        for t in T:
            # 充進來的電量
            total_charge_received = lpSum(K[j][src][t] for src in I_g + I_r)

            if t == 1:
                prev_SOC = soc_init
            else:
                prev_SOC = SOC[i][t - 1]

            # Constraint 16: 儲能設備 𝑖 在時間點 𝑡 的儲存電能量，等於前一時間點的儲存電能量，加上該時段充電量，並扣除該時段放電量。
            BIG_LP += (SOC[i][t] == prev_SOC + total_charge_received - P[i][t], f"SOC_Tracking_{i}_t{t}")

            # Constraint 17: 儲能設備 𝑖 儲存量上下限。
            BIG_LP += (SOC[i][t] >= soc_min, f"SOC_Minimum_Limit_{i}_t{t}")
            BIG_LP += (SOC[i][t] <= soc_max, f"SOC_Maximum_Limit_{i}_t{t}")

            # Constraint 18: 儲能設備 𝑖 不能放出超過最低存量的電能。
            BIG_LP += (P[i][t] <= prev_SOC - soc_min, f"Discharge_Safety_Limit_{i}_t{t}")

            # Constraint 19: 同一個儲能設備 𝑖 同一時間點 𝑡 不可同時充電又放電。
            # 1. 如果 is_chg 是 1 (充電)
            # total_charge <= chg_max * 1 (可以充電)
            # P[i][t] <= dis_max * 0      (強制放電為 0)
            # 2. 如果 is_chg 是 0 (決定放電)
            # total_charge <= chg_max * 0 (強制充電為 0)
            # P[i][t] <= dis_max * 1      (可以放電)

            BIG_LP += (total_charge_received <= chg_max * is_chg[i][t], f"Exclusivity_Charge_{i}_t{t}")
            BIG_LP += (P[i][t] <= dis_max * (1 - is_chg[i][t]), f"Exclusivity_Discharge_{i}_t{t}")

def Constraint_20():
    global BIG_LP
    # 傳統機組與再生能源的出力可供應外部負載，也可供應充電。
    for i in I_g + I_r:
        for t in T:
            total = lpSum(K[j][i][t] for j in J_all) # 這個設備在時間點 t 供應給所有任務 (J_all) 的電量加總
            BIG_LP += (total <= P[i][t], f"Power_Distribution_Limit_{i}_t{t}")

    # 儲能設備的放電只供應外部負載，不拿去幫別的儲能設備充電。
    for i in I_b:
        for t in T:
            total = lpSum(K[j][i][t] for j in J_periodic + J_aperiodic) # 只能分配給一般用電任務 J
            BIG_LP += (total <= P[i][t], f"Battery_Discharge_Limit_{i}_t{t}")

def Constraint_21():
    global BIG_LP
    # Jchg 的電不能由儲能設備提供
    for j in J_charging:
        for i in I_b:
            for t in T:
                BIG_LP += (K[j][i][t] == 0, f"No_Battery_To_Battery_Charge_{j}_{i}_t{t}")

def Constraint_22_23():
    global BIG_LP
    #Constraint 22: 總售出電能量不可為負值。 (已在宣告 Sell 變數時的 lowBound=0 完成)
    #Constraint 23 每個時間點 𝑡 總供電能量必須等於用電需求電能量、儲能充電消耗電能量與售電量的總和。 (能量守恆)

    for t in T:
        # 計算 t 時間點的總電量
        total_generated = lpSum(P[i][t] for i in I_all)

        # 2. 計算總耗電量 (J_periodic + J_aperiodic + J_chg)
        total_consumed = lpSum(K[j][i][t] for j in J_all for i in I_all)

        # 發出來的電 = 任務吃掉 + 剩下拿去 Sell 的
        BIG_LP += (total_generated == total_consumed + SELL[t], f"System_Energy_Balance_t{t}")

# 新加的 強制保留電
def Constraint_24():
    global BIG_LP
    reserved_battery = 20

    for t in T:
        BIG_LP += (SELL[t] + Shortfall[t] >= reserved_battery, f"Soft_Reserved_t{t}")

Constraint_1()
Constraint_2()
Constraint_3()
Constraint_4()
Constraint_5()
Constraint_6_to_10()
Constraint_11_12()
Constraint_13()
Constraint_14_15()
Constraint_16_to_19()
Constraint_20()
Constraint_21()
Constraint_22_23()
Constraint_24()

# 發電機成本資料
gen_costs = {g["generator_id"]: {"fixed": g["cost_fixed"], "var": g["cost_variable"]} for g in processors["generator"]}

# 市場價格資料
price_dict = {p["hour"]: p["market_price"] for p in prices["price"]}

# 目標 1：最小化 aperiodic miss deadline數量 (懲罰係數 alpha = 10000)
f1 = lpSum(MISS[j] for j in J_aperiodic) * 10000

# 目標 2：最小化傳統機組發電成本 fixed + var
f2 = lpSum(gen_costs[i]["fixed"] * is_gen_on[i][t] + gen_costs[i]["var"] * P[i][t] for i in I_g for t in T)

# 目標 3：最大化售電收益 (因為是最小化問題，所以前面要加負號)
f3 = -lpSum(price_dict[t] * SELL[t] for t in T)

# 目標 4：盡可能保持SELL非0以保證隨時有多餘的電可以應付sporadic
penalty = lpSum(Shortfall[t] * 1000 for t in T)

BIG_LP += f1 + f2 + f3 + penalty, "Total_Objective"

# 開始求解
print("模型建置完成，開始求解...")
BIG_LP.solve()

# 印求解狀態 (Optimal 代表找到最佳解，Infeasible 代表無解)
print(f"求解狀態: {LpStatus[BIG_LP.status]}")


# 驗證, 印出結果
if BIG_LP.status == 1: # 1 代表 Optimal
    print(f"\n總目標函數值: {value(BIG_LP.objective)}")

    # 計算實際的發電成本與售電收益
    actual_f2 = sum(gen_costs[i]["fixed"] * is_gen_on[i][t].varValue + gen_costs[i]["var"] * P[i][t].varValue for i in I_g for t in T)
    actual_f3 = sum(price_dict[t] * SELL[t].varValue for t in T)

    print(f"傳統機組發電總成本: {actual_f2}")
    print(f"市場售電總收益: {actual_f3}")

    print("\n--- Aperiodic 任務逾期結算 ---")
    miss_count = 0
    for j in J_aperiodic:
        if MISS[j].varValue == 1:
            print(f"[警告] 任務 {j} 逾期了！")
            miss_count += 1
        else:
            print(f"[成功] 任務 {j} 準時完成！")

    print(f"\n總共 Miss 了 {miss_count} 個 Aperiodic 任務。")
else:
    print("模型無解，請檢查限制式或測資是否有衝突！")


print("\n--- 開始執行 Sporadic 任務 Acceptance Test ---")

# 取得每個小時的剩餘資源 (原本要賣掉的電)
slack = {t: SELL[t].varValue for t in T}

J_sporadic_list = list(J_sporadic)
max_time = -1
best_accepted = []
best_allocations = {}

def dfs(idx, current_slack, current_accepted, current_allocations, current_time):
    global max_time, best_accepted, best_allocations
    # 邊界條件：已經走完所有 sporadic 任務
    if idx == len(J_sporadic_list):
        if current_time > max_time:
            max_time = current_time
            best_accepted = list(current_accepted)
            best_allocations = dict(current_allocations)
        return

    job_id = J_sporadic_list[idx]
    job = jobs_all_dict[job_id]
    r, d, e, w, is_preempt = job["r"], job["d"], job["e"], job["w"], job["preempt"]
    valid_end = min(d, 73)

    # 選擇 1：不選此任務，直接往下走
    dfs(idx + 1, current_slack, current_accepted, current_allocations, current_time)

   # 選擇 2：選此任務（先檢查可用資源是否足夠排滿 e 小時，並挑選最便宜的時段）
    available_hours = []

    if is_preempt == 1:
        # preemptive: 找出所有容量足夠的時段
        valid_slots = [t for t in range(r, valid_end) if current_slack[t] >= w]

        if len(valid_slots) >= e:
            # 💡 核心優化：依照該時段的市場電價 (price_dict) 由低到高排序
            valid_slots.sort(key=lambda t: price_dict[t])
            # 挑選最便宜的 e 個小時
            available_hours = valid_slots[:e]

    else:
        # non-preemptive: 必須找到連續的 e 個小時，並找出總成本最低的區塊
        valid_blocks = []
        temp_block = []

        for t in range(r, valid_end):
            if current_slack[t] >= w:
                temp_block.append(t)
                # 如果收集到的連續時段長度達到 e，這就是一個合法區塊
                if len(temp_block) >= e:
                    valid_blocks.append(temp_block[-e:])
            else:
                temp_block = [] # 中斷了，重新計算

        if valid_blocks:
            # 💡 核心優化：計算每個合法區塊的總電價，並挑選總價最低的那個區塊
            available_hours = min(valid_blocks, key=lambda block: sum(price_dict[t] for t in block))

    # 如果可以順利排滿 e 個小時，則進入「選此任務」的分支
    if len(available_hours) == e:
        new_slack = current_slack.copy()
        for t in available_hours:
            new_slack[t] -= w

        new_allocations = current_allocations.copy()
        new_allocations[job_id] = available_hours
        new_accepted = current_accepted + [job_id]

        dfs(idx + 1, new_slack, new_accepted, new_allocations, current_time + e)

# 啟動枚舉
dfs(0, slack.copy(), [], {}, 0)

# 將最佳解套用至全域結果
accepted_sporadic = best_accepted
sporadic_allocations = best_allocations
rejected_sporadic = [j for j in J_sporadic_list if j not in accepted_sporadic]

# 更新全域的 slack 資源，扣除被最佳組合消耗的電量
for j in accepted_sporadic:
    for t in sporadic_allocations[j]:
        slack[t] -= jobs_all_dict[j]["w"]

# 計算財報
lost_revenue = 0
for j in accepted_sporadic:
    for t in sporadic_allocations[j]:
        lost_revenue += jobs_all_dict[j]["w"] * price_dict[t]

final_revenue = actual_f3 - lost_revenue
final_objective = value(BIG_LP.objective) + lost_revenue

# 建立輸出日誌結構
acceptance_log = {
    "summary": {
        "accepted_count": len(accepted_sporadic),
        "rejected_count": len(rejected_sporadic),
        "max_accepted_time_hours": max_time
    },
    "tasks_detail": {},
    "financials": {
        "original_expected_revenue": actual_f3,
        "lost_revenue_due_to_sporadic": lost_revenue,
        "final_actual_revenue": final_revenue,
        "final_total_objective": final_objective
    }
}

for j in J_sporadic_list:
    if j in accepted_sporadic:
        acceptance_log["tasks_detail"][j] = {
            "status": "Accept",
            "allocated_hours": sporadic_allocations[j]
        }
    else:
        acceptance_log["tasks_detail"][j] = {
            "status": "Reject",
            "allocated_hours": []
        }

# 寫入 JSON 檔案
with open("../output/acceptance_test_log.json", "w", encoding="utf-8") as f:
    json.dump(acceptance_log, f, ensure_ascii=False, indent=4)

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
print(f"最終總目標函數值(利潤): {final_objective}")



# ==========================================
# 1. 盤點每個時間點各設備的「真實剩餘電量」(原本要拿去 Sell 的電)
# ==========================================
leftover_power = {t: {} for t in T}
for t in T:
    for i in I_all:
        gen_val = P[i][t].varValue or 0.0
        # 計算這個設備已經分配給既有任務 (Periodic, Aperiodic, Charging) 的電量
        consumed_val = sum((K[j][i][t].varValue or 0.0) for j in J_all)

        remain = gen_val - consumed_val
        if remain > 1e-4:  # 加上浮點數容差，避免 0.000000001 被算進去
            leftover_power[t][i] = remain

# ==========================================
# 2. 輸出排程結果至 JSON 並分配 Sporadic 的來源
# ==========================================
schedule_result = []

for t in T:
    # 發電量 P (過濾掉沒有發電的設備)
    P_dict = {i: round(P[i][t].varValue, 2) for i in I_all if (P[i][t].varValue or 0.0) > 1e-4}

    # 電量分配 k (既有任務)
    k_dict = {}
    for j in J_all:
        allocations = {i: round(K[j][i][t].varValue, 2) for i in I_all if (K[j][i][t].varValue or 0.0) > 1e-4}
        if allocations:
            # 如果是週期性任務，還原名稱（去除後綴的 _{i}），其餘任務保持原樣
            base_j = j.rsplit('_', 1)[0] if j in J_periodic else j

            # 初始化該任務的字典（若不存在）
            if base_j not in k_dict:
                k_dict[base_j] = {}

            # 累加供電量，避免多個實例在同一個時間點 t 供電時發生覆寫
            for src, val in allocations.items():
                k_dict[base_j][src] = round(k_dict[base_j].get(src, 0.0) + val, 2)

    # Accept 的 Sporadic 任務 (分配具體發電機)
    for j in accepted_sporadic:
        if t in sporadic_allocations.get(j, []):
            w_needed = jobs_all_dict[j]["w"]
            k_dict[j] = {}

            # 從有剩餘電量的設備中「倒水」給這個 Sporadic job
            for src in list(leftover_power[t].keys()):
                if w_needed <= 1e-4:
                    break # 任務需要的電湊齊了就停

                avail = leftover_power[t][src]
                take = min(w_needed, avail) # 取「還需要的電」與「這台發電機剩的電」的最小值

                # 這裡就會確實標上發電機的名字 (例如 "thermal_1": 15)
                k_dict[j][src] = round(take, 2)

                # 扣除已經拿走的電量
                leftover_power[t][src] -= take
                w_needed -= take

                # 如果這台發電機的剩餘電量被這任務吸乾了，就從可用名單移除
                if leftover_power[t][src] <= 1e-4:
                    del leftover_power[t][src]

    # 更新賣出的錢 (剩下的 leftover 全部加總就是實際能賣的電)
    sell_val = round(sum(leftover_power[t].values()), 2)
    soc_dict = {i: round(SOC[i][t].varValue or 0.0, 2) for i in I_b}

    time_slot = {
        "t": t,
        "P": P_dict,
        "k": k_dict,
        "sell": sell_val,
        "soc": soc_dict,
        "missed_aperiodic": [j for j in J_aperiodic if MISS[j].varValue == 1],
        "rejected_sporadic": rejected_sporadic
    }
    schedule_result.append(time_slot)

output_data = {"schedule_result": schedule_result}
with open("../output/schedule_result.json", "w", encoding="utf-8") as f:
    json.dump(output_data, f, ensure_ascii=False, indent=4)

print("\n[成功]排程結果已儲存至 ../output/schedule_result.json")