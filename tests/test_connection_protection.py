"""换乘保联决策：保联约束传播、额外晚点预算、字典序择优、输入校验与 API。"""
import pytest

from app import create_app
from planner import (
    TimetableError,
    plan_connection_protection,
    validate_timetable,
)

DAY = "2026-10-01"

STATIONS = [
    {"id": "A", "transfer_minutes": 0},
    {"id": "B", "transfer_minutes": 5},
    {"id": "C", "transfer_minutes": 0},
    {"id": "D", "transfer_minutes": 0},
]


def hm(value, day=DAY):
    return f"{day}T{value}"


def train(tid, stops_spec):
    """stops_spec：[(站点, 到达或 None, 发车或 None), ...]，时间为完整 ISO 字符串或 None。"""
    return {"id": tid, "stops": [
        {"station": sid, "arrive": arrive, "depart": depart}
        for sid, arrive, depart in stops_spec
    ]}


def tt(trains):
    return validate_timetable({"stations": STATIONS, "trains": trains})


def conn(from_train, from_stop, to_train, to_stop, people=10):
    return {
        "from_train": from_train,
        "from_stop_index": from_stop,
        "to_train": to_train,
        "to_stop_index": to_stop,
        "people": people,
    }


def decide(timetable, ids, delays, clearance, budget, connections):
    return plan_connection_protection(
        timetable, ids, delays, clearance, budget, connections
    )


def basic_timetable():
    """T1：A 08:00 → B 08:30（终到）；T2：B 08:40 → C 09:10。B 站换乘 5 分钟。"""
    return tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
    ])


# ---------- 基本保联与预算 ----------

def stop_times(result, tid):
    """结果中指定班次的 [(到达字符串或 None, 发车字符串或 None), ...]。"""
    for item in result["trains"]:
        if item["train_id"] == tid:
            return [
                (
                    s["arrive"].strftime("%Y-%m-%dT%H:%M") if s["arrive"] else None,
                    s["depart"].strftime("%Y-%m-%dT%H:%M") if s["depart"] else None,
                )
                for s in item["stops"]
            ]
    raise AssertionError(f"结果中没有班次 {tid}")


def test_protect_single_connection():
    # T1 延误 15 分钟，08:45 到 B；T2 原 08:40 发，基准失接（须 ≥ 08:50）
    result = decide(
        basic_timetable(), ["T1", "T2"], {"T1": 15}, 10, 10,
        [conn("T1", 1, "T2", 0, 30)],
    )
    assert result["selected"] == [0]
    assert result["connected"] == [0]
    assert result["protected_passengers"] == 30
    # T2 推迟到 08:50 发、09:20 到：末到较基准新增 10 分钟
    assert result["extra_delay_minutes"] == 10
    assert stop_times(result, "T2") == [
        (None, "2026-10-01T08:50"),
        ("2026-10-01T09:20", None),
    ]
    t2 = next(t for t in result["trains"] if t["train_id"] == "T2")
    assert t2["final_delay_minutes"] == 10


def test_budget_insufficient_protects_nothing():
    # 保护需新增 10 分钟晚点，预算只有 9：空选择，维持基准排程
    result = decide(
        basic_timetable(), ["T1", "T2"], {"T1": 15}, 10, 9,
        [conn("T1", 1, "T2", 0, 30)],
    )
    assert result["selected"] == []
    assert result["connected"] == []
    assert result["protected_passengers"] == 0
    assert result["extra_delay_minutes"] == 0
    t2 = next(t for t in result["trains"] if t["train_id"] == "T2")
    assert t2["final_delay_minutes"] == 0  # 基准排程下 T2 正点


def test_zero_budget_allows_empty_choice_only():
    result = decide(
        basic_timetable(), ["T1", "T2"], {"T1": 15}, 10, 0,
        [conn("T1", 1, "T2", 0, 30)],
    )
    assert result["selected"] == []
    assert result["extra_delay_minutes"] == 0


# ---------- 字典序择优：旅客总数 → 额外晚点 → 选中组数 → 序号 ----------

def two_candidate_timetable():
    """T1 到 B 后，T2（B→C）与 T3（B→D）都可接：保护成本分别为 10、8 分钟。"""
    return tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
        train("T3", [("B", None, hm("08:42")), ("D", hm("09:12"), None)]),
    ])


def test_passenger_count_dominates():
    # 预算 10 只够保一组：组 0 成本 10（10 人），组 1 成本 8（20 人）→ 选人数多的组 1
    result = decide(
        two_candidate_timetable(), ["T1", "T2", "T3"], {"T1": 15}, 10, 10,
        [conn("T1", 1, "T2", 0, 10), conn("T1", 1, "T3", 0, 20)],
    )
    assert result["selected"] == [1]
    assert result["connected"] == [1]
    assert result["protected_passengers"] == 20
    assert result["extra_delay_minutes"] == 8


def test_protect_both_when_budget_allows():
    # 预算 20 ≥ 10 + 8：两组都保，30 人全部接上
    result = decide(
        two_candidate_timetable(), ["T1", "T2", "T3"], {"T1": 15}, 10, 20,
        [conn("T1", 1, "T2", 0, 10), conn("T1", 1, "T3", 0, 20)],
    )
    assert result["selected"] == [0, 1]
    assert result["connected"] == [0, 1]
    assert result["protected_passengers"] == 30
    assert result["extra_delay_minutes"] == 18


def test_extra_delay_tiebreak():
    # 两组人数相同、预算只够一组：选额外晚点更小的组 1（8 < 10）
    result = decide(
        two_candidate_timetable(), ["T1", "T2", "T3"], {"T1": 15}, 10, 10,
        [conn("T1", 1, "T2", 0, 10), conn("T1", 1, "T3", 0, 10)],
    )
    assert result["selected"] == [1]
    assert result["protected_passengers"] == 10
    assert result["extra_delay_minutes"] == 8


def shared_train_timetable():
    """T1（A→B）与 T3（D→B）基准都 08:45 到 B，都要换 T2（B→C）。"""
    return tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T3", [("D", None, hm("08:05")), ("B", hm("08:35"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
    ])


def test_unselected_group_also_connected_and_lexicographic():
    # 保组 0 使 T2 推迟到 08:50，组 1（T3 08:45 到 + 5 分钟）恰好也接上；
    # 保组 1 同理。两者人数、晚点、选中数都相同，取序号字典序最小的 [0]
    result = decide(
        shared_train_timetable(), ["T1", "T3", "T2"], {"T1": 15, "T3": 10}, 10, 10,
        [conn("T1", 1, "T2", 0, 5), conn("T3", 1, "T2", 0, 5)],
    )
    assert result["selected"] == [0]
    assert result["connected"] == [0, 1]  # 组 1 未选中但也接上，人数计入
    assert result["protected_passengers"] == 10
    assert result["extra_delay_minutes"] == 10


def test_fewest_selected_tiebreak():
    # 选 {0} 已让两组都接上：与选 {0,1} 人数、晚点相同，选中组数更少者胜
    result = decide(
        shared_train_timetable(), ["T1", "T3", "T2"], {"T1": 15, "T3": 10}, 10, 100,
        [conn("T1", 1, "T2", 0, 5), conn("T3", 1, "T2", 0, 5)],
    )
    assert result["selected"] == [0]
    assert result["connected"] == [0, 1]
    assert result["extra_delay_minutes"] == 10


# ---------- 保联约束沿固定区间顺序传播 ----------

def test_protection_propagates_through_section():
    # T2 被保联推迟后占用 B～C 区间，T3 被迫再等清空：额外晚点含被带动的 T3
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
        train("T3", [("B", None, hm("09:00")), ("C", hm("09:30"), None)]),
    ])
    # 基准：T1 延误 20 → 08:50 到 B；T3 已受区间清空约束 09:20 发、09:50 到。
    # 保联：T2 须 ≥ 08:55 发、09:25 到（较基准 +15）；
    # T3 须等 T2 到 C + 10 分钟清空 → 09:35 发、10:05 到（较基准再 +15）
    result = decide(
        timetable, ["T1", "T2", "T3"], {"T1": 20}, 10, 30,
        [conn("T1", 1, "T2", 0, 7)],
    )
    assert result["selected"] == [0]
    assert result["protected_passengers"] == 7
    assert result["extra_delay_minutes"] == 15 + 15  # T2、T3 末到各较基准新增 15
    finals = {t["train_id"]: t["final_delay_minutes"] for t in result["trains"]}
    assert finals == {"T1": 20, "T2": 15, "T3": 35}
    # 预算 29 < 30：保不起，维持基准
    result = decide(
        timetable, ["T1", "T2", "T3"], {"T1": 20}, 10, 29,
        [conn("T1", 1, "T2", 0, 7)],
    )
    assert result["selected"] == []
    assert result["extra_delay_minutes"] == 0


def test_result_trains_follow_timetable_order():
    result = decide(
        basic_timetable(), ["T2", "T1"], {"T1": 15}, 10, 10,
        [conn("T1", 1, "T2", 0, 30)],
    )
    assert [t["train_id"] for t in result["trains"]] == ["T1", "T2"]
    assert result["total_stops"] == 4


# ---------- 输入校验 ----------

def base_args():
    return (basic_timetable(), ["T1", "T2"], {"T1": 15}, 10, 10)


def test_connections_must_be_list():
    timetable, ids, delays, clearance, budget = base_args()
    with pytest.raises(TimetableError, match="connections 必须是数组"):
        decide(timetable, ids, delays, clearance, budget, "x")


@pytest.mark.parametrize("count", [0, 9])
def test_connections_count_limit(count):
    timetable, ids, delays, clearance, budget = base_args()
    connections = [conn("T1", 1, "T2", 0, 10) for _ in range(count)]
    # 9 组时必有重复组合，但数量校验在前
    with pytest.raises(TimetableError, match="1～8"):
        decide(timetable, ids, delays, clearance, budget, connections)


@pytest.mark.parametrize("budget", [-1, 10001, 2.5, "60", True, None])
def test_bad_budget_rejected(budget):
    timetable, ids, delays, clearance, _ = base_args()
    with pytest.raises(TimetableError, match="额外晚点预算"):
        decide(timetable, ids, delays, clearance, budget, [conn("T1", 1, "T2", 0)])


def test_schedule_params_reused_and_validated():
    timetable = basic_timetable()
    with pytest.raises(TimetableError, match="train_ids 必须是数组"):
        decide(timetable, "T1", None, 10, 10, [conn("T1", 1, "T2", 0)])
    with pytest.raises(TimetableError, match="清空间隔"):
        decide(timetable, ["T1", "T2"], None, 31, 10, [conn("T1", 1, "T2", 0)])
    with pytest.raises(TimetableError, match="不存在"):
        decide(timetable, ["T1", "ZZ"], None, 10, 10, [conn("T1", 1, "T2", 0)])


@pytest.mark.parametrize("field,value", [
    ("from_train", "ZZ"),      # 前车不在所选班次
    ("from_train", "T2"),      # 前后同班（T2→T2）
    ("to_train", "ZZ"),        # 后车不在所选班次
])
def test_bad_trains_rejected(field, value):
    timetable, ids, delays, clearance, budget = base_args()
    c = conn("T1", 1, "T2", 0)
    c[field] = value
    if field == "from_train" and value == "T2":
        c["to_train"] = "T2"
    with pytest.raises(TimetableError):
        decide(timetable, ids, delays, clearance, budget, [c])


def test_same_train_rejected():
    timetable, ids, delays, clearance, budget = base_args()
    with pytest.raises(TimetableError, match="前后班次必须不同"):
        decide(timetable, ids, delays, clearance, budget, [conn("T1", 1, "T1", 0)])


@pytest.mark.parametrize("field,value", [
    ("from_stop_index", -1),
    ("from_stop_index", 2),    # T1 只有 2 个停靠
    ("from_stop_index", 0),    # 首站没有到达时间
    ("from_stop_index", True),
    ("to_stop_index", -1),
    ("to_stop_index", 2),      # T2 只有 2 个停靠
    ("to_stop_index", 1),      # 末站没有发车时间
    ("to_stop_index", "0"),
])
def test_bad_stop_index_rejected(field, value):
    timetable, ids, delays, clearance, budget = base_args()
    c = conn("T1", 1, "T2", 0)
    c[field] = value
    with pytest.raises(TimetableError):
        decide(timetable, ids, delays, clearance, budget, [c])


def test_different_stations_rejected():
    # T1 在 B 到达，T2 的停靠 #1 在 C：换乘须同站
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [
            ("B", None, hm("08:40")),
            ("C", hm("09:10"), hm("09:15")),
            ("D", hm("09:40"), None),
        ]),
    ])
    with pytest.raises(TimetableError, match="同站"):
        decide(timetable, ["T1", "T2"], {"T1": 90}, 10, 500,
               [conn("T1", 1, "T2", 1)])  # T1 到 B，T2 停靠 #1 在 C


def test_original_table_must_satisfy_transfer():
    # T2 原 08:33 发 < T1 原到 08:30 + 换乘 5：原表就不满足换乘时间
    timetable = tt([
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:33")), ("C", hm("09:03"), None)]),
    ])
    with pytest.raises(TimetableError, match="换乘时间"):
        decide(timetable, ["T1", "T2"], {"T1": 15}, 10, 100,
               [conn("T1", 1, "T2", 0)])


def test_original_arrive_must_precede_depart():
    # 换乘 0 分钟且原到发同刻：满足换乘但不满足"原到达早于原发车"
    stations = [
        {"id": "A", "transfer_minutes": 0},
        {"id": "B", "transfer_minutes": 0},
        {"id": "C", "transfer_minutes": 0},
    ]
    timetable = validate_timetable({"stations": stations, "trains": [
        train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
        train("T2", [("B", None, hm("08:30")), ("C", hm("09:00"), None)]),
    ]})
    with pytest.raises(TimetableError, match="早于"):
        decide(timetable, ["T1", "T2"], {"T1": 15}, 10, 100,
               [conn("T1", 1, "T2", 0)])


def test_baseline_connected_rejected():
    # 无延误时基准排程能接上（08:40 ≥ 08:30+5）：无需保护
    timetable, ids, _, clearance, budget = base_args()
    with pytest.raises(TimetableError, match="并未失接"):
        decide(timetable, ids, None, clearance, budget, [conn("T1", 1, "T2", 0)])


@pytest.mark.parametrize("people", [0, 1001, -5, 2.5, "10", True, None])
def test_bad_people_rejected(people):
    timetable, ids, delays, clearance, budget = base_args()
    with pytest.raises(TimetableError, match="人数"):
        decide(timetable, ids, delays, clearance, budget,
               [conn("T1", 1, "T2", 0, people)])


def test_duplicate_connection_rejected():
    timetable, ids, delays, clearance, budget = base_args()
    with pytest.raises(TimetableError, match="重复"):
        decide(timetable, ids, delays, clearance, budget,
               [conn("T1", 1, "T2", 0, 10), conn("T1", 1, "T2", 0, 20)])


def test_non_dict_connection_rejected():
    timetable, ids, delays, clearance, budget = base_args()
    with pytest.raises(TimetableError, match="必须是对象"):
        decide(timetable, ids, delays, clearance, budget, [["T1", 1, "T2", 0, 10]])


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
        "stations": STATIONS,
        "trains": [
            train("T1", [("A", None, hm("08:00")), ("B", hm("08:30"), None)]),
            train("T2", [("B", None, hm("08:40")), ("C", hm("09:10"), None)]),
            train("T3", [("B", None, hm("08:42")), ("D", hm("09:12"), None)]),
        ],
    }


def import_api_sample(client):
    assert client.post("/api/import", json=api_payload()).get_json()["ok"]


def protection_payload(budget=10, connections=None):
    return {
        "train_ids": ["T1", "T2", "T3"],
        "initial_delays": {"T1": 15},
        "clearance_minutes": 10,
        "max_extra_delay_minutes": budget,
        "connections": connections if connections is not None else [
            conn("T1", 1, "T2", 0, 10),
            conn("T1", 1, "T3", 0, 20),
        ],
    }


def test_api_connection_protection_ok(client):
    import_api_sample(client)
    res = client.post("/api/connection-protection", json=protection_payload())
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"]
    assert data["clearance_minutes"] == 10
    assert data["max_extra_delay_minutes"] == 10
    assert data["selected"] == [1]
    assert data["connected"] == [1]
    assert data["protected_passengers"] == 20
    assert data["extra_delay_minutes"] == 8
    assert data["train_count"] == 3
    assert data["total_stops"] == 6
    assert [t["train_id"] for t in data["trains"]] == ["T1", "T2", "T3"]
    t3 = data["trains"][2]
    assert t3["final_delay_minutes"] == 8
    assert t3["stops"] == [
        {"station": "B", "arrive": None, "depart": "2026-10-01T08:50",
         "original_arrive": None, "original_depart": "2026-10-01T08:42"},
        {"station": "D", "arrive": "2026-10-01T09:20", "depart": None,
         "original_arrive": "2026-10-01T09:12", "original_depart": None},
    ]


def test_api_connection_protection_without_timetable(client):
    res = client.post("/api/connection-protection", json=protection_payload())
    assert res.status_code == 400
    assert "尚未导入" in res.get_json()["error"]


@pytest.mark.parametrize("body", [
    None,
    {"train_ids": ["T1"]},  # 缺预算与旅客组
])
def test_api_connection_protection_bad_body(client, body):
    import_api_sample(client)
    if body is None:
        res = client.post("/api/connection-protection", data="x",
                          content_type="text/plain")
    else:
        res = client.post("/api/connection-protection", json=body)
    assert res.status_code == 400
    assert not res.get_json()["ok"]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(max_extra_delay_minutes=10001),
    lambda p: p.update(connections=[]),
    lambda p: p["connections"][0].update(people=0),
    lambda p: p["connections"][0].update(from_train="T2"),  # 前后同班（T2→T2）
    lambda p: p.update(initial_delays={}),                  # 基准不失接
])
def test_api_connection_protection_invalid_inputs(client, mutate):
    import_api_sample(client)
    payload = protection_payload()
    mutate(payload)
    res = client.post("/api/connection-protection", json=payload)
    assert res.status_code == 400
    assert not res.get_json()["ok"]


def test_api_connection_protection_does_not_modify_timetable(client):
    import_api_sample(client)
    before = client.get("/api/timetable").get_json()
    versions_before = client.get("/api/timetable-versions").get_json()
    res = client.post("/api/connection-protection", json=protection_payload())
    assert res.get_json()["ok"]
    assert client.get("/api/timetable").get_json() == before
    assert client.get("/api/timetable-versions").get_json() == versions_before
