# Virtual Power Plant (VPP) Real-Time Scheduling System
**虛擬電廠即時排程與最佳化系統**

## 專案簡介 (Introduction)
本專案模擬經營一個**虛擬電廠（Virtual Power Plant, VPP）**。系統整合了「傳統火力機組」、「再生能源（太陽能）」以及「儲能設備（電池）」，目標是在未來 72 小時內，精準調度電力以滿足各式各樣具備**時效限制 (Real-time constraints)** 的用電需求，同時追求**系統利潤最大化**與**違約成本最小化**。

本系統不僅實作了基礎的 **日前靜態排程 (Day-Ahead Static Scheduling, Level 1)**，更進一步開發了業界水準的 **滾動時域動態排程 (Rolling Horizon Dynamic Scheduling, Level 2)**，成功克服了天氣驟變、電池真實損耗與即時電價震盪等真實世界挑戰。

---

## 核心領域知識 (Domain Knowledge)

為了讓外部開發者快速理解本專案，以下整理系統中的三大類「用電需求 (Tasks)」特性：

1. **Periodic Task (週期性任務)**
   * **特性**：如工廠固定排班的製程，具有固定的發生週期、執行時間與**絕對死線 (Hard Deadline)**。
   * **排程要求**：絕對不能逾期，且部分任務不可中斷 (Non-preemptive)。
2. **Aperiodic Task (非週期性任務)**
   * **特性**：隨機發生的臨時用電，無週期性，具有**軟性死線 (Soft Deadline)**。
   * **排程要求**：允許逾期，但會面臨鉅額的違約罰款 (Penalty)。
3. **Sporadic Task (突發性任務)**
   * **特性**：突發且極度緊急的用電（如設備故障需緊急冷卻），具有**絕對死線 (Hard Deadline)**。
   * **排程要求**：系統必須執行 **接收測試 (Acceptance Test)**，若接下任務會導致既有排程崩潰，則必須果斷拒絕 (Reject)。

*(詳細的專案縮寫與名詞定義，請參閱附檔 `note.txt`)*

---

## 系統先決條件與物理限制 (System Constraints)

本系統非單純的貪婪演算法，而是利用 **混合整數線性規劃 (MILP)** 嚴格遵守以下真實世界的物理與商業限制：

* **發電機組限制**：包含出力上下限、升降載速率限制 (Ramp-up/down)、以及最嚴格的**最小連續開機/關機時間 (Min Up/Down Time)**。
* **儲能設備限制**：考慮電池的最大容量、充放電功率上限，且嚴格限制**同一時間點不可同時充放電**。在 Level 2 中更加入了自放電率、充放電效率與老化成本。
* **即時系統限制**：任務不可在釋放時間 (Release time) 前偷跑，且不可中斷任務 (Non-preemptive) 一旦啟動必須連續執行至結束。
* **能量平衡原則**：每一小時的總發電量，必須完美等於總用電量加上售出至市場的電量 (Sell)。

---

## 專案檔案架構 (File Structure)

本專案將不同的職責解耦，分為以下核心模組：

```text
├── src/
│   ├── note.txt                  # 專案名詞縮寫與術語對照表 (Terminology)
│   ├── task_generator.py         # 隨機生成合法的 Periodic Task 測試資料集
│   ├── demo_simulator.py         # 模擬器：動態注入 Aperiodic 與 Sporadic 突發任務
│   ├── scheduler.py              # [Level 1] MILP 靜態排程主程式與 Sporadic 接收測試
│   ├── advanced_scheduler.py     # [Level 2] 動態滾動排程主程式 (包含放寬假設與雷達預警)
│   └── evaluator.py              # 評估器：針對排程結果計算 KPI 與財報指標
├── input/
│   ├── processor_settings.json   # 發電機、電池、再生能源的硬體參數設定檔
│   └── price_72hr.json           # 未來 72 小時的市場電價預測
└── output/
    ├── task_set.json             # 生成的用電需求資料
    ├── schedule_result.json      # [Level 1] 靜態排程器輸出的最終排程決策
    ├── schedule_result_dynamic.json # [Level 2] 動態排程器輸出的最終排程決策
    └── evaluation_results.json   # 最終系統效能與財務結算報告
```

---

## 環境設定與執行流程 (How to Run)

### 1. 安裝依賴套件 (Prerequisites)
本專案使用 Python 開發，並依賴 `PuLP` 套件進行 MILP 最佳化求解。
```bash
pip install pulp
```

### 2. 執行順序 (Execution Flow)

**Step 1: 建立基礎用電需求**
```bash
python src/task_generator.py
```

**Step 2: 注入突發干擾事件**
```bash
python src/demo_simulator.py
```

**Step 3: 啟動大腦進行排程 (擇一執行)**
* **[Level 1] 日前靜態排程：**
  ```bash
  python src/scheduler.py
  ```
* **[Level 2] 滾動時域動態排程 (推薦)：**
  ```bash
  python src/advanced_scheduler.py
  ```
  *(會根據即時天氣驟變、電價震盪與突發任務動態重排程)*

**Step 4: 產出效能與財報**
```bash
python src/evaluator.py
```

---

## 核心技術亮點 (Technical Highlights)

### Level 1 基礎架構 (scheduler.py)
1. **線性化非線性邏輯 (Linearization)**：
   利用二元決策變數 (Binary Variables) 成功將 `min()`、`max()` 等轉換為純線性方程式，避免傳統 `if-else` 導致的死胡同。
2. **資源餘裕接收測試 (Slack Reserve Acceptance Test)**：
   在處理 Sporadic 任務時，利用 LP 算出的「預計售出電量 (Sell)」作為系統的資源餘裕 (Slack)。在不更動既有機組狀態的前提下，快速且安全地吸納突發任務。

### Level 2 進階架構 (advanced_scheduler.py)
1. **滾動時域控制與歷史鎖定 (Rolling Horizon & History Locking)**：
   放棄靜態排程，採用逐時推進的動態模擬。在重排程時透過「歷史鎖定法」強制固定過去的決策，完美解決傳統動態 LP 模型狀態交接斷裂的問題。
2. **2小時短視距天氣雷達 (Nowcasting Radar)**：
   導入真實氣候漸進變化（烏雲遮蔽），並為 LP 模型賦予未來 2 小時的預測視野。引導發電機提前啟動升載 (Ramp-up)，徹底消滅了氣候驟變導致的模型無解 (Infeasible) 危機。
3. **電池真實物理放寬 (Battery Degradation)**：
   引入充電效率(93%)、放電效率(93%)與自放電率。並在目標函數中加入「電池老化成本」，促使求解器發展出「Just-in-Time Charging (及時充電)」的商業智慧行為。

---

## 6-2 目標函數權衡分析與保留策略 (Trade-off Analysis)

在虛擬電廠的營運中，我們面臨了「維持高緊急應變能力」與「追求短線財報利潤」的兩難。本組在設計 LP 目標函數時，進行了深度的權衡分析。

### 軟性備轉容量懲罰 (Soft Reserve Penalty) vs. 售電收益 (Revenue)
在原始模型中，LP 求解器為了追求短線利潤，往往會在電價低迷時將機組降載至極限，導致 `Sell` (備用電量) 為 0，進而使緊急突發的 **Sporadic 任務** 接納率 (Value Rate) 極低。

為了解決此問題，我們並未採用粗暴的硬限制（這會導致尖峰時刻無解），而是導入了 **「引導性目標 (Surrogate Objective)」** 技巧：
1. 我們在目標函數中加入了「未達備轉目標的懲罰金 (Penalty)」。
2. **權衡差異**：若未加入此懲罰，系統發電成本極低，市場售電利潤最大化；但加入懲罰後，求解器被「虛擬罰金」逼迫，寧願承擔較高的燃料成本啟動火力機組，也要維持系統餘裕。
3. **脫鉤的藝術**：這筆虛擬的 Penalty 並不會計入最終 Evaluator 的真實財報中。我們實質上是用「發電成本上升 / 售電收益下降」的真實代價，成功換取了 Sporadic 任務接納率的大幅飆升。

### 管理哲學：優先處理緊急狀況 (Long-term Strategy)
就短期財報而言，放棄賣電並啟動昂貴機組來伺候零星的 Sporadic 任務似乎是「虧本生意」。
然而，從 VPP 長遠的營運大計來看，**Sporadic 任務代表的是工廠設備異常或急難救助等「極度緊急狀況」**。本組認為，做為穩定的能源供應商，**「優先確保緊急狀況獲得解決」的價值遠高於「短線的市場套利」**。透過此權衡設定，我們確保了電網的極致可靠性與長期的商譽價值。