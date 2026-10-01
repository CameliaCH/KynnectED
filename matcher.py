import json

with open("users.json", "r") as f:
    data = json.load(f)

pairs = []
scores = []

for i in range(len(data)):
    for j in range(i+1, len(data)):
        if i != j:
            pairs.append([data[i], data[j]])

for i in pairs:
    scores.append([len(set(i[0]["subjects"]).intersection(set(i[1]["subjects"])))])

for i in range(len(pairs)):
    a = pairs[i][0]["availability"]
    b = pairs[i][1]["availability"]
    scores[i].append(0)
    for day in ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]:
        scores[i][-1] += len(set(a[day]).intersection(set(b[day])))

for i in range(len(pairs)):
    scores[i].append(0)

    if pairs[i][0]["gender"] == pairs[i][1]["gender"]:
        if "same" in pairs[i][0]["gender_preferred"] and "same" in pairs[i][1]["gender_preferred"]:
            scores[i][-1] += 1
    else:
        if "opposite" in pairs[i][0]["gender_preferred"] and "opposite" in pairs[i][1]["gender_preferred"]:
            scores[i][-1] += 1
    
    if pairs[i][0]["age"] > pairs[i][1]["age"]:
        if "younger" in pairs[i][0]["age_preferred"] and "older" in pairs[i][1]["age_preferred"]:
            scores[i][-1] += 1
    elif pairs[i][0]["age"] < pairs[i][1]["age"]:
        if "older" in pairs[i][0]["gender_preferred"] and "younger" in pairs[i][1]["age_preferred"]:
            scores[i][-1] += 1
    else:
        if "same-age" in pairs[i][0]["gender_preferred"] and "same-age" in pairs[i][1]["gender_preferred"]:
            scores[i][-1] += 1


print("All pair scores:")
result = sorted([[scores[i], f'ID: {pairs[i][0]["id"]} Name: {" ".join(pairs[i][0]["name"])}, ID: {pairs[i][1]["id"]} Name: {" ".join(pairs[i][1]["name"])}', pairs[i][0]["id"], " ".join(pairs[i][0]["name"]), pairs[i][1]["id"], " ".join(pairs[i][1]["name"])] for i in range(len(pairs))], key=lambda x: x[0], reverse=True)
for i in result:
    print(" ".join(map(str,i)))
print("\nOptimal matches:")

used = set()
ids = set([i["id"] for i in data])

for i in result:
    if i[2] not in used and i[4] not in used:
        used.add(i[2])
        used.add(i[4])
        print(i[2], i[3], i[4], i[5])
if len(data)%2:
    print(f"\nOdd number of people; unable to find a match for {' '.join(data[[*ids-used][0]-1]['name'])} (ID {[*map(str, ids-used)][0]})")
