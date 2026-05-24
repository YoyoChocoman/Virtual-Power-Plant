from random import randint as rand
import json
import math

period_choice = [6, 8, 9, 12, 18, 24]
release, period, execution, dl, wat, preemp = [], [], [], [], [], []
tasks = 0

def frame_check():
    possible_f =[3, 4, 6, 8, 9, 12, 18, 24, 36, 72]

    for f in possible_f:
        if f < max(execution): continue

        for i in range(tasks):
            if 2 * f - math.gcd(f, period[i]) > dl[i]: break
        else: return True

    return False

def examine():
    cnt1, cnt2, jobs, density = 0.0, 0, 0, 0.0
    for i in range(tasks):
        if dl[i] == execution[i]:
            cnt1 += 1.0
        if execution[i] != 1 and preemp[i] == 0:
            cnt2 += 1
        jobs += 72 / period[i]
        density += float(execution[i]) / float(period[i])

    if execution.count(2) < 2: return False
    if tasks - execution.count(2) - execution.count(1) < 1: return False
    if len([i for i in wat if i >= 14]) < 2: return False
    if cnt1 < float(tasks) / 5.0: return False
    if cnt2 < 2: return False
    if jobs <= 30: return False
    if density < 0.7 or density > 1.0: return False
    if len(set(period)) < 3: return False

    return frame_check()

while (True):
    release, period, execution, dl, wat, preemp = [], [], [], [], [], []
    tasks = rand(6, 10)
    for i in range(tasks):
        period.append(period_choice[rand(0, 5)])

        release.append(rand(1, period[i]))

        execution.append(rand(1, 4))

        dl.append(rand(execution[i], period[i]))

        wat.append(rand(6, 18))

        preemp.append(rand(0, 1))

    if examine():
        break

data = {"periodic": {}}

for i in range(tasks):
    data["periodic"][f"p{i+1}"] = {
        "r": release[i],
        "p": period[i],
        "e": execution[i],
        "d": dl[i],
        "w": wat[i],
        "preempt": preemp[i]
    }


with open("../output/task_set.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)