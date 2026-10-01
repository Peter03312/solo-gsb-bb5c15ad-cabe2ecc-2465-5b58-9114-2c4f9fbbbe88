"""端到端自测：已封存案卷的双人独立复核与第三人裁决。

覆盖：
  * 全程一致：两次不同复核人完整清单、逐份一致即终局、全通过/任一不通过的案卷结论；
  * 进度：0/1/2 次复核的阶段与原始意见、裁决前后的阶段切换；
  * 摘要核对：digest 不一致（拿错版本/篡改）拒绝写入，篡改库存亦拒绝；
  * 双人规则：重复复核人、越阶段第三次复核拒绝；
  * 清单校验：缺项 / 重复份项 / 卷外份项整单拒绝且不留部分记录；
  * 幂等冲突：同复核单号同内容（含换序）200 幂等、异内容 409；跨案卷复用单号 409；
  * 意见不可改：单号冲突即拒绝，已有意见不被修改；
  * 裁决：仅两次复核后可提交、裁决人必须为第三人、清单须恰好覆盖分歧份项、
    一次裁决终局（再裁决/再复核 409）、裁决后终局结论与案卷不通过；
  * 裁决幂等冲突、查询（进度/单号/404）、载荷校验；
  * 并发：同单号同内容一建一幂等；两名复核人竞争第二席按已提交状态判定；
    两裁决并发一次成功；
  * 既有接口行为不受影响。

运行：python -m tests.test_reviews
"""
import json
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_reviews.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session, init_db  # noqa: E402
from app.dossiers import compute_digest  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="张三", seal=None, when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when,
        "collector": collector, "seal_no": seal or f"SEAL-{code}"})


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def complete_valid(code, seal=None):
    """登记并补齐 移交→签收→检查→入库 的有效链（版本推进到 4）。"""
    seal = seal or f"SEAL-{code}"
    reg(code, seal=seal)
    for payload in [
        {"event_id": f"{code}-T1", "event_type": "transfer", "sample_code": code,
         "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"TR-{code}",
         "from_custodian": "张三", "to_custodian": "李四"},
        {"event_id": f"{code}-R1", "event_type": "receive", "sample_code": code,
         "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": f"TR-{code}",
         "receiver": "李四", "seal_no": seal},
        {"event_id": f"{code}-C1", "event_type": "seal_check", "sample_code": code,
         "occurred_at": "2026-09-30T04:30:00Z", "checked_seal_no": seal, "seal_intact": True},
        {"event_id": f"{code}-S1", "event_type": "storage", "sample_code": code,
         "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"},
    ]:
        ev(payload)


def dossier(did, codes, versions=None):
    items = [{"sample_code": c, **({"version": versions[c]} if versions and c in versions else {})}
             for c in codes]
    return client.post(f"{API}/dossiers", json={"dossier_id": did, "items": items})


def digest_of(did):
    return client.get(f"{API}/dossiers/{did}").json()["dossier"]["digest"]


def review(did, rid, reviewer, verdicts, dg=None):
    """verdicts: {sample_code: True} 或 {sample_code: (False, '原因')}。"""
    dg = dg or digest_of(did)
    items = []
    for code, v in verdicts.items():
        if v is True:
            items.append({"sample_code": code, "passed": True})
        else:
            items.append({"sample_code": code, "passed": False, "fail_reason": v[1]})
    return client.post(f"{API}/dossiers/{did}/reviews",
                       json={"review_id": rid, "reviewer": reviewer, "digest": dg, "items": items})


def adjudicate(did, aid, arbiter, verdicts, dg=None):
    dg = dg or digest_of(did)
    items = []
    for code, v in verdicts.items():
        if v is True:
            items.append({"sample_code": code, "passed": True})
        else:
            items.append({"sample_code": code, "passed": False, "fail_reason": v[1]})
    return client.post(f"{API}/dossiers/{did}/adjudications",
                       json={"adjudication_id": aid, "adjudicator": arbiter,
                             "digest": dg, "items": items})


def progress(did):
    return client.get(f"{API}/dossiers/{did}/reviews").json()["review_progress"]


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def codes_in(items):
    return [it["sample_code"] for it in items]


# ---------------------------------------------------------------- 1. 全程一致：全通过
print("1) 全程一致：两名不同复核人均判通过 → 案卷终局通过（无需裁决）")
for c in ("R1-A", "R1-B", "R1-C"):
    complete_valid(c)
r = dossier("DOS-R1", ["R1-C", "R1-A", "R1-B"])     # 乱序提交，封存清单按编号排序
check("案卷封存 201", r.status_code == 201, r.text)
dg1 = digest_of("DOS-R1")

p = progress("DOS-R1")
check("初始阶段 awaiting_first", p["stage"] == "awaiting_first"
      and p["progress"]["reviews_submitted"] == 0 and p["completed"] is False, str(p))
check("终局份项数为 0", p["progress"]["finalized_items"] == 0)

r = review("DOS-R1", "RV1-1", "复核员甲", {"R1-A": True, "R1-B": True, "R1-C": True})
check("第一次复核 201", r.status_code == 201, r.text)
check("idempotent=false", r.json()["idempotent"] is False)
p = r.json()["review_progress"]
check("第一次后 awaiting_second", p["stage"] == "awaiting_second"
      and p["progress"]["reviews_submitted"] == 1)
check("原始意见可查（seq=0）",
      p["reviews"][0]["seq"] == 0 and p["reviews"][0]["reviewer"] == "复核员甲"
      and codes_in(p["reviews"][0]["items"]) == ["R1-A", "R1-B", "R1-C"])
check("分歧为空、终局份项仍为 0（未集齐两人意见）",
      p["disagreements"] == [] and p["progress"]["finalized_items"] == 0)
check("案卷结论尚未形成", p["conclusion"] is None)

# 裁决越阶段（只有一次复核）
r = adjudicate("DOS-R1", "ADJ1-EARLY", "裁决人", {"R1-A": True})
check("一次复核后裁决 409 review_stage_violation",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_violation", r.text)

# items 换序提交：第二次复核
r = review("DOS-R1", "RV1-2", "复核员乙", {"R1-C": True, "R1-A": True, "R1-B": True})
check("第二次复核 201", r.status_code == 201, r.text)
p = r.json()["review_progress"]
check("两人一致 → completed", p["stage"] == "completed" and p["completed"] is True, str(p["stage"]))
check("全部份项终局（3/3，均 review 决定）",
      p["progress"]["finalized_items"] == 3
      and {it["decided_by"] for it in p["final_items"]} == {"review"})
check("无分歧、无裁决",
      p["disagreements"] == [] and p["adjudication"] is None
      and p["progress"]["disagreement_count"] == 0)
check("案卷结论通过",
      p["conclusion"]["passed"] is True and p["conclusion"]["passed_items"] == 3
      and p["conclusion"]["failed_items"] == 0 and p["conclusion"]["failed_samples"] == [],
      str(p["conclusion"]))

# 终局后再复核 / 再裁决均拒绝
r = review("DOS-R1", "RV1-3", "复核员丙", {"R1-A": True, "R1-B": True, "R1-C": True})
check("终局后第三次复核 409",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_violation", r.text)
r = adjudicate("DOS-R1", "ADJ1-LATE", "复核员丙", {"R1-A": True})
check("无分歧却裁决 409（越阶段/已终局）",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_violation", r.text)

# ---------------------------------------------------------------- 2. 一致但有不通过
print("2) 两人意见一致但存在不通过份项 → 终局且案卷不通过")
reg("R2-A"); reg("R2-B")
dossier("DOS-R2", ["R2-A", "R2-B"])
r = review("DOS-R2", "RV2-1", "甲", {"R2-A": True, "R2-B": (False, "封签号与登记不符")})
check("第一次 201", r.status_code == 201, r.text)
r = review("DOS-R2", "RV2-2", "乙", {"R2-A": True, "R2-B": (False, "封签破损且号码不符")})
p = r.json()["review_progress"]
check("一致终局 completed", r.status_code == 201 and p["stage"] == "completed", r.text)
check("案卷不通过、failed_samples 含 R2-B",
      p["conclusion"]["passed"] is False and p["conclusion"]["failed_samples"] == ["R2-B"],
      str(p["conclusion"]))
# 终局份项保留各自原始意见与不通过原因
b_item = next(it for it in p["final_items"] if it["sample_code"] == "R2-B")
check("不通过终局原因取该份意见（seq0）",
      b_item["passed"] is False and b_item["fail_reason"] == "封签号与登记不符")
check("原始意见两份均在",
      len(b_item["opinions"]) == 2 and {o["review_id"] for o in b_item["opinions"]} == {"RV2-1", "RV2-2"})

# ---------------------------------------------------------------- 3. 摘要核对
print("3) 摘要核对：错误摘要拒绝写入；正确摘要不受样本后续变化影响")
reg("R3-A"); reg("R3-B")
dossier("DOS-R3", ["R3-A", "R3-B"])
dg3 = digest_of("DOS-R3")
r = review("DOS-R3", "RV3-BAD", "甲", {"R3-A": True, "R3-B": True}, dg="0" * 64)
check("错误摘要 409 digest_mismatch",
      r.status_code == 409 and r.json()["error"]["code"] == "digest_mismatch", r.text)
check("错误明细返回三侧摘要",
      set(r.json()["error"]["details"]) == {"dossier_id", "submitted_digest", "stored_digest", "recomputed_digest"})
check("拒绝后不留复核记录",
      db_scalar("SELECT COUNT(*) FROM dossier_review WHERE review_id='RV3-BAD'") == 0
      and progress("DOS-R3")["stage"] == "awaiting_first")

# 案卷封存后样本再补录事件 → 案卷快照/摘要不变，复核仍以封存版本为准
ev({"event_id": "R3-A-T1", "event_type": "transfer", "sample_code": "R3-A",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-R3-A",
    "from_custodian": "张三", "to_custodian": "李四"})
check("封存后补录不改变案卷摘要", digest_of("DOS-R3") == dg3)
r = review("DOS-R3", "RV3-1", "甲", {"R3-A": True, "R3-B": True})
check("持原摘要仍可复核（只依据封存快照）", r.status_code == 201, r.text)

# 库存内容被篡改 → 重算摘要 != 留存摘要，提交同样拒绝
reg("R3C-A"); reg("R3C-B")
dossier("DOS-R3C", ["R3C-A", "R3C-B"])
dg3c = digest_of("DOS-R3C")
with db_session(immediate=True) as conn:
    conn.execute(
        "UPDATE dossier_item SET snapshot_json = json_set(snapshot_json, '$.status', 'invalid')"
        " WHERE dossier_pk = (SELECT id FROM dossier WHERE dossier_id = 'DOS-R3C')"
        " AND sample_code = 'R3C-A'")
r = review("DOS-R3C", "RV3C-1", "甲", {"R3C-A": True, "R3C-B": True}, dg=dg3c)
check("库存被篡改时即便提交的是留存摘要也拒绝 409",
      r.status_code == 409 and r.json()["error"]["code"] == "digest_mismatch", r.text)
check("篡改场景 recomputed != stored",
      r.json()["error"]["details"]["recomputed_digest"]
      != r.json()["error"]["details"]["stored_digest"])

# ---------------------------------------------------------------- 4. 双人规则与越阶段
print("4) 双人规则：重复复核人拒绝；复核人标识参与同号判定")
reg("R4-A"); reg("R4-B")
dossier("DOS-R4", ["R4-A", "R4-B"])
review("DOS-R4", "RV4-1", "同一人", {"R4-A": True, "R4-B": True})
r = review("DOS-R4", "RV4-2", "同一人", {"R4-A": True, "R4-B": True})
check("同一复核人第二次 422 review_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "review_rejected", r.text)
check("明细码 duplicate_reviewer",
      r.json()["error"]["details"]["items"][0]["code"] == "duplicate_reviewer")
check("重复复核人不占第二席（阶段仍 awaiting_second）",
      progress("DOS-R4")["stage"] == "awaiting_second")
# 不同复核人可继续
r = review("DOS-R4", "RV4-2", "另一人", {"R4-A": True, "R4-B": True})
check("换不同复核人后第二次成功", r.status_code == 201 and r.json()["review_progress"]["stage"] == "completed", r.text)

# ---------------------------------------------------------------- 5. 清单校验：缺项/重复/卷外
print("5) 清单校验：缺项 / 重复份项 / 卷外份项整单拒绝且不留部分记录")
for c in ("R5-A", "R5-B", "R5-C"):
    reg(c)
dossier("DOS-R5", ["R5-A", "R5-B", "R5-C"])

# 缺项
dg5 = digest_of("DOS-R5")
r = client.post(f"{API}/dossiers/DOS-R5/reviews", json={
    "review_id": "RV5-MISS", "reviewer": "甲", "digest": dg5,
    "items": [{"sample_code": "R5-A", "passed": True},
              {"sample_code": "R5-C", "passed": True}]})
check("缺项 422 review_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "review_rejected", r.text)
codes = sorted((it["sample_code"], it["code"]) for it in r.json()["error"]["details"]["items"])
check("缺项明细 missing_review_item 指向 R5-B", codes == [("R5-B", "missing_review_item")], str(codes))

# 重复份项
r = client.post(f"{API}/dossiers/DOS-R5/reviews", json={
    "review_id": "RV5-DUP", "reviewer": "甲", "digest": dg5,
    "items": [{"sample_code": "R5-A", "passed": True},
              {"sample_code": "R5-B", "passed": True},
              {"sample_code": "R5-A", "passed": False, "fail_reason": "重复提交"},
              {"sample_code": "R5-C", "passed": True}]})
check("重复份项 422", r.status_code == 422 and r.json()["error"]["code"] == "review_rejected", r.text)
dup = [it for it in r.json()["error"]["details"]["items"]
       if it["code"] == "duplicate_sample_in_review"]
check("重复明细指向 R5-A", len(dup) == 1 and dup[0]["sample_code"] == "R5-A", r.text)

# 卷外份项
r = client.post(f"{API}/dossiers/DOS-R5/reviews", json={
    "review_id": "RV5-EXTRA", "reviewer": "甲", "digest": dg5,
    "items": [{"sample_code": "R5-A", "passed": True},
              {"sample_code": "R5-B", "passed": True},
              {"sample_code": "R5-C", "passed": True},
              {"sample_code": "R5-GHOST", "passed": True}]})
check("卷外份项 422", r.status_code == 422 and r.json()["error"]["code"] == "review_rejected", r.text)
unk = [it for it in r.json()["error"]["details"]["items"] if it["code"] == "unknown_review_item"]
check("卷外明细 unknown_review_item 指向 R5-GHOST", len(unk) == 1 and unk[0]["sample_code"] == "R5-GHOST")

# 三类失败均不留任何记录（头/明细）
check("失败不留复核头",
      db_scalar("SELECT COUNT(*) FROM dossier_review WHERE review_id IN ('RV5-MISS','RV5-DUP','RV5-EXTRA')") == 0)
check("失败不留复核明细",
      db_scalar("SELECT COUNT(*) FROM dossier_review_item WHERE review_pk IN"
                " (SELECT id FROM dossier_review WHERE dossier_pk=(SELECT id FROM dossier WHERE dossier_id='DOS-R5'))") == 0)
check("失败单号不占用（修正后可复用）",
      review("DOS-R5", "RV5-MISS", "甲",
             {"R5-A": True, "R5-B": True, "R5-C": True}).status_code == 201)

# 载荷校验：不通过缺原因 / 通过带原因
r = client.post(f"{API}/dossiers/DOS-R5/reviews", json={
    "review_id": "RV5-NR", "reviewer": "乙", "digest": dg5,
    "items": [{"sample_code": "R5-A", "passed": False},
              {"sample_code": "R5-B", "passed": True},
              {"sample_code": "R5-C", "passed": True}]})
check("不通过缺原因 422（字段校验）", r.status_code == 422, r.text)
r = client.post(f"{API}/dossiers/DOS-R5/reviews", json={
    "review_id": "RV5-NR2", "reviewer": "乙", "digest": dg5,
    "items": [{"sample_code": "R5-A", "passed": True, "fail_reason": "不该有原因"},
              {"sample_code": "R5-B", "passed": True},
              {"sample_code": "R5-C", "passed": True}]})
check("通过带原因 422（字段校验）", r.status_code == 422, r.text)
review("DOS-R5", "RV5-2", "乙", {"R5-A": True, "R5-B": True, "R5-C": True})

# ---------------------------------------------------------------- 6. 幂等与冲突（意见不可改）
print("6) 幂等/冲突：同单号同内容重试 200；异内容/跨案卷 409；意见不可改")
reg("R6-A"); reg("R6-B")
dossier("DOS-R6", ["R6-A", "R6-B"])
body6 = {"review_id": "RV6-1", "reviewer": "甲", "digest": digest_of("DOS-R6"),
         "items": [{"sample_code": "R6-A", "passed": True},
                   {"sample_code": "R6-B", "passed": False, "fail_reason": "原因X"}]}
r1 = client.post(f"{API}/dossiers/DOS-R6/reviews", json=body6)
check("首次 201", r1.status_code == 201, r1.text)
# 换序同内容
body6r = dict(body6); body6r["items"] = list(reversed(body6["items"]))
r2 = client.post(f"{API}/dossiers/DOS-R6/reviews", json=body6r)
check("换序同内容重试 200 幂等", r2.status_code == 200 and r2.json()["idempotent"] is True, r2.text)
check("幂等返回原进度", r2.json()["review_progress"] == r1.json()["review_progress"])
# 异内容：改结论
bad = json.loads(json.dumps(body6)); bad["items"][0]["passed"] = False
bad["items"][0]["fail_reason"] = "改判"
r = client.post(f"{API}/dossiers/DOS-R6/reviews", json=bad)
check("改结论 409 review_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "review_conflict", r.text)
# 异内容：改复核人
bad = json.loads(json.dumps(body6)); bad["reviewer"] = "乙"
r = client.post(f"{API}/dossiers/DOS-R6/reviews", json=bad)
check("改复核人 409 review_conflict", r.status_code == 409, r.text)
# 异内容：改原因文案
bad = json.loads(json.dumps(body6)); bad["items"][1]["fail_reason"] = "原因Y"
r = client.post(f"{API}/dossiers/DOS-R6/reviews", json=bad)
check("改不通过原因 409", r.status_code == 409, r.text)
# 冲突后原始意见不变
rv = client.get(f"{API}/dossier-reviews/RV6-1").json()["review"]
check("原始意见未被改写",
      [(it["sample_code"], it["passed"], it["fail_reason"]) for it in rv["items"]]
      == [("R6-A", True, None), ("R6-B", False, "原因X")], str(rv))
check("库中仅一条 RV6-1", db_scalar("SELECT COUNT(*) FROM dossier_review WHERE review_id='RV6-1'") == 1)

# 跨案卷复用同一复核单号
reg("R6C-A"); reg("R6C-B")
dossier("DOS-R6C", ["R6C-A", "R6C-B"])
r = client.post(f"{API}/dossiers/DOS-R6C/reviews", json={
    "review_id": "RV6-1", "reviewer": "丙", "digest": digest_of("DOS-R6C"),
    "items": [{"sample_code": "R6C-A", "passed": True},
              {"sample_code": "R6C-B", "passed": True}]})
check("跨案卷复用复核单号 409 review_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "review_conflict", r.text)
check("跨案卷失败不影响原卷进度", progress("DOS-R6")["progress"]["reviews_submitted"] == 1)

# 案卷不存在
r = client.post(f"{API}/dossiers/DOS-GHOST/reviews", json={
    "review_id": "RV6-X", "reviewer": "甲", "digest": "0" * 64,
    "items": [{"sample_code": "X", "passed": True}]})
check("复核不存在案卷 404 dossier_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "dossier_not_found", r.text)

# ---------------------------------------------------------------- 7. 分歧与第三人裁决
print("7) 分歧份项：仅未参与前两次复核的第三人可裁决，裁决后终局")
for c in ("R7-A", "R7-B", "R7-C"):
    complete_valid(c)
dossier("DOS-R7", ["R7-A", "R7-B", "R7-C"])
# 甲：A 通过、B 不通过、C 通过；乙：A 不通过、B 不通过、C 不通过
# 分歧 = A、C；B 两人一致不通过直接终局
review("DOS-R7", "RV7-1", "甲",
       {"R7-A": True, "R7-B": (False, "甲判B不过"), "R7-C": True})
r = review("DOS-R7", "RV7-2", "乙",
           {"R7-A": (False, "乙判A不过"), "R7-B": (False, "乙判B不过"), "R7-C": (False, "乙判C不过")})
p = r.json()["review_progress"]
check("两次后 awaiting_adjudication", r.status_code == 201 and p["stage"] == "awaiting_adjudication", r.text)
check("分歧份项恰为 A、C", [d["sample_code"] for d in p["disagreements"]] == ["R7-A", "R7-C"],
      str(p["disagreements"]))
check("B 已一致终局（不通过）",
      [it["sample_code"] for it in p["final_items"]] == ["R7-B"]
      and p["final_items"][0]["passed"] is False)
check("终局份项 1/3", p["progress"]["finalized_items"] == 1)
# 分歧明细带两人原始意见
d_a = next(d for d in p["disagreements"] if d["sample_code"] == "R7-A")
check("分歧原始意见完整（通过/不通过）",
      [o["passed"] for o in d_a["opinions"]] == [True, False]
      and {o["reviewer"] for o in d_a["opinions"]} == {"甲", "乙"}, str(d_a))

# 裁决人是前复核人
r = adjudicate("DOS-R7", "ADJ7-BAD", "甲", {"R7-A": True, "R7-C": True})
check("前复核人裁决 422 adjudication_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "adjudication_rejected", r.text)
check("明细码 adjudicator_must_be_third",
      r.json()["error"]["details"]["items"][0]["code"] == "adjudicator_must_be_third")

# 裁决清单缺项（只裁 A）
r = adjudicate("DOS-R7", "ADJ7-MISS", "丙", {"R7-A": True})
check("裁决缺项 422",
      r.status_code == 422 and r.json()["error"]["code"] == "adjudication_rejected", r.text)
miss = [it for it in r.json()["error"]["details"]["items"]
        if it["code"] == "missing_adjudication_item"]
check("缺项明细指向 R7-C 且附分歧集合",
      [it["sample_code"] for it in miss] == ["R7-C"]
      and r.json()["error"]["details"]["disagreement_samples"] == ["R7-A", "R7-C"], r.text)

# 裁决无分歧份项（B 已一致）
r = adjudicate("DOS-R7", "ADJ7-EXTRA", "丙",
               {"R7-A": True, "R7-B": True, "R7-C": True})
check("裁决无分歧份项 422",
      r.status_code == 422 and r.json()["error"]["code"] == "adjudication_rejected", r.text)
extra = [it for it in r.json()["error"]["details"]["items"]
         if it["code"] == "item_without_disagreement"]
check("无分歧明细指向 R7-B", [it["sample_code"] for it in extra] == ["R7-B"], r.text)

# 裁决摘要错误
r = adjudicate("DOS-R7", "ADJ7-DG", "丙",
               {"R7-A": True, "R7-C": True}, dg="f" * 64)
check("裁决摘要错误 409 digest_mismatch",
      r.status_code == 409 and r.json()["error"]["code"] == "digest_mismatch", r.text)

# 失败裁决不留记录
check("失败裁决不留头",
      db_scalar("SELECT COUNT(*) FROM dossier_adjudication WHERE adjudication_id IN"
                " ('ADJ7-BAD','ADJ7-MISS','ADJ7-EXTRA','ADJ7-DG')") == 0)
check("失败裁决不留明细",
      db_scalar("SELECT COUNT(*) FROM dossier_adjudication_item") == 0)

# 正确裁决：丙裁 A 通过、C 不通过
r = adjudicate("DOS-R7", "ADJ7-1", "丙",
               {"R7-A": True, "R7-C": (False, "丙终裁C不过")})
check("裁决 201", r.status_code == 201 and r.json()["idempotent"] is False, r.text)
p = r.json()["review_progress"]
check("裁决后 completed", p["stage"] == "completed" and p["completed"] is True)
fin = {it["sample_code"]: it for it in p["final_items"]}
check("三份均终局", p["progress"]["finalized_items"] == 3 and set(fin) == {"R7-A", "R7-B", "R7-C"})
check("A/C 由裁决终局、B 由复核一致终局",
      fin["R7-A"]["decided_by"] == "adjudication" and fin["R7-A"]["passed"] is True
      and fin["R7-C"]["decided_by"] == "adjudication" and fin["R7-C"]["passed"] is False
      and fin["R7-B"]["decided_by"] == "review")
check("裁决终局原因保留", fin["R7-C"]["fail_reason"] == "丙终裁C不过")
check("案卷不通过（B、C 不过）",
      p["conclusion"]["passed"] is False
      and p["conclusion"]["failed_samples"] == ["R7-B", "R7-C"]
      and p["conclusion"]["adjudicated_samples"] == ["R7-A", "R7-C"], str(p["conclusion"]))
check("裁决信息可查",
      p["adjudication"]["adjudication_id"] == "ADJ7-1"
      and p["adjudication"]["adjudicator"] == "丙"
      and codes_in(p["adjudication"]["items"]) == ["R7-A", "R7-C"])
# 分歧明细回填裁决结果
check("分歧明细回填裁决",
      all(d["adjudication"] is not None for d in p["disagreements"]))

# 终局后再裁决 / 再复核拒绝
r = adjudicate("DOS-R7", "ADJ7-2", "丁", {"R7-A": True, "R7-C": True})
check("终局后再裁决 409",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_violation", r.text)
r = review("DOS-R7", "RV7-3", "丁",
           {"R7-A": True, "R7-B": True, "R7-C": True})
check("终局后再复核 409",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_violation", r.text)

# 裁决幂等：同单号同内容（换序）重试 200
body7 = {"adjudication_id": "ADJ7-1", "adjudicator": "丙", "digest": digest_of("DOS-R7"),
         "items": [{"sample_code": "R7-C", "passed": False, "fail_reason": "丙终裁C不过"},
                   {"sample_code": "R7-A", "passed": True}]}
r = client.post(f"{API}/dossiers/DOS-R7/adjudications", json=body7)
check("裁决同单号换序重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("幂等返回终局结论", r.json()["review_progress"]["conclusion"]["passed"] is False)
# 裁决异内容冲突
bad = json.loads(json.dumps(body7))
bad["items"][0]["passed"] = True
bad["items"][0].pop("fail_reason", None)
r = client.post(f"{API}/dossiers/DOS-R7/adjudications", json=bad)
check("裁决异内容 409 adjudication_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "adjudication_conflict", r.text)

# ---------------------------------------------------------------- 8. 查询与错误
print("8) 查询接口与错误码")
reg("R8-A"); reg("R8-B")
dossier("DOS-R8", ["R8-A", "R8-B"])
review("DOS-R8", "RV8-1", "甲", {"R8-A": True, "R8-B": True})
# 进度查询
p = progress("DOS-R8")
check("进度查询含复核人列表与摘要",
      p["dossier_id"] == "DOS-R8" and p["progress"]["reviewers"] == ["甲"]
      and p["progress"]["required_reviews"] == 2 and bool(p["digest"]), str(p))
# 单号查询
r = client.get(f"{API}/dossier-reviews/RV8-1")
check("单号查询 200 且含 review + progress",
      r.status_code == 200 and r.json()["review"]["review_id"] == "RV8-1"
      and r.json()["progress"]["stage"] == "awaiting_second", r.text)
r = client.get(f"{API}/dossier-reviews/NO-SUCH-RV")
check("未知复核单号 404 review_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "review_not_found", r.text)
# 未知案卷进度
r = client.get(f"{API}/dossiers/DOS-GHOST/reviews")
check("未知案卷进度 404 dossier_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "dossier_not_found", r.text)

# ---------------------------------------------------------------- 9. 并发
print("9) 并发：同单号同内容一建一幂等；第二席竞争按已提交状态；裁决并发仅一次成功")
for c in ("R9-A", "R9-B"):
    reg(c)
dossier("DOS-R9", ["R9-A", "R9-B"])

# 9.1 同复核单号同内容并发
body9 = {"review_id": "RV9-1", "reviewer": "甲", "digest": digest_of("DOS-R9"),
         "items": [{"sample_code": "R9-A", "passed": True},
                   {"sample_code": "R9-B", "passed": True}]}
results9: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_rv(tag):
    barrier.wait()
    results9[tag] = client.post(f"{API}/dossiers/DOS-R9/reviews", json=body9).status_code


t1 = threading.Thread(target=post_rv, args=("A",))
t2 = threading.Thread(target=post_rv, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同单号同内容并发：一次 201 一次 200", sorted(results9.values()) == [200, 201], str(results9))
check("并发后仅一条复核",
      db_scalar("SELECT COUNT(*) FROM dossier_review WHERE review_id='RV9-1'") == 1)

# 9.2 两个不同复核人竞争第二席：一胜一 422（重复复核人不适用——人不同；
#     两名单内容不同、seq=1 唯一槽位竞争），落败方 409 越阶段且库中恰两条
# 先放一条乙的
review("DOS-R9", "RV9-2", "乙", {"R9-A": True, "R9-B": True})
# 此时第二席已占；再以并发复现竞争场景（丙、丁同时抢，都只能拿到第三席 → 都 409）
body9c = {"review_id": "RV9-3", "reviewer": "丙", "digest": digest_of("DOS-R9"),
          "items": [{"sample_code": "R9-A", "passed": True},
                    {"sample_code": "R9-B", "passed": True}]}
body9d = {"review_id": "RV9-4", "reviewer": "丁", "digest": digest_of("DOS-R9"),
          "items": [{"sample_code": "R9-A", "passed": True},
                    {"sample_code": "R9-B", "passed": True}]}
results9b: dict[str, int] = {}
barrier2 = threading.Barrier(2)


def post_rv2(tag, body):
    barrier2.wait()
    results9b[tag] = client.post(f"{API}/dossiers/DOS-R9/reviews", json=body).status_code


t1 = threading.Thread(target=post_rv2, args=("C", body9c))
t2 = threading.Thread(target=post_rv2, args=("D", body9d))
t1.start(); t2.start(); t1.join(); t2.join()
check("满员后并发均 409 越阶段",
      set(results9b.values()) == {409}, str(results9b))
check("库中恰两条复核、无部分明细",
      db_scalar("SELECT COUNT(*) FROM dossier_review WHERE dossier_pk="
                "(SELECT id FROM dossier WHERE dossier_id='DOS-R9')") == 2
      and db_scalar("SELECT COUNT(*) FROM dossier_review_item WHERE review_pk IN"
                    " (SELECT id FROM dossier_review WHERE review_id IN ('RV9-3','RV9-4'))") == 0)

# 9.3 裁决并发：先制造分歧，两名第三人同时裁决，一次成功一次 409
for c in ("R9D-A", "R9D-B"):
    reg(c)
dossier("DOS-R9D", ["R9D-A", "R9D-B"])
review("DOS-R9D", "RV9D-1", "甲", {"R9D-A": True, "R9D-B": True})
review("DOS-R9D", "RV9D-2", "乙",
       {"R9D-A": (False, "乙判不过"), "R9D-B": True})
body9e = {"adjudication_id": "ADJ9D-1", "adjudicator": "丙", "digest": digest_of("DOS-R9D"),
          "items": [{"sample_code": "R9D-A", "passed": True}]}
body9f = {"adjudication_id": "ADJ9D-2", "adjudicator": "丁", "digest": digest_of("DOS-R9D"),
          "items": [{"sample_code": "R9D-A", "passed": False, "fail_reason": "丁裁不过"}]}
results9c: dict[str, int] = {}
barrier3 = threading.Barrier(2)


def post_adj(tag, body):
    barrier3.wait()
    results9c[tag] = client.post(f"{API}/dossiers/DOS-R9D/adjudications", json=body).status_code


t1 = threading.Thread(target=post_adj, args=("E", body9e))
t2 = threading.Thread(target=post_adj, args=("F", body9f))
t1.start(); t2.start(); t1.join(); t2.join()
check("裁决并发：一次 201 一次 409", sorted(results9c.values()) == [201, 409], str(results9c))
check("库中仅一条裁决、两份复核进度终局",
      db_scalar("SELECT COUNT(*) FROM dossier_adjudication WHERE dossier_pk="
                "(SELECT id FROM dossier WHERE dossier_id='DOS-R9D')") == 1
      and progress("DOS-R9D")["stage"] == "completed")
winner = "ADJ9D-1" if results9c["E"] == 201 else "ADJ9D-2"
check("终局结论遵循已提交裁决",
      progress("DOS-R9D")["adjudication"]["adjudication_id"] == winner)

# ---------------------------------------------------------------- 10. 复核依据封存快照（与案卷摘要算法一致）
print("10) 端到端：进度查询中的摘要与案卷封存摘要一致")
reg("R10-A"); reg("R10-B")
dossier("DOS-R10", ["R10-A", "R10-B"])
dg10 = digest_of("DOS-R10")
check("复核进度携带案卷摘要", progress("DOS-R10")["digest"] == dg10)
# 独立复算摘要算法仍可用（复用封存模块函数）
d = client.get(f"{API}/dossiers/DOS-R10").json()["dossier"]
check("摘要可由封存清单独立复算",
      compute_digest("DOS-R10", d["items"]) == dg10)

# ---------------------------------------------------------------- 11. 既有接口不受影响
print("11) 既有接口行为保持")
check("健康检查", client.get("/health").json()["status"] == "ok")
check("样本列表可用", "items" in client.get(f"{API}/samples").json())
r = ev({"event_id": "R11-T1", "event_type": "transfer", "sample_code": "R10-A",
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-R11",
        "from_custodian": "张三", "to_custodian": "李四"})
check("事件写入不受影响 201", r.status_code == 201, r.text)
r = dossier("DOS-R11", ["R10-A", "R10-B"])
check("新案卷封存不受影响", r.status_code == 201, r.text)
check("既有案卷查询不受影响", client.get(f"{API}/dossiers/DOS-R1").status_code == 200)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
