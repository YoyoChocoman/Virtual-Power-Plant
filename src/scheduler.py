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

# is_chg[i][t]: 電池 i 在時間 t 是否正在充電 (Binary 1充電, 0放電或閒置)
is_chg = LpVariable.dicts("is_chg", (I_b, T), cat="Binary")

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


# ON[i][t]: 設備 i 在時間 t 是否啟動 (Binary 0或1)
ON = LpVariable.dicts("ON", (I_g, T), cat="Binary")

def Constraint_6_to_9():
    # 我把ON移去全域了 by吳哲愷
    gen_data = {g["generator_id"]: g for g in processors["generator"]}
    for i in I_g:
        out_min = gen_data[i]["output_min"]
        out_max = gen_data[i]["output_max"]
        ru = gen_data[i]["ramp_up_rate"]
        rd = gen_data[i]["ramp_down_rate"]
        for t in T:
            prob += (P[i][t] >= out_min * ON[i][t], f"Power_Minium_Limit_{i}_t{t}")
            prob += (P[i][t] <= out_max * ON[i][t], f"Power_Maxium_Limit_{i}_t{t}")
            if t == 1: prob += (P[i][t] <= ru, f"Power_Ramp_Up_Limit_{i}_t{t}")
            else:
                prob += (P[i][t] - P[i][t-1] <= ru, f"Power_Ramp_Up_Limit_{i}_t{t}")
                prob += (P[i][t-1] - P[i][t] <= rd, f"Power_Ramp_Down_Limit_{i}_t{t}")

            ut = gen_data[i]["min_up_time"]
            dt = gen_data[i]["min_down_time"]
            up_len = min(ut, 73 - t)
            total_status = lpSum(ON[i][tmp] for tmp in range(t, t + up_len))
            prob += (total_status >= up_len * (ON[i][t] - ON[i][t-1]), f"Min_Up_Time_{i}_t{t}")

            down_len = min(dt, 73 - t)
            total_status = lpSum(ON[i][tmp] for tmp in range(t, t + down_len))
            prob += (total_status <= down_len * (ON[i][t] - ON[i][t-1]), f"Min_down_Time_{i}_t{t}")

def Constraint_6_to_10():
    # 我把ON移去全域了 by吳哲愷
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
    #Jchg的電不能由儲能設備提供
    for j in J_chg:
        for i in I_b:
            for t in T:
                prob += (k[j][i][t] == 0, f"No_Battery_To_Battery_Charge_{j}_{i}_t{t}")

def Constraint_22_23():
    #Constraint 22: 總售出電能量不可為負值。 (已在宣告 Sell 變數時的 lowBound=0 完成)
    #Constraint 23 每個時間點 𝑡 總供電能量必須等於用電需求電能量、儲能充電消耗電能量與售電量的總和。 (能量守恆)

    for t in T:
        # 計算t時間點的總電量
        total_generated = lpSum(P[i][t] for i in I_all)

        # 2. 計算總耗電量 (J + J_chg)
        total_consumed = lpSum(k[j][i][t] for j in J_all for i in I_all)

        # 發出來的電 = 任務吃掉 + 剩下拿去 Sell 的
        prob += (total_generated == total_consumed + Sell[t], f"System_Energy_Balance_t{t}")