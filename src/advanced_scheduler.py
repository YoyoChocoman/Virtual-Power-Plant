import json
import random
from pulp import *

# 1. 讀取外部設定檔資料
with open("../output/task_set.json", "r", encoding="utf-8") as f:
    data = json.load(f)

with open("../input/processor_settings.json", "r", encoding="utf-8") as f:
    processors = json.load(f)

with open("../input/price_72hr.json", "r", encoding="utf-8") as f:
    prices = json.load(f)

SHIFT = 0
jobs_periodic_dict = {}
# 將週期性任務(Periodic)在 72 小時內展開成多個獨立的執行實例
for task, info in data["periodic"].items():
    for i in range(72 // info["p"]):
        # 若任務加上其執行時長的結束時間超過 72 小時則停止展開
        if info["r"] + info["p"] * i + info["d"] > 72: break

        jobs_periodic_dict[f"{task}_{i + 1}"] = {
            "r": info["r"] + info["p"] * i + SHIFT,               # 絕對抵達時間
            "d": info["r"] + info["p"] * i + info["d"] + SHIFT,   # 絕對截止時間
            "e": info["e"],                                       # 執行所需時間
            "w": info["w"],                                       # 消耗功率
            "preempt": info["preempt"]                            # 是否可搶佔
        }

# 處理非週期性任務(Aperiodic)的絕對時間轉換
jobs_aperiodic_dict = data.get("aperiodic", {})
for task, info in data.get("aperiodic", {}).items():
    jobs_aperiodic_dict[task] = {
        "r": info["r"] + SHIFT,
        "d": info["r"] + info["d"] + SHIFT, # 轉為 absolute deadline
        "e": info["e"],
        "w": info["w"],
        "preempt": info["preempt"]
    }

# 處理突發性任務(Sporadic)的絕對時間轉換
jobs_sporadic_dict = {}
for task, info in data.get("sporadic", {}).items():
    jobs_sporadic_dict[task] = {
        "r": info["r"] + SHIFT,
        "d": info["r"] + info["d"] + SHIFT, # 轉為 absolute deadline
        "e": info["e"],
        "w": info["w"],
        "preempt": info["preempt"]
    }

jobs_all_dict = {**jobs_periodic_dict, **jobs_aperiodic_dict, **jobs_sporadic_dict}

# 定義時間範圍與設備 ID 集合
T = range(1, 73)
I_g = [g["generator_id"] for g in processors["generator"]]                # 發電機 ID
I_r = [r["renewable_id"] for r in processors["renewable_capacity"]]       # 再生能源 ID
I_b = [b["storage_id"] for b in processors["storage"]]                    # 儲能(電池) ID
I_all = I_g + I_r + I_b                                                   # 所有電源 ID

# 任務分類清單
J_periodic = list(jobs_periodic_dict.keys())
J_aperiodic = list(jobs_aperiodic_dict.keys())
J_sporadic = list(jobs_sporadic_dict.keys())
J_charging = [c["job_id"] for c in processors["charging_jobs"]]           # 電池充電任務
J_all = J_periodic + J_aperiodic + J_sporadic + J_charging


# -------------------- 為了後續動態規劃先把浮動值拉出來 ---------------------------------
# 建立再生能源容量與預測字典
cap_dict = {r["renewable_id"]: r["capacity"] for r in processors["renewable_capacity"]}
forecast_dict = {r_id: f_list for r_data in processors["renewable_forecast"] for r_id, f_list in r_data.items()}

# 模擬太陽能(PV)的「真實」發電狀況 (加入氣候不確定性)
realized_pv = {}
for i in I_r:
    realized_pv[i] = {}
    cloud_level = 0  # 0: 無雲, 1: 輕度烏雲(80%), 2: 重度烏雲(50%~70%)
    cloud_duration = 0

    for t in T:
        base_forecast = forecast_dict[i][t-1]["pv_forecast"]

        if cloud_duration > 0:
            if cloud_level == 1:
                # 烏雲剛來或快走的「邊緣」，發電量掉到 80% 左右
                actual_pv = base_forecast * random.uniform(0.75, 0.85)
                cloud_level = 2 # 下一小時進入重度烏雲
            else:
                # 烏雲中心，發電量掉到 50%~70%
                actual_pv = base_forecast * random.uniform(0.5, 0.7)

            cloud_duration -= 1
            # 如果快結束了，變回輕度烏雲
            if cloud_duration == 1:
                cloud_level = 1
        else:
            # 正常天氣 (±10% 波動)
            actual_pv = base_forecast * random.uniform(0.90, 1.10)
            # 10% 機率遠方飄來烏雲 (持續 2~4 小時)
            if random.random() < 0.1:
                cloud_duration = random.randint(2, 4)
                cloud_level = 1 # 第一個小時先輕微下降
                actual_pv = base_forecast * random.uniform(0.75, 0.85)

        realized_pv[i][t] = max(0.0, min(1.0, actual_pv))

# 模擬市場電價的波動性
price_dict = {p["hour"]: p["market_price"] for p in prices["price"]}
rt_price_dict = {}
for t in T:
    if random.random() < 0.05:
        # 5% 機率市場電力過剩，價格變負值 (倒貼)
        rt_price_dict[t] = price_dict[t] * random.uniform(-2.0, -0.5)
    elif random.random() < 0.10:
        # 10% 機率市場電力稀缺，價格飆升 3 到 5 倍
        rt_price_dict[t] = price_dict[t] * random.uniform(3.0, 5.0)
    else:
        # 正常情況下微幅波動 ±10%
        rt_price_dict[t] = price_dict[t] * random.uniform(0.9, 1.1)
# ------------------------------------------------------------------------------------


# 動態排程核心函式 (給定當前時間、活躍的突發任務、以及歷史狀態)
def reschedule(current_time, active_sporadics, history):
    BIG_LP = LpProblem("VPP_Scheduling", LpMinimize)

    # 確定當下模型需要處理的任務範圍
    J_current = J_periodic + J_aperiodic + active_sporadics
    J_all_current = J_current + J_charging
    J_hard_current = J_periodic + active_sporadics # 硬性截止任務(不能 Miss)

    # 宣告決策變數
    MISS = LpVariable.dicts("Miss", J_aperiodic, cat="Binary")                  # 是否錯過非週期性任務
    X = LpVariable.dicts("x", (J_current, T), cat="Binary")                     # 任務 j 在時間 t 是否執行
    P = LpVariable.dicts("P", (I_all, T), lowBound=0, cat="Continuous")         # 設備 i 在時間 t 的輸出功率
    K = LpVariable.dicts("k", (J_all, I_all, T), lowBound=0, cat="Continuous")  # 設備 i 分配給任務 j 的功率
    SELL = LpVariable.dicts("Sell", T, lowBound=0, cat="Continuous")            # 賣給電網的電量
    SOC = LpVariable.dicts("SOC", (I_b, T), lowBound=0, cat="Continuous")       # 儲能電池在時間 t 的電量狀態
    is_chg = LpVariable.dicts("is_chg", (I_b, T), cat="Binary")                 # 電池是否處於充電狀態
    is_gen_on = LpVariable.dicts("ON", (I_g, T), cat="Binary")                  # 發電機是否處於開啟狀態
    s = LpVariable.dicts("start", (J_current, T), cat="Binary")                 # 任務啟動指標 (未在限制式中明確使用，供擴充)

    # 維持歷史軌跡不變：針對過去的時間(小於 current_time)，強制變數等於歷史紀錄
    for past in range(1, current_time):
        record = history[past]
        BIG_LP += (SELL[past] == record["Sell"])
        for i in I_g:
            BIG_LP += (is_gen_on[i][past] == record["ON"][i])
            BIG_LP += (P[i][past] == record["P"][i])
        for i in I_b:
            BIG_LP += (SOC[i][past] == record["SOC"][i])
            BIG_LP += (is_chg[i][past] == record["is_chg"][i])
        for j in J_current:
            past_x = record["x"].get(j, 0)
            BIG_LP += (X[j][past] == past_x)

    gen_data = {g["generator_id"]: g for g in processors["generator"]}
    storage_data = {battery["storage_id"]: battery for battery in processors["storage"]}
    chg_job_map = {chg_job["target_storage"]: chg_job["job_id"] for chg_job in processors["charging_jobs"]}

    eta_chg, eta_dis, leak_rate = 0.93, 0.93, 0.005 # 充放電效率與電池自放電率

    for t in range(current_time, 73):
        # 電力供需平衡限制式 (總發電 = 總消耗 + 賣電)
        total_gen = lpSum(P[i][t] for i in I_all)
        total_con = lpSum(K[j][i][t] for j in J_all_current for i in I_all)
        BIG_LP += (total_gen == total_con + SELL[t])

        # 任務功率分配限制式：若任務執行(X=1)，分配的總電力必須等於任務耗電要求(w)
        for j in J_current:
            BIG_LP += (lpSum(K[j][i][t] for i in I_all) == jobs_all_dict[j]["w"] * X[j][t])
            if t < jobs_all_dict[j]["r"]:
                BIG_LP += (X[j][t] == 0) # 抵達時間前不可執行

        # 再生能源上限限制式
        for i in I_r:
            # 過去時間使用「實際發電量」，未來時間只能基於「預測值」規劃
            if t <= current_time + 3:
                actual_pv = realized_pv[i][t] if t <= 72 else 0
            else:
                actual_pv = forecast_dict[i][t-1]["pv_forecast"]
            BIG_LP += (P[i][t] <= cap_dict[i] * actual_pv)

        # 燃氣/柴油發電機物理限制式
        for i in I_g:
            # 輸出上下限
            BIG_LP += (P[i][t] >= gen_data[i]["output_min"] * is_gen_on[i][t])
            BIG_LP += (P[i][t] <= gen_data[i]["output_max"] * is_gen_on[i][t])

            prev_on = is_gen_on[i][t-1] if t > 1 else (1 if gen_data[i]["initial_on_time"] > 0 else 0)
            prev_p = P[i][t-1] if t > 1 else gen_data[i]["initial_energy"]

            # 升降載速率限制 (Ramp-up / Ramp-down rate)
            BIG_LP += (P[i][t] - prev_p <= gen_data[i]["ramp_up_rate"])
            BIG_LP += (prev_p - P[i][t] <= gen_data[i]["ramp_down_rate"])

            # 最短開啟時間與最短關閉時間 (Min up/down time) 避免頻繁啟停造成損壞
            for tmp in range(t, min(73, t + gen_data[i]["min_up_time"])):
                BIG_LP += (is_gen_on[i][tmp] >= is_gen_on[i][t] - prev_on)
            for tmp in range(t, min(73, t + gen_data[i]["min_down_time"])):
                BIG_LP += (1 - is_gen_on[i][tmp] >= prev_on - is_gen_on[i][t])

        # 儲能系統(電池)物理與狀態限制式
        for i in I_b:
            prev_SOC = SOC[i][t-1] if t > 1 else storage_data[i]["soc_init"]
            j_c = chg_job_map[i]
            # 獲取其他發電源對該電池充電任務的總供電
            tot_charge = lpSum(K[j_c][src][t] for src in I_g + I_r)

            # 電量轉移方程式 (考慮充電效率、放電效率與自放電)
            BIG_LP += (SOC[i][t] == prev_SOC * (1 - leak_rate) + tot_charge * eta_chg - P[i][t] / eta_dis)
            # 電池容量上下限
            BIG_LP += (SOC[i][t] >= storage_data[i]["soc_min"])
            BIG_LP += (SOC[i][t] <= storage_data[i]["soc_max"])
            # 充放電速率上限
            BIG_LP += (P[i][t] <= storage_data[i]["discharge_max"])
            BIG_LP += (tot_charge <= storage_data[i]["charge_max"])
            # 放電量不可超過當下可用電量
            BIG_LP += (P[i][t] <= prev_SOC - storage_data[i]["soc_min"])

            # 互斥限制式：確保同一時間段內只能「純充電」或「純放電」
            BIG_LP += (tot_charge <= storage_data[i]["charge_max"] * is_chg[i][t])
            BIG_LP += (P[i][t] <= storage_data[i]["discharge_max"] * (1 - is_chg[i][t]))

            # 防止電池互充產生無效迴圈
            BIG_LP += (lpSum(K[j_c][i_b][t] for i_b in I_b) == 0)

    # 任務期限限制：硬性任務(Periodic & 接受的 Sporadic)必須在其可運行區間內完成所需執行時間 (e)
    for j in J_hard_current:
        valid_end = min(jobs_all_dict[j]["d"], 73)
        BIG_LP += (lpSum(X[j][t] for t in range(jobs_all_dict[j]["r"], valid_end)) == jobs_all_dict[j]["e"])
        for t in T:
            if t >= jobs_all_dict[j]["d"]:
                BIG_LP += (X[j][t] == 0)

    # 軟性任務(Aperiodic)限制：可選擇是否放棄(MISS)
    for j in J_aperiodic:
        r, d, e = jobs_all_dict[j]["r"], jobs_all_dict[j]["d"], jobs_all_dict[j]["e"]
        sum_x_before = lpSum(X[j][t] for t in range(r, min(d, 73)))
        # 若未放棄(MISS=0)，則在截止時間前需做滿 e
        BIG_LP += (sum_x_before >= e * (1 - MISS[j]))
        BIG_LP += (sum_x_before <= (e - 1) + e * (1 - MISS[j]))
        # 確保整個 72 小時區間內最終一定會完成
        BIG_LP += (lpSum(X[j][t] for t in range(r, 73)) == e)

    # 不可中斷任務(Non-preemptive)限制
    for j in J_current:
        for t in T:
            prev_x = X[j][t-1] if t > 1 else 0
            if t ==- jobs_all_dict[j]["r"]:
                BIG_LP += (s[j][t] == X[j][t])
            elif t > jobs_all_dict[j]["r"]:
                BIG_LP += (s[j][t] >= X[j][t] - prev_x)

        if jobs_all_dict[j]["preempt"] == 0:
            valid_end = min(jobs_all_dict[j]["d"], 73) if j not in J_aperiodic else 73
            BIG_LP += (lpSum(s[j][t] for t in range(jobs_all_dict[j]["r"], valid_end)) == 1)

    # 目標函數(Objective Function) 設定：尋找成本最小化策略
    aging_cost = 5.0
    f1 = lpSum(MISS[j] for j in J_aperiodic) * 10000  # 懲罰成本：錯過軟性任務的高額罰金
    f2 = lpSum(gen_data[i]["cost_fixed"] * is_gen_on[i][t] + gen_data[i]["cost_variable"] * P[i][t] for i in I_g for t in T) # 發電成本：發電機啟動成本與燃料成本
    f3 = -lpSum(rt_price_dict[t] * SELL[t] for t in T) # 售電收益：賣給電網賺的錢 (用負值代表扣減成本)
    f4 = lpSum(aging_cost * (lpSum(K[chg_job_map[i]][src][t] for src in I_g + I_r) + P[i][t]) for i in I_b for t in T) # 電池老化成本：頻繁充放電的耗損折舊
    f5 = -lpSum(SOC[i][t] * 20 for i in I_b for t in T)

    BIG_LP += f1 + f2 + f3 + f4 + f5

    # 執行求解器 (靜音模式)
    BIG_LP.solve(PULP_CBC_CMD(msg=0))

    # 若有找到最佳解，將變數數值解析回 dict
    if BIG_LP.status == 1:
        ans = {
            "x": {j: {t: X[j][t].varValue for t in T} for j in J_current},
            "P": {i: {t: P[i][t].varValue for t in T} for i in I_all},
            "k": {j: {i: {t: K[j][i][t].varValue for t in T} for i in I_all} for j in J_all_current},
            "Sell": {t: SELL[t].varValue for t in T},
            "SOC": {i: {t: SOC[i][t].varValue for t in T} for i in I_b},
            "ON": {i: {t: is_gen_on[i][t].varValue for t in T} for i in I_g},
            "is_chg": {i: {t: is_chg[i][t].varValue for t in T} for i in I_b},
            "Miss": {j: MISS[j].varValue for j in J_aperiodic}
        }
        return 1, ans

    return 0, None


# ------------ 主程式迴圈 (模擬現實時間推進) ---------------------
history = {}
accepted_sporadic = []
rejected_sporadic = []

status, current_plan = reschedule(1, accepted_sporadic, history)
if status != 1:
    exit()

# 開始時間步進模擬 (Rolling Horizon)
for current in T:
    need_reschedule = False
    arrived_sporadics = [j for j in J_sporadic if jobs_all_dict[j]["r"] == current]

    # 檢查實際觀測到的太陽能發電是否與原預期發生嚴重落差
    for i in I_r:
        for look_ahead in range(0, 3):
            t_check = current + look_ahead
            if t_check <= 72:
                actual = realized_pv[i][t_check]
                forecast = forecast_dict[i][t_check-1]["pv_forecast"]
                if actual < forecast * 0.7:
                    need_reschedule = True
                    if look_ahead > 0:
                        print(f"[雷達預警] 設備 {i} 預計在第 {t_check} 小時遭遇烏雲，提早啟動應變！")
                    break

    # 觸發條件：有新任務抵達，或外部氣候落差過大
    # 先將新到的突發任務加入測試佇列
    # 測試成功：系統有能力負荷新任務與天氣變化
    # 測試失敗：系統無法負荷，將該批任務拒絕
    # 若不僅有新任務，還有惡劣天氣，需單獨處理惡劣天氣的重排程
    if arrived_sporadics or need_reschedule:
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
                    print(f"嚴重警告：第 {current} 小時天氣過度惡劣，Ramp-up 不及，導致系統物理上無解 (跳電)！")

    # 將當下的決策固化為歷史軌跡，未來時間點的排程不得竄改此資料
    history[current] = {
        "x": {j: current_plan["x"][j][current] for j in current_plan["x"]},
        "P": {i: current_plan["P"][i][current] for i in I_all},
        "SOC": {i: current_plan["SOC"][i][current] for i in I_b},
        "ON": {i: current_plan["ON"][i][current] for i in I_g},
        "is_chg": {i: current_plan["is_chg"][i][current] for i in I_b},
        "Sell": current_plan["Sell"][current],
        "k": {j: {i: current_plan["k"][j][i][current] for i in I_all} for j in current_plan["k"]},
        "Miss": {j: current_plan["Miss"].get(j, 0) for j in J_aperiodic}
    }

# ----------------- 資料輸出 -----------------------------
schedule_result = []
for t in T:
    P_dict = {i: round(history[t]["P"][i], 2) for i in I_all}
    k_dict = {}

    for j in J_periodic + J_aperiodic + accepted_sporadic + J_charging:
        if j in history[t]["k"]:
            allocations = {i: round(history[t]["k"][j][i], 2) for i in I_all if history[t]["k"][j][i] > 0}
            if allocations:
                k_dict[j] = allocations

    time_slot = {
        "t": t,
        "P": P_dict,
        "k": k_dict,
        "sell": round(history[t]["Sell"], 2),
        "soc": {i: round(history[t]["SOC"][i], 2) for i in I_b},
        "missed_aperiodic": [j for j in J_aperiodic if history[t]["Miss"].get(j, 0) == 1],
        "rejected_sporadic": rejected_sporadic
    }
    schedule_result.append(time_slot)

with open("../output/schedule_result_dynamic.json", "w", encoding="utf-8") as f:
    json.dump({"schedule_result": schedule_result}, f, ensure_ascii=False, indent=4)