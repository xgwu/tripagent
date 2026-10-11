# -*- coding: utf-8 -*-
"""Phase 2 探针：离线验证拟案器 + dry_run 全命中（零 LLM、零成本）。"""
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from eval_regression import AGENT_CASES, _draft_proposal  # noqa: E402
from src import poi_db, proposal_planner  # noqa: E402

for city_name, query, days, per_day, must in AGENT_CASES:
    city = poi_db.load_city(city_name)
    prop = _draft_proposal(city, days, per_day, must, query)
    dry = proposal_planner.validate_proposal(city, prop, days)
    n_stops = sum(len(d["stops"]) for d in prop["days"])
    print(f"{city_name}: 拟案 {n_stops} 点 dry_ok={dry['ok']} "
          f"unmatched={len(dry['unmatched'])} warn={len(dry['warnings'])}")
    if not dry["ok"]:
        print("  unmatched:", [u.get("name") for u in dry["unmatched"]])
    if dry["warnings"]:
        print("  warnings:", dry["warnings"][:2])
