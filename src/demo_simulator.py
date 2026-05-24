from random import randint as rand
import json

#讀取 task_generator.py 產生的 periodic task
try:
    with open("../output/task_set.json", "r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    print("找不到 task_set.json, 請先執行 task_generator.py")
    exit()

#加入 Aperiodic Tasks (Soft Deadline)
data["aperiodic"] = {}
num_a = rand(7, 13)
for i in range(num_a):
    e = rand(1, 4)
    w = rand(5, 15)
    r = rand(1, 72 - e)
    d = rand(e, e + 12) #隨便抓的

    data["aperiodic"][f"a{i+1}"] = {
        "r": r,
        "e": e,
        "d": d,
        "w": w,
        "preempt": rand(0, 1)
    }

#加入 Sporadic Tasks (Hard Deadline)
data["sporadic"] = {}
num_s = rand(4, 7)
for i in range(num_s):
    e = rand(1, 3)
    w = rand(5, 20)
    r = rand(1, 72 - e)
    d = rand(e, e + 6) #隨便抓的

    data["sporadic"][f"s{i+1}"] = {
        "r": r,
        "e": e,
        "d": d,
        "w": w,
        "preempt": rand(0, 1)
    }

with open("../output/task_set.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)

print(f"成功加入 {num_a} 個 Aperiodic 任務與 {num_s} 個 Sporadic 任務至 task_set.json")