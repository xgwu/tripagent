# -*- coding: utf-8 -*-
"""M2 求解层：OR-Tools RoutingModel 单日 TOPTW。

对应可行性分析 §3.2 的设计：
- 候选池 = LLM 主选（高利润）+ 多路召回备选（低利润），求解器有权「换点」
- 利润函数 = 主选标记 × 大权重 + 评分 + LLM 顺位奖励（只作用于「留谁」；
  访问顺序由弧成本与时间窗决定，忠实模式下顺位奖励被 1e6 淹没，见 RANK_BONUS 注释）
- 硬约束 = 营业时间窗 + 通行时间（L1 矩阵）+ 当日时间上限，由求解器保证可行
- 超时/不可行 → 自动降级 M1 贪婪链路
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from src import poi_db

MAIN_BONUS = 600           # 主选 POI 的额外利润（M3-1 扫描：300~1000 结果一致，取中位留余量）
RATING_W = 60              # 每分评分的利润
RANK_BONUS = 15            # LLM 顺位奖励：只影响「留谁」，**不影响访问顺序**
# ⚠️ 2026-09-17 口径纠正：利润只进 AddDisjunction 的「丢弃罚分」，访问顺序由
# 弧成本（travel_min）与时间窗决定 —— 即行程内的先后完全是几何/时窗最优，
# 与 LLM 给的顺序无关。且默认开启的 lock_mains 会给每个主选 +1e6，
# 使 MAIN_BONUS/RATING_W/RANK_BONUS 三者在主选之间的差异被彻底淹没
# （600/300/15 vs 1e6），此时 rank 对结果无任何影响。
# 所以文件头「相似度目标，Google 用 70% 权重」只在 lock_mains=False 且
# 时间预算真的装不下时才部分生效——别据此以为提案顺序会被尊重。
SOFT_W = 5.0               # best_time 软时间窗：每偏差 1 分钟的罚分（M3-1 扫描最优：均距/覆盖显著改善）
SOFT_CAP_MIN = 60          # 软窗罚分封顶：偏差超过该分钟数等效罚分封顶（soft_w×SOFT_CAP_MIN=300），
                           # 再远不允许——旧逻辑线性无上限（晚到 5h 罚 1500 分），求解器宁可让傍晚空着
                           # 也不放 morning 点进晚间，时段空间被白扔；封顶后空窗由日内填空补晚间型点
WAIT_SLACK_MAX_MIN = 120   # 单点最大等待（slack）——与 m2_planner.GAP_WAIT_MAX_H(2h) 同口径。
                           # 0 会让「19:00 开门」类点结构性不可行（见 AddDimension 处注释）；
                           # 无上限则允许荒谬空等，故取有界值。
                           # 注：不再叠加「总跨度代价」——实测（2026-09-17）跨度计价
                           # **压不掉空档却会吃掉 POI**：广州 300min 空档在 span=0/1 下
                           # 完全相同，而苏州 6 点掉到 5 点、span=5 时更是崩到 1 点。
                           # 原因是这类空档是结构性的（要排 19:00 的点就必然跨到 21:30），
                           # 跨度代价唯一的「优化」手段就是把夜间点丢掉，正好退回本次要修的
                           # 缺陷。空档由 sequencer.scan_gaps 披露（M15 既定取舍：披露不惩罚）。
TIME_LIMIT_S = 2.0         # 单日求解预算
MEAL_BUFFER_MIN = 120      # M6：排序器会在日中插入午餐+晚餐各 1h，求解器预算预扣，防止过度打包后整体后移溢出
LATE_POINT_MARGIN_MIN = 60 # 晚间型点（18:00 后开门）预算放宽时预留的收尾/返程余量

# 建议时段 → 软时间窗（相对 day_start 的小时时刻；None = 该侧不约束）
SLOT_WINDOWS = {
    "morning":   (None, 12.0),   # 最好上午到（soft upper）
    "afternoon": (13.0, None),   # 最好 13 点后到（soft lower）
    "evening":   (17.0, None),   # 夜间体验类
    "lunch":     (11.0, 13.5),   # 餐饮类，午市窗口
}


def solve_day(candidates: list, day_ids: list, city: dict, all_pois: dict,
              time_limit_s: float = TIME_LIMIT_S,
              main_bonus: float = MAIN_BONUS, soft_w: float = SOFT_W,
              hotel: dict | None = None, forced: set | None = None,
              mode: str | None = None, lock_mains: bool = False,
              day_no: int | None = None):
    """单日 TOPTW。

    candidates: 备选池（parsed POI，含主选与备选）
    day_ids:    LLM 主选（有序，顺序代表优先级）
    hotel:      M6 住宿锚点 —— 传入则作为 depot（每日强制从酒店出发并返回）
    forced:     必选点集合（如住宿锚点地标）——利润放大至不可舍弃，时间可行性仍由求解器硬约束保证
    mode:       出行方式（None=车驾 | cycling/hiking）—— 通行矩阵按该方式的速度模型计算，
                骑行主题下求解器的时间预算与选点半径与骑行者真实能力对齐
    lock_mains: 忠实执行模式（toptw_faithful_mode）——主选视为必选（同 forced 口径的
                极大利润），求解器只优化顺序与可行性；主选落选的唯一原因是时间预算
                真装不下（闭馆主选已在池构造前剔除），落选后由阶段3 alt 换位兜底
    返回: (ordered_ids, dropped_ids, solved_flag)
    """
    forced = forced or set()
    if not candidates:
        return [], [], True

    day_ids = [i for i in day_ids if i in {p["id"] for p in candidates}]
    rank = {pid: len(day_ids) - k for k, pid in enumerate(day_ids)}  # 越靠前越大

    # 按天起始时刻覆盖（跨城联游抵达日）：与 sequencer.day_start_of 共用口径，
    # 否则求解器按 09:00 给预算、排序器按抵达时刻起算 → 多塞的点会被逐个判超时剔除
    from src.sequencer import day_start_of, day_end_of
    start_day = poi_db.hhmm_to_h(day_start_of(city, day_no))
    # day_end 同样支持按天覆盖：夜间转移日的可用时间提前到出发时刻，
    # 否则求解器按 21:30 给预算、排到 21:30 的点会与 21:00 出发撞车
    end_day = poi_db.hhmm_to_h(day_end_of(city, day_no))
    # M6：求解器 horizon 预扣餐块缓冲（排序器实测会插入午餐+晚餐）；不足则保底 4h 活动时间
    day_window = max(240, int((end_day - start_day) * 60))
    horizon = max(240, day_window - MEAL_BUFFER_MIN)

    def to_min(h_abs):
        return int(round((h_abs - start_day) * 60))

    # 预算放宽（2026-09-15 夜间型点缺陷）：餐块预扣是"防过度打包"的保守估计，但它不能
    # 让**营业窗口本来可排**的点变成不可行——只要池里有 18:00 后开门的点，预扣后 horizon
    # 会小于「该点最早可结束时刻 + 返程腿」，该点就被判死：实测 宝业路宵夜街(18:00-02:00)
    # 与 珠江夜游(19:00-21:30) 在 horizon=630 下恒不可行，预扣降到 60 立刻入选。
    # 故按池内最晚需求放宽，上限 = 真实日窗（排序器口径 09:00→21:30）——求解器放行的
    # 解在排序器侧仍可能判超时，但那是可解释的剔点，好过整类夜间点结构性排不进。
    latest_end = max((to_min(p["open_h"]) + int(round(p["dur"] * 60)) for p in candidates),
                     default=0)
    if latest_end + LATE_POINT_MARGIN_MIN > horizon:
        horizon = max(horizon, min(day_window, latest_end + LATE_POINT_MARGIN_MIN))

    # M5 预剔除：闭店前玩不完的 POI（to_min(close)-dur < 0）窗口为空，进模型会 CP Solver fail
    candidates = [p for p in candidates
                  if to_min(p["close_h"]) - int(round(p["dur"] * 60)) >= 0]
    if not candidates:
        return [], [], True

    def profit(p):
        pr = RATING_W * p["rating"] + RANK_BONUS * rank.get(p["id"], 0)
        if p["id"] in rank:
            pr += main_bonus
            if lock_mains:
                # 忠实执行模式（toptw_faithful_mode）：主选视为必选（同 forced 口径的
                # 极大利润），求解器只优化顺序与可行性；主选落选的唯一原因是时间预算
                # 真装不下（闭馆主选已在池构造前剔除），落选后由阶段3 alt 换位兜底
                pr += 1_000_000
        if p["id"] in forced:
            pr += 1_000_000  # 必选点（住宿锚点地标）：舍弃代价远超任何组合收益
        # 点级需求加成（如湖偏好的湖线点）：时间预算不足时优先剔非加成点。
        # 由调用方在候选池上以 p["_bonus"] 塞入（dict 拷贝，不污染 all_pois）。
        # 必须 int——RoutingModel.AddDisjunction 的罚分要求 int64。
        pr += int(p.get("_bonus") or 0)
        return int(pr)

    nodes = list(candidates)
    # M6：酒店锚点作为 depot —— 每日强制从酒店出发并返回；无酒店则取主选第一点作 depot
    if hotel is not None:
        h = dict(hotel)
        h["open_h"], h["close_h"], h["dur"] = 0.0, 24.0, 0.0  # 虚拟节点：全天开放、不占游玩时长
        nodes = [h] + nodes
    elif day_ids:
        # 无酒店：depot 取主选第一点（以首点为出发/收尾锚点，减少首跳空驶）。
        # ⚠️ 换位必须发生在 RoutingIndexManager 构造之前——索引映射、时间窗、
        # disjunction 罚分全部按 nodes 的**位置**建立；挪到其后会导致
        # 「位置 idx 的 POI 被换掉了，但该位置的窗口与罚分仍按旧点算」的静默错位。
        # 定位用「基于 id 的下标」而非 list.index(obj)：POI 为 dict，相等性判断
        # 可能让 index() 返回非目标位置（旧实现即栽在这里）。
        k = next((i for i, p in enumerate(nodes) if p["id"] == day_ids[0]), 0)
        if k != 0:
            nodes[0], nodes[k] = nodes[k], nodes[0]  # 显式下标交换，RHS 先求值后落位
    n = len(nodes)

    # 注意：本 ortools 构建的绑定参数顺序为 (num_nodes, num_vehicles, starts, ends)
    # 实证：传 (1, n, 0) 会得到 n 辆车、1 个节点 —— 故显式按 (n, 1, [0], [0]) 传入
    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [0])  # 1 车，depot=节点0
    routing = pywrapcp.RoutingModel(manager)

    def travel_min(i, j):
        ni, nj = nodes[manager.IndexToNode(i)], nodes[manager.IndexToNode(j)]
        if manager.IndexToNode(i) == manager.IndexToNode(j):
            return 0
        return int(round(poi_db.travel_hours(ni, nj, mode) * 60))

    transit_cb = routing.RegisterTransitCallback(travel_min)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    # 时间维度：transit 含「上游节点游玩时长 + 通行」，节点时间窗 = 营业时间
    def full_transit(i, j):
        if i == j:
            return 0  # 自环（未被访问的可选节点）必须 0 传播，否则维度不可行
        ni = nodes[manager.IndexToNode(i)]
        return int(round(ni["dur"] * 60)) + travel_min(i, j)

    full_cb = routing.RegisterTransitCallback(full_transit)
    # fix_start=False：出发时间浮动（depot 是真实 POI，营业窗口未必包含 day_start）
    #
    # ⚠️ 2026-09-17 修复（高严重度）：slack_max 此前是 0，即**禁止在任何节点等待**，
    # cumul 沿路径刚性递推（cumul[j] == cumul[i] + transit）。后果是「营业窗口本来
    # 可排」的点变成数学上不可行——实测「上午馆(09-17) + 下午馆(09-17) + 夜市(19-23)」
    # 三点相邻，slack=0 只落地 2/3（夜市被丢），即使忠实模式给主选 1e6 利润也救不回来，
    # 因为**利润权重解决不了不可行**。M15 的 LATE_POINT_MARGIN_MIN 放宽 horizon 只治了
    # 症状；README「遗留」写的「TOPTW 不惩罚空档」也说反了——它是**无法表达**空档。
    #
    # 取值：WAIT_SLACK_MAX_MIN 与 m2_planner.GAP_WAIT_MAX_H(2h) 对齐，
    # 允许合理等待（等开门/等餐点），但不允许「上午排完空等 6 小时到夜market」。
    routing.AddDimension(full_cb, WAIT_SLACK_MAX_MIN, horizon, False, "time")
    tdim = routing.GetDimensionOrDie("time")
    for idx in range(routing.Size()):
        node = manager.IndexToNode(idx)
        lo = max(0, to_min(nodes[node]["open_h"]))          # 00:00 开放/午夜前需钳位到维度域
        # 窗口语义与 sequencer 对齐：到达 + 游玩时长 ≤ 闭店（到达截止 = 闭店 - 时长）
        hi = min(horizon, to_min(nodes[node]["close_h"]) - int(round(nodes[node]["dur"] * 60)))
        if lo > hi:
            lo = hi
        tdim.CumulVar(idx).SetRange(lo, hi)
        # M3-2：best_time 软时间窗（偏差每分钟罚 soft_w，与硬约束的营业窗口叠加）
        if soft_w > 0:
            sl, su = SLOT_WINDOWS.get(nodes[node].get("best_time", "any"), (None, None))
            if sl is not None:
                sl_m = min(horizon, max(0, to_min(sl)))
                tdim.SetCumulVarSoftLowerBound(idx, sl_m, int(soft_w))
                # 罚分封顶（下侧）：早到偏差超过 SOFT_CAP_MIN 分钟不再允许
                lo = max(lo, sl_m - SOFT_CAP_MIN)
            if su is not None:
                su_m = min(horizon, max(0, to_min(su)))
                tdim.SetCumulVarSoftUpperBound(idx, su_m, int(soft_w))
                # 罚分封顶（上侧）：晚到偏差超过 SOFT_CAP_MIN 分钟不再允许——
                # morning 点要么贴近其时段安排，要么干脆不排（弃利润），
                # 而不是拖着整条线到深夜、把傍晚空间白扔
                hi = min(hi, su_m + SOFT_CAP_MIN)
            if lo > hi:          # 软窗封顶与营业窗冲突 → 营业窗优先（能开就行）
                lo = hi
    # 终点：不晚于当日结束（depot 即终点）
    tdim.CumulVar(routing.End(0)).SetRange(0, horizon)

    # 可选访问：非主选可以不访问（penalty = 利润，丢弃损失利润）
    for idx in range(1, n):
        node = manager.IndexToNode(idx)
        routing.AddDisjunction([idx], profit(nodes[node]))
    # depot（nodes[0]）已在上方 RoutingIndexManager 构造前定好：
    # 有酒店 = 酒店虚拟节点，无酒店 = 主选第一点。disjunction 从 idx=1 开始添加，
    # depot（idx=0）天然不在其中 → 必被访问（这正是「强制从锚点出发」的实现方式）。

    p = pywrapcp.DefaultRoutingSearchParameters()
    p.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    p.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    p.time_limit.FromSeconds(int(time_limit_s))
    p.log_search = False

    # P1 提前停止：GLS 在预算末段常陷入「解略有提升但耗时照付」的空转——
    # 实测 2s 预算 100% 打满。挂 at-solution 回调，目标值连续两次提升 <0.5% 即认为收敛，
    # FinishCurrentSearch 提前结束（SolveWithParameters 仍返回当前最优解）。
    obj_hist: list = []

    def _at_solution():
        try:
            obj_hist.append(routing.CostVar().Max())
        except Exception:  # noqa: 取值失败则放弃判断，等超时兜底
            return
        if len(obj_hist) >= 3:
            prev, cur = obj_hist[-2], obj_hist[-1]
            if prev and abs(prev - cur) / max(abs(prev), 1) < 0.005:
                routing.solver().FinishCurrentSearch()

    routing.AddAtSolutionCallback(_at_solution)
    sol = routing.SolveWithParameters(p)

    if sol is None:
        return [], [], False

    # 提取访问序列（depot 本身就是一个真实 POI，必须计入序列）与被丢弃节点
    idx, route_nodes = routing.Start(0), []
    while not routing.IsEnd(idx):
        route_nodes.append(manager.IndexToNode(idx))
        idx = sol.Value(routing.NextVar(idx))
    ordered = [nodes[i]["id"] for i in route_nodes]
    # M6：酒店是 depot（起终点），不属于行程访问序列
    if hotel is not None:
        ordered = [i for i in ordered if i != "HOTEL"]
    dropped = [p_["id"] for p_ in nodes
               if p_["id"] not in set(ordered) and p_["id"] != "HOTEL"]
    return ordered, dropped, True
