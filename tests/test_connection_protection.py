"""换乘保联决策：保联约束、额外晚点预算、目标字典序、选中未接上、输入校验与 API。"""
from datetime import datetime

import pytest

from app import create_app
from planner import (
    TimetableError,
    plan_connection_protection,
    plan_delay_propagation,
    validate_timetable,
)

DAY = "2026-10-01"

STATIONS = [
    {"id": "A", "transfer_minutes": 10},
    {"id": "B", "transfer_minutes": 10},
    {"id": "C", "transfer_minutes": 10},
    {"id": "D", "transfer_minutes": 10},
    {"id": "E", "transfer_minutes": 10},
    {"id": "F", "transfer_minutes": 10},
]


def hm(value, day=DAY):
    return f"{day}T{value}"


def dt(value, day=DAY):
    return datetime.strptime(f"{day}T{value}", "%Y-%m-%dT%H:%M")


def train(tid, stops_spec):
    """stops_spec：[(站点, 到达或 None, 发车或 None), ...]，时间为完整 ISO 字符串或 None。"""
    return {"id": tid, "stops": [
        {"station": sid, "arrive": arrive, "depart": depart}
        for sid, arrive, depart in stops_spec
    ]}


def tt(trains, stations=None):
    return validate_timetable({"stations": stations or STATIONS, "trains": trains})


def protect(timetable, ids, delays, clearance, budget, connections):
    return plan_connection_protection(
        timetable, ids, delays, clearance, budget, connections
    )


def adjusted(result, tid):
    """结果中指定班次的 [(到达, 发车), ...]（datetime 或 None）。"""
    for item in result["trains"]:
        if item["train_id"] == tid:
            return [(s["arrive"], s["depart"]) for s in item["stops"]]
    raise AssertionError(f"结果中没有班次 {tid}")


def extra_delay(result, tid):
    for item in result["trains"]:
        if item["train_id"] == tid:
            return item["extra_delay_minutes"]
    raise AssertionError(f"结果中没有班次 {tid}")


def basic_timetable():
    """T1：A 08:00 → B 08:30；T2：B 08:40 → C 09:10（B 站换乘 10 分钟）。"""
    return tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
    ])


BASIC_CONN = {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0,
              "people": 100}


# ---------- 基本保联 ----------

def test_basic_protection():
    # T1 首发延误 20 分钟：基准到 B 08:50，T2 基准 08:40 发车，失接；
    # 保联要求 T2 发车 ≥ 08:50 + 10 = 09:00，额外晚点恰好 20 分钟
    timetable = basic_timetable()
    result = protect(timetable, ["T1", "T2"], {"T1": 20}, 10, 20, [BASIC_CONN])
    assert result["selected_indices"] == [0]
    assert result["protected"] == [0]
    assert result["protected_people"] == 100
    assert result["extra_delay_minutes"] == 20
    assert adjusted(result, "T1") == [(None, dt("08:20")), (dt("08:50"), None)]
    assert adjusted(result, "T2") == [(None, dt("09:00")), (dt("09:30"), None)]
    assert extra_delay(result, "T1") == 0   # 相对基准排程无新增
    assert extra_delay(result, "T2") == 20
    # 末到延误仍按原时刻表计算
    assert result["trains"][0]["final_delay_minutes"] == 20
    assert result["trains"][1]["final_delay_minutes"] == 20


def test_budget_insufficient_selects_none():
    # 预算 19 < 所需 20：唯一候选不可行，退回空集（不保护任何组）
    timetable = basic_timetable()
    result = protect(timetable, ["T1", "T2"], {"T1": 20}, 10, 19, [BASIC_CONN])
    assert result["selected_indices"] == []
    assert result["protected"] == []
    assert result["protected_people"] == 0
    assert result["extra_delay_minutes"] == 0
    # 排程退化为基准排程
    base = plan_delay_propagation(timetable, ["T1", "T2"], {"T1": 20}, 10)
    assert adjusted(result, "T2") == [
        (None, dt("08:40")), (dt("09:10"), None),
    ] == [
        (s["arrive"], s["depart"]) for s in base["trains"][1]["stops"]
    ]


def test_zero_budget_forces_empty_selection():
    timetable = basic_timetable()
    result = protect(timetable, ["T1", "T2"], {"T1": 20}, 10, 0, [BASIC_CONN])
    assert result["selected_indices"] == []
    assert result["protected_people"] == 0
    assert result["extra_delay_minutes"] == 0


def test_result_trains_follow_timetable_order():
    timetable = basic_timetable()
    result = protect(timetable, ["T2", "T1"], {"T1": 20}, 10, 20, [BASIC_CONN])
    assert [item["train_id"] for item in result["trains"]] == ["T1", "T2"]
    assert result["total_stops"] == 4


def test_baseline_includes_section_clearance():
    # 基准失接由区间清空传播造成：T3 延误 40 经 A～B 区间清空推迟 T1，
    # 使 T1→T2 在 B 站失接（原表 09:05 ≥ 08:50+10 可接续）
    timetable = tt([
        train("T3", [
            ("C", None, hm("08:00")),
            ("A", hm("08:20"), hm("08:25")),
            ("B", hm("08:45"), None),
        ]),
        train("T1", [("A", None, hm("08:30")), ("B", hm("08:50"), None)]),
        train("T2", [("B", None, hm("09:05")), ("D", hm("09:30"), None)]),
    ])
    # 基准：T3 09:05 离 A、09:25 到 B；T1 ≥ 09:35 发、09:55 到；T2 09:05 < 09:55+10 失接
    base = plan_delay_propagation(timetable, ["T1", "T2", "T3"], {"T3": 40}, 10)
    assert [t["final_delay_minutes"] for t in base["trains"]] == [40, 65, 0]
    conn = {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0,
            "people": 30}
    result = protect(timetable, ["T1", "T2", "T3"], {"T3": 40}, 10, 100, [conn])
    assert result["selected_indices"] == [0]
    assert result["protected"] == [0]
    # T2 发车 ≥ 09:55 + 10 = 10:05，额外晚点 60 分钟
    assert adjusted(result, "T2") == [(None, dt("10:05")), (dt("10:30"), None)]
    assert result["extra_delay_minutes"] == 60
    assert extra_delay(result, "T2") == 60
    assert extra_delay(result, "T1") == 0


# ---------- 目标字典序 ----------

def two_pair_timetable():
    """两对独立换乘：T1→T2@B 与 T3→T4@D，互不传播。"""
    return tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
        train("T3", [("E", None, hm("08:00")), ("D", hm("08:30"), None)]),
        train("T4", [("D", None, hm("08:40")), ("F", hm("09:10"), None)]),
    ])


def test_maximize_people_first():
    # 预算 20 只够一组：组0 费用 20（50 人），组1 费用 15（100 人）——先比人数
    timetable = two_pair_timetable()
    conns = [
        {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 50},
        {"from_train": "T3", "from_stop": 1, "to_train": "T4", "to_stop": 0, "people": 100},
    ]
    result = protect(timetable, ["T1", "T2", "T3", "T4"],
                     {"T1": 20, "T3": 15}, 0, 20, conns)
    assert result["selected_indices"] == [1]
    assert result["protected"] == [1]
    assert result["protected_people"] == 100
    assert result["extra_delay_minutes"] == 15


def test_minimize_extra_delay_second():
    # 人数相同（各 100），预算只够一组：选额外晚点更小的组1（15 < 20）
    timetable = two_pair_timetable()
    conns = [
        {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 100},
        {"from_train": "T3", "from_stop": 1, "to_train": "T4", "to_stop": 0, "people": 100},
    ]
    result = protect(timetable, ["T1", "T2", "T3", "T4"],
                     {"T1": 20, "T3": 15}, 0, 20, conns)
    assert result["selected_indices"] == [1]
    assert result["protected_people"] == 100
    assert result["extra_delay_minutes"] == 15


def test_lexicographic_last():
    # 人数、费用全相同：取输入序号列表字典序最小者 [0]
    timetable = two_pair_timetable()
    conns = [
        {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 100},
        {"from_train": "T3", "from_stop": 1, "to_train": "T4", "to_stop": 0, "people": 100},
    ]
    result = protect(timetable, ["T1", "T2", "T3", "T4"],
                     {"T1": 20, "T3": 20}, 0, 20, conns)
    assert result["selected_indices"] == [0]
    assert result["protected_people"] == 100
    assert result["extra_delay_minutes"] == 20


def test_fewest_groups_before_lexicographic():
    # 组1（T1→T2，基准到 08:50）的约束更紧，选中后组0（T3→T2，基准到 08:45）
    # 免费接上：{1} 与 {0,1} 同为 200 人、20 分钟额外晚点，先比组数取 {1}
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T3", [("D", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
    ])
    conns = [
        {"from_train": "T3", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 100},
        {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 100},
    ]
    result = protect(timetable, ["T1", "T2", "T3"], {"T1": 20, "T3": 15}, 0, 100, conns)
    assert result["selected_indices"] == [1]
    assert result["protected"] == [0, 1]  # 组0 未选中但随排程接上
    assert result["protected_people"] == 200
    assert result["extra_delay_minutes"] == 20


def test_selected_but_not_protected():
    # 选中但未接上：组0（T1→T2@B）的约束按基准时刻计算；组1（T4→T5@A）的约束
    # 经区间清空进一步推迟 T1，使组0 最终仍失接。最优解 {0,1}：
    # 组1 接上（100 人）、组2（T3→T2@B）随组0 的约束免费接上（20 人）、组0 未接上
    timetable = tt([
        train("T4", [("E", None, hm("08:00")), ("A", hm("08:30"), None)]),
        train("T5", [("A", None, hm("08:40")), ("B", hm("09:10"), None)]),
        train("T1", [("A", None, hm("08:50")), ("B", hm("09:20"), None)]),
        train("T3", [("D", None, hm("08:00")), ("B", hm("09:30"), None)]),
        train("T2", [("B", None, hm("09:55")), ("C", hm("10:25"), None)]),
    ])
    ids = ["T1", "T2", "T3", "T4", "T5"]
    delays = {"T4": 25, "T3": 20}
    conns = [
        {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 10},
        {"from_train": "T4", "from_stop": 1, "to_train": "T5", "to_stop": 0, "people": 100},
        {"from_train": "T3", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 20},
    ]
    result = protect(timetable, ids, delays, 10, 10000, conns)
    assert result["selected_indices"] == [0, 1]
    assert result["protected"] == [1, 2]  # 组0 选中但未接上
    assert result["protected_people"] == 120
    # 额外晚点 = T5 25 + T1 25 + T2 5 = 55
    assert result["extra_delay_minutes"] == 55
    assert extra_delay(result, "T5") == 25
    assert extra_delay(result, "T1") == 25
    assert extra_delay(result, "T2") == 5
    assert extra_delay(result, "T4") == 0
    assert extra_delay(result, "T3") == 0


def test_eight_groups_enumeration():
    # 8 组独立换乘，每组保联费用 20，预算 40 至多接 2 组：选人数最多的两组
    stations = []
    trains = []
    conns = []
    delays = {}
    people = [5, 10, 15, 20, 25, 30, 35, 40]
    for i in range(8):
        p, q, r = f"P{i}", f"Q{i}", f"R{i}"
        stations += [
            {"id": p, "transfer_minutes": 0},
            {"id": q, "transfer_minutes": 10},
            {"id": r, "transfer_minutes": 0},
        ]
        trains.append(train(f"F{i}", [(p, None, hm("08:00")), (q, hm("08:30"), None)]))
        trains.append(train(f"G{i}", [(q, None, hm("08:40")), (r, hm("09:10"), None)]))
        delays[f"F{i}"] = 20
        conns.append({"from_train": f"F{i}", "from_stop": 1,
                      "to_train": f"G{i}", "to_stop": 0, "people": people[i]})
    timetable = tt(trains, stations)
    ids = [t["id"] for t in trains]
    result = protect(timetable, ids, delays, 0, 40, conns)
    assert result["selected_indices"] == [6, 7]
    assert result["protected"] == [6, 7]
    assert result["protected_people"] == 75
    assert result["extra_delay_minutes"] == 40


# ---------- 输入校验 ----------

def expect_error(match, timetable=None, ids=None, delays=None, clearance=0,
                 budget=100, connections=None):
    timetable = timetable or basic_timetable()
    ids = ids if ids is not None else ["T1", "T2"]
    delays = delays if delays is not None else {"T1": 20}
    connections = connections if connections is not None else [BASIC_CONN]
    with pytest.raises(TimetableError, match=match):
        protect(timetable, ids, delays, clearance, budget, connections)


@pytest.mark.parametrize("budget", [-1, 10001, 2.5, "10", True, None])
def test_bad_budget_rejected(budget):
    expect_error("额外晚点预算", budget=budget)


def test_connections_must_be_list():
    expect_error("connections 必须是数组", connections="x")


@pytest.mark.parametrize("count", [0, 9])
def test_connections_count_limit(count):
    expect_error("1～8", connections=[dict(BASIC_CONN) for _ in range(count)])


def test_connection_must_be_object():
    expect_error("必须是对象", connections=["x"])


def test_connection_trains_must_be_selected():
    expect_error("不是所选班次", connections=[dict(BASIC_CONN, from_train="T9")])
    expect_error("不是所选班次", connections=[dict(BASIC_CONN, to_train="T9")])
    expect_error("不是所选班次", connections=[dict(BASIC_CONN, from_train=None)])


def test_connection_same_train_rejected():
    expect_error("前后班次不能相同", connections=[dict(BASIC_CONN, to_train="T1")])


@pytest.mark.parametrize("field,value", [
    ("from_stop", -1), ("from_stop", 2), ("from_stop", "1"), ("from_stop", True),
    ("to_stop", -1), ("to_stop", 2), ("to_stop", None),
])
def test_connection_stop_index_rejected(field, value):
    expect_error("停靠序号范围", connections=[dict(BASIC_CONN, **{field: value})])


def test_connection_must_share_station():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("C", None, hm("08:40")), ("D", hm("09:10"), None)]),
    ])
    expect_error("同一站点", timetable=timetable,
                 connections=[{"from_train": "T1", "from_stop": 1,
                               "to_train": "T2", "to_stop": 0, "people": 1}])


def test_connection_feeder_needs_arrive():
    # from_stop 指向前车首站（无到达时间）
    timetable = tt([
        train("T1", [("B", None, hm("08:00")), ("C", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("D", hm("09:10"), None)]),
    ])
    expect_error("没有到达时间", timetable=timetable, delays={},
                 connections=[{"from_train": "T1", "from_stop": 0,
                               "to_train": "T2", "to_stop": 0, "people": 1}])


def test_connection_follower_needs_depart():
    # to_stop 指向后车末站（无发车时间）
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("C", None, hm("08:40")), ("B", hm("09:10"), None)]),
    ])
    expect_error("没有发车时间", timetable=timetable, delays={},
                 connections=[{"from_train": "T1", "from_stop": 1,
                               "to_train": "T2", "to_stop": 1, "people": 1}])


def test_connection_feeder_must_arrive_before_follower_departs():
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:30")), ("C", hm("09:00"), None)]),
    ])
    expect_error("前车原到达须早于后车原发车", timetable=timetable,
                 delays={"T1": 20})


def test_connection_original_must_satisfy_transfer():
    # 原表 08:35 < 08:30 + 10：不满足本站换乘时间
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:35")), ("C", hm("09:05"), None)]),
    ])
    expect_error("原时刻表不满足本站换乘时间", timetable=timetable)


def test_connection_baseline_must_be_missed():
    # T1 不晚点：基准下 08:40 ≥ 08:30 + 10，未失接
    expect_error("基准排程下该换乘未失接", delays={"T1": 0})
    expect_error("基准排程下该换乘未失接", delays={})


@pytest.mark.parametrize("people", [0, 1001, -5, 2.5, "5", True, None])
def test_connection_people_rejected(people):
    expect_error("人数", connections=[dict(BASIC_CONN, people=people)])


def test_connection_duplicate_rejected():
    expect_error("重复", connections=[dict(BASIC_CONN), dict(BASIC_CONN)])


def test_connection_same_trains_different_stops_allowed():
    # 同一对班次、不同停靠序号不算重复组合
    timetable = tt([
        train("T1", [
            ("A", None, hm("08:00")),
            ("B", hm("08:30"), hm("08:35")),
            ("C", hm("09:00"), None),
        ]),
        train("T2", [
            ("B", None, hm("08:45")),
            ("D", hm("09:10"), hm("09:15")),
            ("C", hm("09:40"), hm("09:45")),
            ("E", hm("10:10"), None),
        ]),
    ])
    conns = [
        {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0, "people": 10},
        {"from_train": "T1", "from_stop": 2, "to_train": "T2", "to_stop": 2, "people": 20},
    ]
    # T1 延误 40：基准到 B 09:10（T2 08:45 已发，失接）、到 C 09:40（T2 09:45 发，失接）；
    # 组0 的约束把 T2 离 B 推迟到 09:20，传播后 T2 离 C 10:20，组1 随之免费接上
    result = protect(timetable, ["T1", "T2"], {"T1": 40}, 0, 100, conns)
    assert result["selected_indices"] == [0]
    assert result["protected"] == [0, 1]
    assert result["protected_people"] == 30


def test_delay_plan_validation_reused():
    # 沿用延误排程的输入校验：班次、首发延误、清空间隔
    expect_error("train_ids 必须是数组", ids="T1")
    expect_error("1～40", ids=[])
    expect_error("重复", ids=["T1", "T1"])
    expect_error("不存在", ids=["T1", "ZZZ"])
    expect_error("首发延误", delays={"T1": 121})
    expect_error("不是所选班次", delays={"T3": 5})
    expect_error("清空间隔", clearance=31)


# ---------- API ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SAMPLE_TIMETABLE", "")
    application = create_app()
    application.config["TESTING"] = True
    with application.test_client() as client:
        yield client


def api_payload():
    return {
        "stations": [
            {"id": "A", "transfer_minutes": 10},
            {"id": "B", "transfer_minutes": 10},
            {"id": "C", "transfer_minutes": 0},
        ],
        "trains": [
            train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
            train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
        ],
    }


def import_api_sample(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]


def api_request(**overrides):
    payload = {
        "train_ids": ["T1", "T2"],
        "initial_delays": {"T1": 20},
        "clearance_minutes": 10,
        "max_extra_delay_minutes": 20,
        "connections": [
            {"from_train": "T1", "from_stop": 1, "to_train": "T2", "to_stop": 0,
             "people": 100},
        ],
    }
    payload.update(overrides)
    return payload


def test_api_connection_protection_ok(client):
    import_api_sample(client)
    res = client.post("/api/connection-protection", json=api_request())
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"]
    assert data["clearance_minutes"] == 10
    assert data["max_extra_delay_minutes"] == 20
    assert data["selected_indices"] == [0]
    assert data["protected"] == [0]
    assert data["protected_people"] == 100
    assert data["extra_delay_minutes"] == 20
    assert data["train_count"] == 2
    assert data["total_stops"] == 4
    assert [t["train_id"] for t in data["trains"]] == ["T1", "T2"]
    t1, t2 = data["trains"]
    assert t1["extra_delay_minutes"] == 0
    assert t1["final_delay_minutes"] == 20
    assert t2["extra_delay_minutes"] == 20
    assert [(s["arrive"], s["depart"]) for s in t2["stops"]] == [
        (None, "2026-10-01T09:00"),
        ("2026-10-01T09:30", None),
    ]
    assert t2["stops"][0]["original_depart"] == "2026-10-01T08:40"


def test_api_connection_protection_without_timetable(client):
    res = client.post("/api/connection-protection", json=api_request())
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]


def test_api_connection_protection_validation(client):
    import_api_sample(client)
    # 非 JSON 请求体
    res = client.post("/api/connection-protection", data="x", content_type="text/plain")
    assert res.status_code == 400
    # 预算越界 / 类型错误
    for bad_budget in (-1, 10001, "10", 2.5, True, None):
        res = client.post("/api/connection-protection",
                          json=api_request(max_extra_delay_minutes=bad_budget))
        assert res.status_code == 400
    # 换乘组非法
    bad_connections = [
        [],
        [dict(BASIC_CONN)] * 9,
        [dict(BASIC_CONN, people=0)],
        [dict(BASIC_CONN, to_train="T1")],
        [dict(BASIC_CONN, from_stop=9)],
        [dict(BASIC_CONN), dict(BASIC_CONN)],
        "x",
    ]
    for bad in bad_connections:
        res = client.post("/api/connection-protection",
                          json=api_request(connections=bad))
        assert res.status_code == 400
    # 基准未失接（无首发延误）
    res = client.post("/api/connection-protection",
                      json=api_request(initial_delays={}))
    assert res.status_code == 400
    # 沿用延误排程校验
    res = client.post("/api/connection-protection",
                      json=api_request(train_ids=["T1", "T1"]))
    assert res.status_code == 400
    res = client.post("/api/connection-protection",
                      json=api_request(clearance_minutes=31))
    assert res.status_code == 400


def test_api_connection_protection_does_not_modify_timetable(client):
    import_api_sample(client)
    before = client.get("/api/timetable").get_json()
    versions_before = client.get("/api/timetable-versions").get_json()
    res = client.post("/api/connection-protection", json=api_request())
    assert res.get_json()["ok"]
    assert client.get("/api/timetable").get_json() == before
    assert client.get("/api/timetable-versions").get_json() == versions_before
