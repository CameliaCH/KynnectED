import io
import json
from pathlib import Path


def matching_results(users, include_scores=True):
    data = [{"subjects": [], "availability": {}, "gender": None, "gender_preferred": [],
             "age": 0, "age_preferred": [], "name": [], **user} for user in users]
    output = io.StringIO()
    pairs = []
    scores = []

    for i in range(len(data)):
        for j in range(i+1, len(data)):
            if i != j:
                if data[i]["role"] == data[j]["role"]: continue
                pairs.append([data[i], data[j]])

    for i in pairs:
        scores.append([len(set(i[0]["subjects"]).intersection(set(i[1]["subjects"])))])

    for i in range(len(pairs)):
        a = pairs[i][0]["availability"]
        b = pairs[i][1]["availability"]
        scores[i].append(0)
        for day in ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]:
            scores[i][-1] += len(set(a.get(day, [])).intersection(set(b.get(day, []))))

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

    result = []
    for pair, score in zip(pairs, scores):
        first, second = pair
        first_name = " ".join(first["name"])
        second_name = " ".join(second["name"])
        label = f"ID: {first['id']} Name: {first_name}, ID: {second['id']} Name: {second_name}"
        result.append([score, label, first["id"], first_name, second["id"], second_name])
    result.sort(key=lambda row: row[0], reverse=True)
    if include_scores:
        print("All pair scores:", file=output)
        for row in result:
            print(" ".join(map(str, row)), file=output)
        print(file=output)
    print("Optimal matches:", file=output)
    if not result:
        print("Not enough accounts to find a match.", file=output)

    used = set()
    for row in result:
        if row[2] not in used and row[4] not in used:
            used.add(row[2])
            used.add(row[4])
            print(row[2], row[3], row[4], row[5], file=output)
    for user in data:
        if user["id"] not in used:
            name = " ".join(user["name"])
            print(f"\nUnable to find a match for {name} (ID {user['id']})", file=output)
    return output.getvalue()


if __name__ == "__main__":
    with Path(__file__).with_name("users.json").open(encoding="utf-8") as source:
        print(matching_results(json.load(source)), end="")
