import json
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

prob = LpProblem("VPP_Scheduling", LpMinimize)

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

# Miss[j]: Aperiodic task j 是否逾期 (Binary 1=逾期, 0=準時)
Miss = LpVariable.dicts("Miss", J_aperiodic, cat="Binary")

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

# is_chg[i][t]: 電池 i 在時間 t 是否正在充電 (Binary 1充電, 0放電或閒置)
is_chg = LpVariable.dicts("is_chg", (I_b, T), cat="Binary")

# ON[i][t]: 設備 i 在時間 t 是否啟動 (Binary 0或1)
ON = LpVariable.dicts("ON", (I_g, T), cat="Binary")

def Constraint_1():
    global prob
    for j in J:
        for t in T:
            total_k = lpSum(k[j][i][t] for i in I_all)
            prob += (total_k == jobs_all_dict[j]["w"] * x[j][t], f"Demand_Match_{j}_t{t}")

def Constraint_2():
    global prob
    for j in J:
        for t in T:
            if t < jobs_all_dict[j]["r"]:
                prob += (x[j][t] == 0, f"Release_Time_{j}_t{t}")
                for i in I_all:
                    prob += (k[j][i][t] == 0, f"No_Power_Before_Release_{j}_{i}_t{t}")

def Constraint_3():
    #用電需求 𝑗 ∈ 𝐽𝑝 ∪ 𝐽𝑠 必須在 deadline 前做滿所需時間段 𝑒 𝑗。
    global prob
    for j in J_hard:
        total_x = lpSum(x[j][t] for t in range(jobs_all_dict[j]["r"], jobs_all_dict[j]["d"]))
        prob += (total_x == jobs_all_dict[j]["e"], f"Execution_Match_{j}")
        for t in T:
            if t >= jobs_all_dict[j]["d"]:
                prob += (x[j][t] == 0, f"Deadline_{j}_t{t}")
                for i in I_all:
                    prob += (k[j][i][t] == 0, f"No_Power_After_Deadline_{j}_{i}_t{t}")

def Constraint_4():
    #Aperiodic task miss 定義
    global prob
    for j in J_aperiodic:
        r = jobs_all_dict[j]["r"]
        e = jobs_all_dict[j]["e"]
        d = jobs_all_dict[j]["d"]

        # 確保上限不超過排程總長度 73
        valid_end = min(d, 73)

        # 計算在 deadline 之前實際做了多少小時
        sum_x_before_dl = lpSum(x[j][t] for t in range(r, valid_end))

        #若 𝑀𝑖𝑠𝑠𝑗 = 0，表示 𝑗_a 需在 deadline 前完成
        prob += (sum_x_before_dl >= e * (1 - Miss[j]), f"Aperiodic_Miss_LB_{j}")

        # 若 𝑀𝑖𝑠𝑠𝑗 = 1，表示 j_a 未於 deadline 前完成，記錄為 deadline miss。
        prob += (sum_x_before_dl <= (e - 1) + e * (1 - Miss[j]), f"Aperiodic_Miss_UB_{j}")

        #用電需求 𝑗 ∈ 𝐽𝑎 必須在最後一個離散時間點 𝑯 前做滿所需時間段 𝑒𝑗。 (就算 miss 也還是要做完的)
        total_x = lpSum(x[j][t] for t in range(r, 73))
        prob += (total_x == e, f"Aperiodic_Total_Exec_{j}")


def Constraint_5():
    global prob
    # s[j][t]: Job j 在時間 t 是否啟動 (Binary 0或1)
    s = LpVariable.dicts("start", (J, T), cat="Binary")
    for j in J:
        for t in T:
            if t == 1:
                prev_x = 0
            else:
                prev_x = x[j][t-1]

            if t == jobs_all_dict[j]["r"]:
                prob += (s[j][t] == x[j][t], f"Initial_Start_{j}_t{t}")
            elif t > jobs_all_dict[j]["r"]:
                prob += (s[j][t] >= x[j][t] - prev_x, f"Capture_Start_{j}_t{t}")

        if jobs_all_dict[j]["preempt"] == 0:
            end_t = 73 if j in J_aperiodic else jobs_all_dict[j]["d"]
            total_u = lpSum(s[j][t] for t in range(jobs_all_dict[j]["r"], end_t))
            prob += (total_u == 1, f"Non-preemptive_{j}")

def Constraint_6_to_10():
    # 我把ON移去全域了 by吳哲愷
    global prob
    gen_data = {g["generator_id"]: g for g in processors["generator"]}

    for i in I_g:
        out_min = gen_data[i]["output_min"]
        out_max = gen_data[i]["output_max"]
        ru = gen_data[i]["ramp_up_rate"]
        rd = gen_data[i]["ramp_down_rate"]
        ut = gen_data[i]["min_up_time"]
        dt = gen_data[i]["min_down_time"]

        # 取得 t=0 的狀態
        init_on = 1 if gen_data[i]["initial_on_time"] > 0 else 0
        init_p = gen_data[i]["initial_energy"]

        for t in T:
            #Constraint 6: 傳統機組出力上下限
            prob += (P[i][t] >= out_min * ON[i][t], f"Power_Min_Limit_{i}_t{t}")
            prob += (P[i][t] <= out_max * ON[i][t], f"Power_Max_Limit_{i}_t{t}")

            # 你原本的寫法可能會讀到t=0但我們T是randint(1, 73),會吃keyerror 所以分成兩個情況
            # 如果現在是第1小時，上一小時就是 t=0，拿 init 來用。如果現在t是大於 1，就拿 t−1 來用
            if t == 1:
                prev_ON = init_on
                prev_P = init_p
            else:
                prev_ON = ON[i][t-1]
                prev_P = P[i][t-1]

            #Constraint 7: 傳統機組 ramp-up / ramp-down 不可標過上限 （註：感覺要分情況但由酷酷的數學式可以直接這樣簡化）
            prob += (P[i][t] - prev_P <= ru, f"Ramp_Up_Limit_{i}_t{t}")
            prob += (prev_P - P[i][t] <= rd, f"Ramp_Down_Limit_{i}_t{t}")

            #Constraint 8: 傳統機組最小出力不可大於單一時間段可達到之 ramp-up 能力。
            #這感覺是 task_generator 那邊的條件吧，暫時不管


            #---Constraint 9, 10 有點難解釋，用註解打不清楚，有問題再問我---

            #Constraint 9: 若傳統機組 𝑖 在時間點 𝑡 由關機狀態轉為開機狀態，則該機組自時間點 𝑡 起，必須至少連續維持開機 𝑈𝑇𝑖 個時間段。
            #開機動作：(ON[i][t] - prev_ON) 如果等於 1，代表這一刻開機了
            #那未來符合最小開機時間的每個小時（未來第 tmp 小時），ON 都必須等於 1
            up_len = min(ut, 73 - t)
            for tmp in range(t, t + up_len):
                prob += (ON[i][tmp] >= ON[i][t] - prev_ON, f"Min_Up_Time_Enforce_{i}_t{t}_to_t{tmp}")

            #Constraint 10: 最小關機時間 (Min Down Time)
            #關機動作：(prev_ON - ON[i][t]) 如果等於 1，代表這一刻關機了
            #那未來符合最小關機時間的每個小時（未來第 tmp 小時），ON 都必須等於 0
            down_len = min(dt, 73 - t)
            for tmp in range(t, t + down_len):
                prob += (1 - ON[i][tmp] >= prev_ON - ON[i][t], f"Min_Down_Time_Enforce_{i}_t{t}_to_t{tmp}")

def Constraint_11_12():
    global prob
    #若傳統機組 𝑖 在排程起點前已處於開機狀態，且其已連續開機時間尚未滿足最小開機時間 𝑈𝑇𝑖，則需補足剩餘的最小開機時間。
    #若傳統機組 𝑖在排程起點前已處於關機狀態，且其已連續關機時間尚未滿足0最小關機時間 𝐷𝑇𝑖，則需補足剩餘的最小關機時間。
    for g in processors["generator"]:
        i = g["generator_id"]
        UT = g["min_up_time"]       #最小開機時間
        DT = g["min_down_time"]     #最小關機時間
        TN = g["initial_on_time"]   #排程前已連續開機多久
        TF = g["initial_off_time"]  #排程前已連續關機多久

    # Constraint 11: 已經開機，但還沒滿足最小開機時間
    if TN > 0 and TN < UT:
        remain_up = UT - TN
        for t in range(1, remain_up + 1):
            prob += (ON[i][t] == 1, f"Force_Initial_Up_{i}_t{t}")

    # Constraint 12: 已經關機，但還沒滿足最小關機時間
    if TF > 0 and TF < DT:
        remain_down = DT - TF
        for t in range(1, remain_down + 1):
            prob += (ON[i][t] == 0, f"Force_Initial_Down_{i}_t{t}")

def Constraint_13():
    global prob
    #再生能源在每一時間點的出力，不可標過該時段預測可用電能量。

    #建 capacity 字典 key:發電機id, value:該發電機可提供的電量
    cap_dict = {}
    for r in processors["renewable_capacity"]:
        key = r["renewable_id"]
        value = r["capacity"]
        cap_dict[key] = value

    #建 forecast 字典 key:發電機id, value:存該發電機每個時間能提供的出力百分比
    forecast_dict = {}
    for r_data in processors["renewable_forecast"]:
        for r_id, f_list in r_data.items():
            forecast_dict[r_id] = f_list

    for i in I_r:
        for t in T:
            #hour從1開始，陣列index從0開始
            max_output = cap_dict[i] * forecast_dict[i][t-1]["pv_forecast"]

            #發電量 P 必須小於等於最大預測值
            prob += (P[i][t] <= max_output, f"Renewable_Max_Limit_{i}_t{t}")

def Constraint_14_15():
    global prob
    #儲能設備 𝑖 在每一時間點的放電量 𝑃𝑖,𝑡，不得標過其單位時間最大放電能力。
    #儲能設備 𝑖 在時間點 𝑡 接收之總充電量不得標過其單位時間最大充電能力。

    #建最大充放電字典 key:儲能設備id, value:最大充放電值
    dis_max = {b["storage_id"]: b["discharge_max"] for b in processors["storage"]}
    chg_max = {b["storage_id"]: b["charge_max"] for b in processors["storage"]}

    # mapping 這顆電池對應到哪個任務
    chg_job_map = {c["target_storage"]: c["job_id"] for c in processors["charging_jobs"]}
    for i in I_b:
        for t in T:
            # Constraint 14: 電池每小時放電量 P 不能超過 discharge_max
            prob += (P[i][t] <= dis_max[i], f"Battery_Max_Discharge_{i}_t{t}")

            # Constraint 15: 電池每小時充電量不能超過 charge_max
            j = chg_job_map[i] # 充這顆電池的充電任務ID

            # source: 傳統機組+再生能源 (I_g + I_r)
            total_charge_received = lpSum(k[j][src][t] for src in I_g + I_r)
            prob += (total_charge_received <= chg_max[i], f"Battery_Max_Charge_{i}_t{t}")

def Constraint_16_to_19():
    global prob
    storage_data = {b["storage_id"]: b for b in processors["storage"]}
    chg_job_map = {c["target_storage"]: c["job_id"] for c in processors["charging_jobs"]} #和14_15一樣

    for i in I_b:
        soc_init = storage_data[i]["soc_init"]       # 初始電量 (t=0)
        soc_min = storage_data[i]["soc_min"]         # 電池容量下限
        soc_max = storage_data[i]["soc_max"]         # 電池容量上限
        dis_max = storage_data[i]["discharge_max"]   # 最大放電功率
        chg_max = storage_data[i]["charge_max"]      # 最大充電功率
        j = chg_job_map[i]                           # 充這顆電池的充電任務ID

        for t in T:
            # 充進來的電量
            total_charge = lpSum(k[j][src][t] for src in I_g + I_r)


            if t == 1:
                prev_SOC = soc_init
            else:
                prev_SOC = SOC[i][t-1]

            # Constraint 16: 儲能設備 𝑖 在時間點 𝑡 的儲存電能量，等於前一時間點的儲存電能量，加上該時段充電量，並扣除該時段放電量。
            prob += (SOC[i][t] == prev_SOC + total_charge - P[i][t], f"SOC_Tracking_{i}_t{t}")

            # Constraint 17: 儲能設備 𝑖 儲存量上下限。
            prob += (SOC[i][t] >= soc_min, f"SOC_Minimum_Limit_{i}_t{t}")
            prob += (SOC[i][t] <= soc_max, f"SOC_Maximum_Limit_{i}_t{t}")

            # Constraint 18: 儲能設備 𝑖 不能放出超過最低存量的電能。
            prob += (P[i][t] <= prev_SOC - soc_min, f"Discharge_Safety_Limit_{i}_t{t}")

            # Constraint 19: 同一個儲能設備 𝑖 同一時間點 𝑡 不可同時充電又放電。
            # 1. 如果 is_chg 是 1 (充電)
            # total_charge <= chg_max * 1 (可以充電)
            # P[i][t] <= dis_max * 0      (強制放電為0)
            # 2. 如果 is_chg 是 0 (決定放電)
            # total_charge <= chg_max * 0 (強制充電為0)
            # P[i][t] <= dis_max * 1      (可以放電)

            prob += (total_charge <= chg_max * is_chg[i][t], f"Exclusivity_Charge_{i}_t{t}")
            prob += (P[i][t] <= dis_max * (1 - is_chg[i][t]), f"Exclusivity_Discharge_{i}_t{t}")

def Constraint_20():
    global prob
    #傳統機組與再生能源的出力可供應外部負載，也可供應充電。
    for i in I_g + I_r:
        for t in T:
            total = lpSum(k[j][i][t] for j in J_all) # 這個設備在時間點 t 供應給所有任務(J_all)的電量加總
            prob += (total <= P[i][t], f"Power_Distribution_Limit_{i}_t{t}")

    #儲能設備的放電只供應外部負載，不拿去幫別的儲能設備充電。
    for i in I_b:
        for t in T:
            total= lpSum(k[j][i][t] for j in J) #只能分配給一般用電任務 J
            prob += (total <= P[i][t], f"Battery_Discharge_Limit_{i}_t{t}")

def Constraint_21():
    global prob
    #Jchg的電不能由儲能設備提供
    for j in J_charging:
        for i in I_b:
            for t in T:
                prob += (k[j][i][t] == 0, f"No_Battery_To_Battery_Charge_{j}_{i}_t{t}")

def Constraint_22_23():
    global prob
    #Constraint 22: 總售出電能量不可為負值。 (已在宣告 Sell 變數時的 lowBound=0 完成)
    #Constraint 23 每個時間點 𝑡 總供電能量必須等於用電需求電能量、儲能充電消耗電能量與售電量的總和。 (能量守恆)

    for t in T:
        # 計算t時間點的總電量
        total_generated = lpSum(P[i][t] for i in I_all)

        # 2. 計算總耗電量 (J + J_chg)
        total_consumed = lpSum(k[j][i][t] for j in J_all for i in I_all)

        # 發出來的電 = 任務吃掉 + 剩下拿去 Sell 的
        prob += (total_generated == total_consumed + Sell[t], f"System_Energy_Balance_t{t}")




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


#定義目標函數

#發電機成本資料
gen_costs = {g["generator_id"]: {"fixed": g["cost_fixed"], "var": g["cost_variable"]} for g in processors["generator"]}

#市場價格資料
price_dict = {p["hour"]: p["market_price"] for p in prices["price"]}

#目標1：最小化 aperiodic miss deadline數量 (懲罰係數 alpha = 10000)
f1 = lpSum(Miss[j] for j in J_aperiodic) * 10000

#目標2：最小化傳統機組發電成本 fixed + var
f2 = lpSum(gen_costs[i]["fixed"] * ON[i][t] + gen_costs[i]["var"] * P[i][t] for i in I_g for t in T)

#目標3：最大化售電收益 (因為是最小化問題，所以前面要加負號)
f3 = -lpSum(price_dict[t] * Sell[t] for t in T)

prob += f1 + f2 + f3, "Total_Objective"

#開始求解
print("模型建置完成，開始求解...")
prob.solve()

#印求解狀態 (Optimal 代表找到最佳解，Infeasible 代表無解)
print(f"求解狀態: {LpStatus[prob.status]}")


#驗證, 印出結果
if prob.status == 1: #1 代表 Optimal
    print(f"\n總目標函數值: {value(prob.objective)}")

    # 計算實際的發電成本與售電收益
    actual_f2 = sum(gen_costs[i]["fixed"] * ON[i][t].varValue + gen_costs[i]["var"] * P[i][t].varValue for i in I_g for t in T)
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

#取得每個小時的剩餘資源 (原本要賣掉的電)
slack = {t: Sell[t].varValue for t in T}

accepted_sporadic = []
rejected_sporadic = []
sporadic_allocations = {} #紀錄被接受的 Sporadic 任務分配在哪些時段

for j in J_sporadic:
    r = jobs_all_dict[j]["r"]
    d = jobs_all_dict[j]["d"]
    e = jobs_all_dict[j]["e"]
    w = jobs_all_dict[j]["w"]
    is_preempt = jobs_all_dict[j]["preempt"]

    valid_end = min(d, 73)
    available_hours = []

    #找可用時段 (該時段的 slack 必須 >= 任務所需電量 w)
    if is_preempt == 1:
        #在區間內湊齊 e 個小時即可
        for t in range(r, valid_end):
            if slack[t] >= w:
                available_hours.append(t)
            if len(available_hours) == e:
                break
    else:
        #必須找到連續的 e 個小時
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

    #判斷 Accept 或 Reject
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
final_objective = value(prob.objective) + lost_revenue

print("\n--- 最終財報結算 (包含 Sporadic 影響) ---")
print(f"原本預期售電收益: {actual_f3}")
print(f"因接受 Sporadic 犧牲的售電收益: {lost_revenue}")
print(f"最終實際售電收益: {final_revenue}")
print(f"最終總目標函數值: {final_objective}")

#輸出排程結果至 JSON
schedule_result = []

schedule_result = []

for t in T:
    #發電量 P
    P_dict = {i: round(P[i][t].varValue, 2) for i in I_all}

    #電量分配 k (只紀錄大於 0 的分配)
    k_dict = {}
    for j in J_all:
        allocations = {i: round(k[j][i][t].varValue, 2) for i in I_all if k[j][i][t].varValue > 0}
        if allocations:
            k_dict[j] = allocations

    #Accept 的 Sporadic 任務
    for j in accepted_sporadic:
        if t in sporadic_allocations[j]:
            k_dict[j] = {"slack_reserve": jobs_all_dict[j]["w"]}

    #賣的錢, 電量狀態
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