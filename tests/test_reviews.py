"""端到端自测：已封存案卷双人独立复核 + 第三人裁决。

覆盖：
  * 摘要核对：提交摘要与案卷封存摘要不一致 → 422 review_digest_mismatch 且不留记录；
    失败不占用复核单号，修正后可提交；
  * 两份独立复核：两名不同复核人各提交一次完整清单；重复复核人 409；
    缺项 / 重复份项 / 未知份项 422 且不留部分记录；份项顺序不敏感；
    通过带原因 / 不通过无原因 → 422（字段校验）；
  * 终局形成：两份均通过 → 终局通过；均不通过且原因相同 → 终局不通过（无需裁决）；
    结论分歧（pass_conflict）或不通过原因分歧（reason_conflict）→ 待第三人裁决；
  * 第三人裁决：裁决人不得参与前两次复核（409 duplicate_reviewer）；
    裁决清单必须恰好覆盖分歧份项（越权份项 / 漏裁 / 重复 → 422，不留记录）；
    裁决后全部份项终局，任一份不通过则案卷不通过；
  * 越阶段：无分歧时裁决、案卷终局后再提交 → 409 review_stage_rejected；意见不可改；
  * 幂等/冲突：同复核单号同内容重试 200 返回原结果；异内容 409 review_conflict；
    复核单号跨案卷全局唯一；
  * 查询：阶段 awaiting_review/first_reviewed/awaiting_adjudication/completed、
    原始意见、分歧对照、逐份终局与案卷结论；
  * 并发：不同复核人并发提交各占一个次序；同复核人并发仅一人成功；
    同复核单号同内容并发一建一幂等——按已提交状态判定。

运行：python -m tests.test_reviews
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_reviews.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()  # 触发 lifespan（建表/启动钩子）
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


def complete_valid(code):
    """登记并补齐 移交→签收→检查→入库 的有效链（版本推进到 4）。"""
    seal = f"SEAL-{code}"
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
        client.post(f"{API}/events", json=payload)


def make_dossier(did, codes):
    complete_valid(codes[0])
    for c in codes[1:]:
        complete_valid(c)
    r = client.post(f"{API}/dossiers", json={
        "dossier_id": did,
        "items": [{"sample_code": c} for c in sorted(codes)]})
    assert r.status_code == 201, r.text
    return r.json()["dossier"]["digest"]


def submit(did, review_no, reviewer, digest, items):
    return client.post(f"{API}/dossiers/{did}/reviews", json={
        "review_no": review_no, "reviewer": reviewer,
        "expected_digest": digest, "items": items})


def progress(did):
    return client.get(f"{API}/dossiers/{did}/reviews").json()


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def op(code, passed=True, reason=None):
    return {"sample_code": code, "passed": passed, "reason": reason}


def all_pass(codes):
    return [op(c, True) for c in codes]


# ---------------------------------------------------------------- 1. 未提交时的进度
print("1) 未提交复核：阶段 awaiting_review，逐份 pending，结论为空")
DIG_A = make_dossier("REV-DOS-A", ["RA-1", "RA-2"])
p = progress("REV-DOS-A")
check("stage=awaiting_review", p["stage"] == "awaiting_review", str(p["stage"]))
check("review_count=0 / 全部 pending",
      p["review_count"] == 0 and p["pending_item_count"] == 2
      and all(it["status"] == "pending" for it in p["items"]), str(p))
check("结论与 finalized_at 为空", p["conclusion"] is None and p["finalized_at"] is None)
check("原始意见为空", all(it["opinions"] == [] for it in p["items"]))

# ---------------------------------------------------------------- 2. 摘要核对
print("2) 摘要核对：不一致拒绝写入且不占用复核单号；修正后可提交")
r = submit("REV-DOS-A", "REV-NO-A1", "QA-甲", "0" * 64, all_pass(["RA-1", "RA-2"]))
check("摘要不一致 422 review_digest_mismatch",
      r.status_code == 422 and r.json()["error"]["code"] == "review_digest_mismatch", r.text)
check("错误详情给出两侧摘要",
      r.json()["error"]["details"]["stored_digest"] == DIG_A
      and r.json()["error"]["details"]["expected_digest"] == "0" * 64, r.text)
check("摘要失败不留复核案",
      db_scalar("SELECT COUNT(*) FROM review_case rc JOIN dossier d ON rc.dossier_pk=d.id"
                " WHERE d.dossier_id='REV-DOS-A'") == 0)
# 同一复核单号修正摘要后成功（失败不占用单号）
r = submit("REV-DOS-A", "REV-NO-A1", "QA-甲", DIG_A, all_pass(["RA-2", "RA-1"]))  # 顺序故意打乱
check("修正摘要后首份 201（份项顺序不敏感）", r.status_code == 201, r.text)
check("提交后阶段 first_reviewed", r.json()["review"]["stage"] == "first_reviewed", r.text)
check("submission.seq=1 kind=review",
      r.json()["submission"]["seq"] == 1 and r.json()["submission"]["kind"] == "review")

# ---------------------------------------------------------------- 3. 完整清单校验
print("3) 完整清单：缺项 / 重复份项 / 未知份项 拒绝且不留部分记录")
DIG_B = make_dossier("REV-DOS-B", ["RB-1", "RB-2", "RB-3"])

r = submit("REV-DOS-B", "REV-NO-B1", "QA-甲", DIG_B, [op("RB-1", True), op("RB-2", True)])
check("缺项 422 review_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "review_rejected", r.text)
codes = {x["code"] for x in r.json()["error"]["details"]["items"]}
check("明细标注 missing_sample_in_review", codes == {"missing_sample_in_review"}, str(codes))

r = submit("REV-DOS-B", "REV-NO-B2", "QA-甲", DIG_B,
           [op("RB-1", True), op("RB-2", True), op("RB-2", False, "破损"), op("RB-3", True)])
check("重复份项 422", r.status_code == 422, r.text)
codes = {x["code"] for x in r.json()["error"]["details"]["items"]}
check("明细含 duplicate_sample_in_review", "duplicate_sample_in_review" in codes, str(codes))

r = submit("REV-DOS-B", "REV-NO-B3", "QA-甲", DIG_B,
           [op("RB-1", True), op("RB-2", True), op("GHOST", False, "未知")])
check("未知份项 422", r.status_code == 422, r.text)
codes = {x["code"] for x in r.json()["error"]["details"]["items"]}
check("明细含 unknown_sample_in_review 且同时报缺项",
      "unknown_sample_in_review" in codes and "missing_sample_in_review" in codes, str(codes))

check("三次失败均不留复核案/提交/意见",
      db_scalar("SELECT COUNT(*) FROM review_case rc JOIN dossier d ON rc.dossier_pk=d.id"
                " WHERE d.dossier_id='REV-DOS-B'") == 0
      and db_scalar("SELECT COUNT(*) FROM review") == 1   # 仅 REV-DOS-A 的一份
      and db_scalar("SELECT COUNT(*) FROM review_opinion") == 2)

# 字段校验：通过带原因 / 不通过无原因
r = submit("REV-DOS-B", "REV-NO-B4", "QA-甲", DIG_B,
           [op("RB-1", True, "不该有原因"), op("RB-2", True), op("RB-3", True)])
check("通过带原因 422（字段校验）", r.status_code == 422, r.text)
r = submit("REV-DOS-B", "REV-NO-B5", "QA-甲", DIG_B,
           [op("RB-1", False), op("RB-2", True), op("RB-3", True)])
check("不通过无原因 422（字段校验）", r.status_code == 422, r.text)
check("字段校验失败同样不留记录",
      db_scalar("SELECT COUNT(*) FROM review_case rc JOIN dossier d ON rc.dossier_pk=d.id"
                " WHERE d.dossier_id='REV-DOS-B'") == 0)

# ---------------------------------------------------------------- 4. 重复复核人
print("4) 两名复核人必须不同：同一人第二次提交 409 duplicate_reviewer")
r = submit("REV-DOS-A", "REV-NO-A2", "QA-甲", DIG_A, all_pass(["RA-1", "RA-2"]))
check("同复核人重复提交 409",
      r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reviewer", r.text)
check("重复复核人不留记录（仍只有一份）",
      progress("REV-DOS-A")["review_count"] == 1)
# 换单号、换内容仍是同一人 → 拒绝（意见不可借新单号补提）
r = submit("REV-DOS-A", "REV-NO-A2X", "QA-甲", DIG_A,
           [op("RA-1", False, "封签破损"), op("RA-2", True)])
check("同复核人换单号仍 409", r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reviewer")

# ---------------------------------------------------------------- 5. 意见一致 → 终局通过
print("5) 两份均通过：逐份终局（agreement），案卷结论通过")
r = submit("REV-DOS-A", "REV-NO-A3", "QA-乙", DIG_A, all_pass(["RA-1", "RA-2"]))
check("第二份独立复核 201", r.status_code == 201, r.text)
rv = r.json()["review"]
check("stage=completed / 全部终局",
      rv["stage"] == "completed" and rv["all_items_final"] and rv["final_item_count"] == 2, str(rv["stage"]))
check("案卷结论通过",
      rv["conclusion"]["passed"] is True and rv["conclusion"]["status"] == "passed"
      and rv["conclusion"]["failed_samples"] == [], str(rv["conclusion"]))
check("案卷结论形成时间已固化", bool(rv["finalized_at"]))
for it in rv["items"]:
    check(f"{it['sample_code']} 终局通过、来源 agreement",
          it["status"] == "final" and it["final"] == {"passed": True, "reason": None, "source": "agreement"},
          str(it["final"]))
    check(f"{it['sample_code']} 含两条原始意见",
          len(it["opinions"]) == 2
          and {o["reviewer"] for o in it["opinions"]} == {"QA-甲", "QA-乙"}, str(it["opinions"]))
check("无分歧份项", rv["disputed_item_count"] == 0 and rv["disputed_samples"] == [])

# ---------------------------------------------------------------- 6. 幂等与冲突
print("6) 同复核单号：同内容重试 200 返回原结果；异内容 409；跨案卷占用 409")
r2 = submit("REV-DOS-A", "REV-NO-A3", "QA-乙", DIG_A, list(reversed(all_pass(["RA-1", "RA-2"]))))
check("同号同内容（换序）重试 200 幂等",
      r2.status_code == 200 and r2.json()["submission"]["review_no"] == "REV-NO-A3", r2.text)
r3 = submit("REV-DOS-A", "REV-NO-A3", "QA-乙", DIG_A,
            [op("RA-1", False, "改了结论"), op("RA-2", True)])
check("同号异内容 409 review_conflict",
      r3.status_code == 409 and r3.json()["error"]["code"] == "review_conflict", r3.text)
# 同号在另一个案卷使用 → 全局唯一冲突
DIG_D = make_dossier("REV-DOS-D", ["RD-1", "RD-2"])
r = submit("REV-DOS-D", "REV-NO-A3", "QA-丁", DIG_D, all_pass(["RD-1", "RD-2"]))
check("复核单号跨案卷占用 409 review_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "review_conflict", r.text)
check("跨案卷冲突不留记录", progress("REV-DOS-D")["review_count"] == 0)

# ---------------------------------------------------------------- 7. 终局后越阶段
print("7) 案卷终局后再提交 → 409 review_stage_rejected（意见不可改）")
r = submit("REV-DOS-A", "REV-NO-A9", "QA-丙", DIG_A, all_pass(["RA-1", "RA-2"]))
check("终局后提交 409",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_rejected", r.text)
check("终局结论不被改变",
      progress("REV-DOS-A")["conclusion"]["status"] == "passed")

# ---------------------------------------------------------------- 8. 均不通过且原因相同 → 终局不通过
print("8) 两份均不通过且原因相同：终局不通过，无分歧、无需裁决")
DIG_E = make_dossier("REV-DOS-E", ["RE-1", "RE-2"])
r = submit("REV-DOS-E", "REV-NO-E1", "QA-甲", DIG_E,
           [op("RE-1", False, "封签破损"), op("RE-2", True)])
r = submit("REV-DOS-E", "REV-NO-E2", "QA-乙", DIG_E,
           [op("RE-1", False, "封签破损"), op("RE-2", True)])
check("第二份 201 且完成", r.status_code == 201 and r.json()["review"]["stage"] == "completed", r.text)
rv = r.json()["review"]
check("案卷不通过、failed_samples=[RE-1]",
      rv["conclusion"]["passed"] is False and rv["conclusion"]["status"] == "rejected"
      and rv["conclusion"]["failed_samples"] == ["RE-1"], str(rv["conclusion"]))
check("RE-1 终局不通过（agreement，保留共同原因）",
      rv["items"][0]["final"] == {"passed": False, "reason": "封签破损", "source": "agreement"},
      str(rv["items"][0]["final"]))
# 无分歧却提交"裁决" → 越阶段
r = submit("REV-DOS-E", "REV-NO-E3", "QA-丙", DIG_E, [op("RE-1", True)])
check("无分歧时裁决 409 review_stage_rejected",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_rejected", r.text)
check("终局后裁决也不留记录", progress("REV-DOS-E")["review_count"] == 2)

# ---------------------------------------------------------------- 9. 分歧与第三人裁决
print("9) 结论分歧 + 原因分歧 → 第三人裁决（资格/清单校验/终局）")
DIG_F = make_dossier("REV-DOS-F", ["RF-X", "RF-Y", "RF-Z"])
r1 = submit("REV-DOS-F", "REV-NO-F1", "QA-甲", DIG_F, [
    op("RF-X", True), op("RF-Y", True), op("RF-Z", False, "封签号不符")])
check("首份 201", r1.status_code == 201, r1.text)
p1 = progress("REV-DOS-F")
check("首份后 stage=first_reviewed、逐份 pending",
      p1["stage"] == "first_reviewed" and all(i["status"] == "pending" for i in p1["items"]))
# 第二份：X 一致通过；Y 结论分歧（乙判不通过）；Z 均不通过但原因不同
r2 = submit("REV-DOS-F", "REV-NO-F2", "QA-乙", DIG_F, [
    op("RF-X", True), op("RF-Y", False, "封签破损"), op("RF-Z", False, "封签破损")])
check("第二份 201", r2.status_code == 201, r2.text)
rv = r2.json()["review"]
check("stage=awaiting_adjudication、分歧 Y/Z",
      rv["stage"] == "awaiting_adjudication"
      and rv["disputed_samples"] == ["RF-Y", "RF-Z"], str(rv))
by = {it["sample_code"]: it for it in rv["items"]}
check("X 已终局通过", by["RF-X"]["status"] == "final" and by["RF-X"]["final"]["passed"] is True)
check("Y 为 pass_conflict",
      by["RF-Y"]["status"] == "disputed" and by["RF-Y"]["disagreement"]["type"] == "pass_conflict"
      and by["RF-Y"]["disagreement"]["reviewers"] == ["QA-甲", "QA-乙"], str(by["RF-Y"]))
check("Z 为 reason_conflict 且保留两人原始原因",
      by["RF-Z"]["status"] == "disputed" and by["RF-Z"]["disagreement"]["type"] == "reason_conflict"
      and [o["reason"] for o in by["RF-Z"]["opinions"]] == ["封签号不符", "封签破损"], str(by["RF-Z"]))
check("查询可追溯全部原始意见（含复核单号/次序）",
      [(o["seq"], o["review_no"], o["passed"]) for o in by["RF-Y"]["opinions"]]
      == [(1, "REV-NO-F1", True), (2, "REV-NO-F2", False)], str(by["RF-Y"]["opinions"]))

# 9.1 裁决人资格：前两次复核人不得裁决
r = submit("REV-DOS-F", "REV-NO-F3X", "QA-甲", DIG_F,
           [op("RF-Y", True), op("RF-Z", True)])
check("参与过前两次复核的人裁决 409 duplicate_reviewer",
      r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reviewer", r.text)

# 9.2 裁决清单必须恰好覆盖分歧份项
r = submit("REV-DOS-F", "REV-NO-F3A", "QA-丙", DIG_F,
           [op("RF-X", True), op("RF-Y", True), op("RF-Z", True)])
check("裁决越权份项（X 非分歧）422", r.status_code == 422, r.text)
codes = {x["code"] for x in r.json()["error"]["details"]["items"]}
check("标注 adjudication_not_allowed", "adjudication_not_allowed" in codes, str(codes))

r = submit("REV-DOS-F", "REV-NO-F3B", "QA-丙", DIG_F, [op("RF-Y", True)])
check("漏裁分歧份项（Z）422", r.status_code == 422, r.text)
codes = {x["code"] for x in r.json()["error"]["details"]["items"]}
check("标注 missing_adjudication", codes == {"missing_adjudication"}, str(codes))

r = submit("REV-DOS-F", "REV-NO-F3C", "QA-丙", DIG_F,
           [op("RF-Y", True), op("RF-Y", False, "重复"), op("RF-Z", True)])
check("裁决清单重复份项 422", r.status_code == 422, r.text)

check("裁决失败不留记录（仍两份、仍待裁决）",
      progress("REV-DOS-F")["review_count"] == 2
      and progress("REV-DOS-F")["stage"] == "awaiting_adjudication")

# 9.3 合法第三人裁决：Y 裁决通过，Z 裁决不通过 → 案卷不通过（Z）
r = submit("REV-DOS-F", "REV-NO-F3", "QA-丙", DIG_F,
           [op("RF-Y", True), op("RF-Z", False, "维持不通过：封签破损")])
check("第三人裁决 201", r.status_code == 201, r.text)
rv = r.json()["review"]
check("stage=completed / 全部终局",
      rv["stage"] == "completed" and rv["all_items_final"], str(rv["stage"]))
check("案卷不通过，failed_samples=[RF-Z]",
      rv["conclusion"]["passed"] is False and rv["conclusion"]["failed_samples"] == ["RF-Z"],
      str(rv["conclusion"]))
by = {it["sample_code"]: it for it in rv["items"]}
check("Y 终局来源 adjudication、通过",
      by["RF-Y"]["final"] == {"passed": True, "reason": None, "source": "adjudication"})
check("Z 终局来源 adjudication、不通过且取裁决原因",
      by["RF-Z"]["final"] == {"passed": False, "reason": "维持不通过：封签破损",
                              "source": "adjudication"}, str(by["RF-Z"]["final"]))
check("分歧份项含三条原始意见（含第三人）",
      len(by["RF-Y"]["opinions"]) == 3 and by["RF-Y"]["opinions"][2]["kind"] == "adjudication"
      and by["RF-Y"]["opinions"][2]["reviewer"] == "QA-丙")
check("一致份项 X 仍只有两条意见", len(by["RF-X"]["opinions"]) == 2)
sub = r.json()["submission"]
check("submission.seq=3 kind=adjudication", sub["seq"] == 3 and sub["kind"] == "adjudication")
check("submission 仅含裁决的两份", sorted(i["sample_code"] for i in sub["items"]) == ["RF-Y", "RF-Z"])

# 9.4 裁决完成后再提交 → 越阶段
r = submit("REV-DOS-F", "REV-NO-F4", "QA-丁", DIG_F, [op("RF-Z", True)])
check("终局后提交 409 review_stage_rejected",
      r.status_code == 409 and r.json()["error"]["code"] == "review_stage_rejected", r.text)
check("终局结论不变",
      progress("REV-DOS-F")["conclusion"]["failed_samples"] == ["RF-Z"])

# ---------------------------------------------------------------- 10. 不存在案卷
print("10) 不存在案卷：提交与查询均 404 dossier_not_found")
r = client.post(f"{API}/dossiers/NO-SUCH-DOS/reviews", json={
    "review_no": "REV-NO-X", "reviewer": "QA-甲",
    "expected_digest": "0" * 64, "items": [op("X", True)]})
check("提交至不存在案卷 404",
      r.status_code == 404 and r.json()["error"]["code"] == "dossier_not_found", r.text)
r = client.get(f"{API}/dossiers/NO-SUCH-DOS/reviews")
check("查询不存在案卷 404",
      r.status_code == 404 and r.json()["error"]["code"] == "dossier_not_found", r.text)

# ---------------------------------------------------------------- 11. 并发
print("11) 并发：按已提交状态判定（不同复核人各占一次序；同人仅一成；同号一建一幂等）")

# 11.1 两名不同复核人并发提交 → 分别成为 seq1/seq2，意见一致直接完成
DIG_G = make_dossier("REV-DOS-G", ["RG-1", "RG-2"])
results_g: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_g(tag, review_no, reviewer):
    barrier.wait()
    results_g[tag] = submit("REV-DOS-G", review_no, reviewer, DIG_G,
                            all_pass(["RG-1", "RG-2"])).status_code


t1 = threading.Thread(target=post_g, args=("甲", "REV-NO-G1", "QA-甲"))
t2 = threading.Thread(target=post_g, args=("乙", "REV-NO-G2", "QA-乙"))
t1.start(); t2.start(); t1.join(); t2.join()
check("两名不同复核人并发：两次均 201", sorted(results_g.values()) == [201, 201], str(results_g))
pg = progress("REV-DOS-G")
check("并发后共两份、阶段 completed（按已提交状态串行判定）",
      pg["review_count"] == 2 and pg["stage"] == "completed", str(pg["stage"]))

# 11.2 同一复核人并发（不同复核单号）→ 仅一人成功
DIG_H = make_dossier("REV-DOS-H", ["RH-1", "RH-2"])
results_h: dict[str, int] = {}
barrier2 = threading.Barrier(2)


def post_h(tag, review_no):
    barrier2.wait()
    results_h[tag] = submit("REV-DOS-H", review_no, "QA-甲", DIG_H,
                            all_pass(["RH-1", "RH-2"])).status_code


t1 = threading.Thread(target=post_h, args=("A", "REV-NO-H1"))
t2 = threading.Thread(target=post_h, args=("B", "REV-NO-H2"))
t1.start(); t2.start(); t1.join(); t2.join()
check("同一复核人并发：一次成功一次 409", sorted(results_h.values()) == [201, 409], str(results_h))
check("并发后仅一份提交记录", progress("REV-DOS-H")["review_count"] == 1)

# 11.3 同复核单号同内容并发 → 一建一幂等
DIG_I = make_dossier("REV-DOS-I", ["RI-1", "RI-2"])
results_i: dict[str, int] = {}
barrier3 = threading.Barrier(2)
body_i = [op("RI-1", True), op("RI-2", True)]


def post_i(tag):
    barrier3.wait()
    results_i[tag] = submit("REV-DOS-I", "REV-NO-I1", "QA-甲", DIG_I, body_i).status_code


t1 = threading.Thread(target=post_i, args=("A",))
t2 = threading.Thread(target=post_i, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同号同内容并发：一次 201 一次 200", sorted(results_i.values()) == [200, 201], str(results_i))
opinion_count = db_scalar(
    "SELECT COUNT(*) FROM review_opinion ro JOIN review r ON ro.review_pk=r.id"
    " JOIN review_case rc ON r.case_pk=rc.id JOIN dossier d ON rc.dossier_pk=d.id"
    " WHERE d.dossier_id='REV-DOS-I'")
check("并发后仅一份提交、该提交两条逐份意见",
      progress("REV-DOS-I")["review_count"] == 1 and opinion_count == 2, str(opinion_count))

# ---------------------------------------------------------------- 12. 既有能力不受影响
print("12) 既有案卷/版本/核验接口行为保持")
r = client.get(f"{API}/dossiers/REV-DOS-F")
check("案卷查询仍返回封存快照与摘要",
      r.status_code == 200 and r.json()["dossier"]["digest"] == DIG_F
      and r.json()["dossier"]["item_count"] == 3)
r = client.post(f"{API}/dossiers/REV-DOS-F/verify")
check("案卷摘要核验仍 match=true（复核不改封存内容）",
      r.status_code == 200 and r.json()["match"] is True, r.text)
check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
