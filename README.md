# Virtual Power Plant (VPP) Real-Time Scheduling System
**虛擬電廠即時排程與最佳化系統**

## 專案簡介 (Introduction)
本專案模擬經營一個**虛擬電廠（Virtual Power Plant, VPP）**。系統整合了「傳統火力機組」、「再生能源（太陽能）」以及「儲能設備（電池）」，目標是在未來 72 小時內，精準調度電力以滿足各式各樣具備**時效限制 (Real-time constraints)** 的用電需求，同時追求**系統利潤最大化**與**違約成本最小化**。

本系統核心採用 **混合整數線性規劃 (MILP, Mixed-Integer Linear Programming)** 建立數學模型，完美解決傳統排程演算法難以處理的「機組最小連續開關機時間」、「電池充放電互斥」等複雜物理限制。

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

本系統非單純的貪婪演算法，而是嚴格遵守以下真實世界的物理與商業限制：

* **發電機組限制**：包含出力上下限、升降載速率限制 (Ramp-up/down)、以及最嚴格的**最小連續開機/關機時間 (Min Up/Down Time)**。
* **儲能設備限制**：考慮電池的最大容量、充放電功率上限，且嚴格限制**同一時間點不可同時充放電**。
* **即時系統限制**：任務不可在釋放時間 (Release time) 前偷跑，且不可中斷任務 (Non-preemptive) 一旦啟動必須連續執行至結束。
* **能量平衡原則**：每一小時的總發電量，必須完美等於總用電量加上售出至市場的電量 (Sell)。

---

## 專案檔案架構 (File Structure)

本專案將不同的職責解耦，分為以下核心模組：

```text
├── src/
│   ├── note.txt              # 專案名詞縮寫與術語對照表 (Terminology)
│   ├── task_generator.py     # 隨機生成合法的 Periodic Task 測試資料集
│   ├── demo_simulator.py     # 模擬器：動態注入 Aperiodic 與 Sporadic 突發任務
│   ├── scheduler.py          # [核心] MILP 靜態排程主程式與 Sporadic 接收測試
│   └── evaluator.py          # 評估器：針對排程結果計算 KPI 與財報指標
├── input/
│   ├── processor_settings.json # 發電機、電池、再生能源的硬體參數設定檔
│   └── price_72hr.json       # 未來 72 小時的市場電價預測
└── output/
    ├── task_set.json         # 生成的用電需求資料
    ├── schedule_result.json  # 排程器輸出的 72 小時最終排程決策
    └── evaluation_results.json # 最終系統效能與財務結算報告
```

---

## 環境設定與執行流程 (How to Run)

### 1. 安裝依賴套件 (Prerequisites)
本專案使用 Python 3.8+ 開發，並依賴 `PuLP` 套件進行 MILP 最佳化求解。
```bash
pip install pulp
```

### 2. 執行順序 (Execution Flow)
請依序執行以下程式，體驗完整的虛擬電廠排程流程：

**Step 1: 建立基礎用電需求**
```bash
python src/task_generator.py
```
> *說明：此步驟會嚴格遵照即時系統理論 (如 Workload density, Frame size 限制)，隨機生成 `output/task_set.json`。*

**Step 2: 注入突發干擾事件**
```bash
python src/demo_simulator.py
```
> *說明：在基礎需求中，隨機加入臨時的 Aperiodic 與 Sporadic 任務，模擬真實世界的不可預期性。*

**Step 3: 啟動大腦進行排程**
```bash
python src/scheduler.py
```
> *說明：*
> * *建構超過千個變數與限制式的 MILP 模型，計算最佳日前排程 (Day-ahead schedule)。*
> * *利用未售出的餘裕電量 (Slack Reserve) 進行 Sporadic 任務的 Acceptance Test，確保系統穩定。*
> * *產出 `output/schedule_result.json`。*

**Step 4: 產出效能與財報**
```bash
python src/evaluator.py
```
> *說明：解析排程結果，計算 Hard/Soft Deadline Miss Rate、Tardiness、Jitter 等即時系統指標，並結算最終發電成本與市場售電收益，輸出至 `evaluation_results.json`。*

---

## 核心技術亮點 (Technical Highlights)

1. **線性化非線性邏輯 (Linearization)**：
   利用二元決策變數 (Binary Variables) 成功將 `min()`、`max()` 以及絕對值 (如任務連續性限制) 轉換為純線性方程式，避免傳統 `if-else` 導致的死胡同。
2. **聰明的備援策略 (Slack Reserve for Acceptance Test)**：
   在處理 Sporadic 任務時，不盲目重啟耗時的排程器。而是巧妙利用 LP 算出的「預計售出電量 (Sell)」作為系統的資源餘裕 (Slack)。在不更動既有機組狀態的前提下，快速且安全地吸納突發任務。
3. **歷史狀態繼承 (Initial State Handling)**：
   完美處理了 `t=0` 時機組的歷史狀態（例如已連續開機 2 小時），確保排程邊界不會產生違規。