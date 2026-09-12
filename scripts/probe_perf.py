# -*- coding: utf-8 -*-
"""性能探针：插桩 LLM 调用与 TOPTW 求解，定位 M7 链路瓶颈。"""
import io, sys, os, time, inspect
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cfg = __import__("src.config", fromlist=["load_config"]).load_config()
os.environ["DEEPSEEK_API_KEY"] = cfg["deepseek_api_key"]
os.environ["M1_MODEL"] = cfg.get("m1_model", "")

from src import llm_client, toptw, poi_db, proposal_planner

T0 = time.time()

def chat_spy(messages, **kw):
    fr = inspect.stack()[1]
    tag = f"{os.path.basename(fr.filename)}:{fr.lineno}"
    t = time.time()
    r = llm_client._chat_orig(messages, **kw)
    dt = time.time() - t
    n = sum(len(m.get("content") or "") for m in messages)
    print(f"  [{time.time()-T0:6.1f}s] LLM  {tag}  耗时 {dt:5.1f}s  入参 {n} 字符", flush=True)
    return r

llm_client._chat_orig = llm_client.chat
llm_client.chat = chat_spy

def solve_spy(*a, **kw):
    t = time.time()
    r = toptw._solve_orig(*a, **kw)
    dt = time.time() - t
    print(f"  [{time.time()-T0:6.1f}s] TOPTW  耗时 {dt:5.1f}s", flush=True)
    return r

toptw._solve_orig = toptw.solve_day
toptw.solve_day = solve_spy

city = poi_db.load_city("武汉")
t = time.time()
print("=== 开始 M7 规划：武汉2天经典深度游，喜欢历史文化、寺庙和博物馆 ===", flush=True)
r = proposal_planner.plan(city, "武汉2天经典深度游，喜欢历史文化、寺庙和博物馆", 2, use_llm=True)
print(f"\n=== 总耗时 {time.time()-t:.1f}s | 模式 {r['mode']} | 违规 {r['itinerary']['total_violations']} ===", flush=True)
