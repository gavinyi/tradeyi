import json
import pathlib

def dump_templates():
    json_path = pathlib.Path("src/futu_quant/config_center/_specs_data.json")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 提取所有包含 '{' 的 pattern
    found = []
    for spec in data:
        key = spec.get("key", "")
        aliases = spec.get("aliases", [])
        runtime_key = spec.get("runtime_key", "")
        scope = spec.get("scope", "")
        risk = spec.get("risk_level", "")
        desc = spec.get("description", "")
        
        all_candidates = set()
        for p in [key] + aliases + ([runtime_key] if runtime_key else []):
            if "{" in p and "}" in p:
                all_candidates.add(p)
                
        for cand in sorted(all_candidates):
            found.append({
                "pattern": cand,
                "canonical": key,
                "scope": scope,
                "risk": risk,
                "runtime_key": runtime_key,
                "desc": desc,
            })
            
    # 按 pattern 排序
    found.sort(key=lambda x: x["pattern"])
    print(f"Total matching pattern entries: {len(found)}")
    for idx, item in enumerate(found, 1):
        print(f"{idx}. {item['pattern']}")
        print(f"   canonical: {item['canonical']}")
        print(f"   scope: {item['scope']}, risk: {item['risk']}, runtime_key: {item['runtime_key']}")
        print(f"   desc: {item['desc']}")

if __name__ == "__main__":
    dump_templates()
