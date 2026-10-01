"""案卷双人独立复核与第三人裁决（质控）。

流程（与 README 一致）：
  1. 复核只依据案卷封存时的版本快照：每次提交必须携带 expected_digest，
     与案卷留存的 SHA-256 摘要核对，不一致拒绝写入（review_digest_mismatch）。
  2. 每卷由两名不同复核人各提交一次完整清单（seq=1/2，kind=review）：
     清单必须恰好覆盖卷内每份样本（缺项 / 重复份项 / 未知份项拒绝，且不留部分记录）。
  3. 两份意见都到齐后，逐份比对：结论（通过/不通过）一致即形成该份终局结论；
     结论不一致，或均不通过但原因不同 → 该份为分歧份项。
  4. 有分歧的份项只允许未参与前两次复核的第三人裁决（seq=3，kind=adjudication）：
     裁决人不得是前两名复核人之一；裁决清单必须恰好覆盖截止提交时的全部分歧份项。
  5. 全部份项终局后形成案卷终局结论：任一份终局不通过则案卷不通过；
     终局在全部份项终局的那次提交内一次性固化，此后拒绝一切提交（越阶段）。

幂等 / 并发 / 原子：
  * 同复核单号同内容重试 → 幂等返回原结果（200）；同复核单号异内容 → 409 review_conflict；
  * 全部校验先于任何写入，失败不留下部分记录；
  * 写事务以 BEGIN IMMEDIATE 串行：并发提交按已提交状态判定
    （seq / 复核人 / 阶段均以事务内读到的已提交数据为准，唯一键兜底）。
"""
import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import ReviewIn
from .service import DomainError, _fingerprint

# 提交次序与类型
KIND_REVIEW = "review"
KIND_ADJUDICATION = "adjudication"


# --------------------------------------------------------------------------- 工具

def _request_fingerprint(data: ReviewIn) -> str:
    """复核提交请求指纹（同复核单号幂等/冲突判定）。

    对 items 提交顺序不敏感（按样本编号归一化排序）；纳入案卷摘要与全部输入字段。
    """
    items = sorted(
        ({"sample_code": it.sample_code, "passed": it.passed,
          "reason": it.reason.strip() if it.reason else None}
         for it in data.items),
        key=lambda x: x["sample_code"],
    )
    canonical = {
        "review_no": data.review_no,
        "reviewer": data.reviewer,
        "expected_digest": data.expected_digest.lower(),
        "items": items,
    }
    return _fingerprint(canonical)


def _get_dossier_or_404(conn: sqlite3.Connection, dossier_id: str) -> sqlite3.Row:
    dossier = conn.execute(
        "SELECT * FROM dossier WHERE dossier_id = ?", (dossier_id,)
    ).fetchone()
    if dossier is None:
        raise DomainError(404, "dossier_not_found", f"案卷不存在: {dossier_id}")
    return dossier


def _dossier_items(conn: sqlite3.Connection, dossier_pk: int) -> list[sqlite3.Row]:
    """案卷封存清单（按样本编号排序后的 item_index 升序）。"""
    return conn.execute(
        "SELECT * FROM dossier_item WHERE dossier_pk = ? ORDER BY item_index ASC",
        (dossier_pk,),
    ).fetchall()


def _existing_reviews(conn: sqlite3.Connection, case_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review WHERE case_pk = ? ORDER BY seq ASC", (case_pk,)
    ).fetchall()


def _opinions_by_review(conn: sqlite3.Connection, review_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_opinion WHERE review_pk = ? ORDER BY item_index ASC",
        (review_pk,),
    ).fetchall()


def _opinion_map(conn: sqlite3.Connection, review_pk: int) -> dict[str, sqlite3.Row]:
    return {r["sample_code"]: r for r in _opinions_by_review(conn, review_pk)}


def _disputed_samples(
    conn: sqlite3.Connection, review_pk1: int, review_pk2: int, item_rows: list[sqlite3.Row]
) -> set[str]:
    """两份独立复核后的分歧份项集合。

    一致（形成终局）：两份均通过；或两份均不通过且原因文本一致。
    分歧：结论不同；或均不通过但原因不同。
    """
    m1, m2 = _opinion_map(conn, review_pk1), _opinion_map(conn, review_pk2)
    disputed: set[str] = set()
    for it in item_rows:
        code = it["sample_code"]
        o1, o2 = m1[code], m2[code]
        if bool(o1["passed"]) != bool(o2["passed"]):
            disputed.add(code)
        elif not o1["passed"] and o1["reason"] != o2["reason"]:
            disputed.add(code)
    return disputed


def _item_decisions(
    conn: sqlite3.Connection,
    item_rows: list[sqlite3.Row],
    reviews: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    """逐份计算复核状态、原始意见、分歧与终局结论。

    每次查询实时重算（意见为追加不可变，故结果确定）；终局原因：
      * 一致通过 → passed=true，reason=null；
      * 一致不通过且原因相同 → passed=false，reason=共同原因；
      * 第三人裁决 → 以裁决意见为准。
    """
    review_rows = {r["seq"]: r for r in reviews}
    maps: dict[int, dict[str, sqlite3.Row]] = {
        seq: _opinion_map(conn, r["id"]) for seq, r in review_rows.items()
    }
    decisions: list[dict[str, Any]] = []
    for it in item_rows:
        code = it["sample_code"]
        opinions: list[dict[str, Any]] = []
        for seq in sorted(review_rows):
            row = maps[seq].get(code)
            if row is None:
                continue
            rv = review_rows[seq]
            opinions.append({
                "seq": seq,
                "kind": rv["kind"],
                "review_no": rv["review_no"],
                "reviewer": rv["reviewer"],
                "passed": bool(row["passed"]),
                "reason": row["reason"],
                "submitted_at": rv["created_at"],
            })

        status: str
        final_passed: bool | None = None
        final_reason: str | None = None
        final_source: str | None = None
        adj: sqlite3.Row | None = review_rows.get(3)

        if 1 not in review_rows or 2 not in review_rows:
            status = "pending"                                   # 两份独立复核尚未到齐
        else:
            m1, m2 = maps[1], maps[2]
            o1, o2 = m1[code], m2[code]
            if bool(o1["passed"]) != bool(o2["passed"]):
                agree = False
                same_fail_reason = False
            else:
                agree = True
                same_fail_reason = bool(o1["passed"]) or o1["reason"] == o2["reason"]

            if same_fail_reason:
                status = "final"
                final_passed = bool(o1["passed"])
                final_reason = None if o1["passed"] else o1["reason"]
                final_source = "agreement"
            elif adj is not None and code in maps[3]:
                status = "final"
                a = maps[3][code]
                final_passed = bool(a["passed"])
                final_reason = a["reason"]
                final_source = "adjudication"
            else:
                status = "disputed"

        decisions.append({
            "index": it["item_index"],
            "sample_code": code,
            "version": it["version"],
            "status": status,                                    # pending/disputed/final
            "opinions": opinions,
            "disagreement": None if status != "disputed" else _disagreement_detail(
                maps[1][code], maps[2][code],
                review_rows[1]["reviewer"], review_rows[2]["reviewer"]),
            "final": None if status != "final" else {
                "passed": final_passed,
                "reason": final_reason,
                "source": final_source,
            },
        })
    return decisions


def _disagreement_detail(
    o1: sqlite3.Row, o2: sqlite3.Row, reviewer1: str, reviewer2: str
) -> dict[str, Any]:
    """分歧份项的原始意见对照（结论分歧 / 不通过原因分歧）。"""
    if bool(o1["passed"]) != bool(o2["passed"]):
        dtype = "pass_conflict"
    else:
        dtype = "reason_conflict"
    return {
        "type": dtype,
        "reviewers": [reviewer1, reviewer2],
        "opinions": [
            {"reviewer": reviewer1, "passed": bool(o1["passed"]), "reason": o1["reason"]},
            {"reviewer": reviewer2, "passed": bool(o2["passed"]), "reason": o2["reason"]},
        ],
    }


def _progress_stage(reviews: list[sqlite3.Row]) -> str:
    """案卷复核整体阶段（与进度计算一致）。"""
    kinds = {r["kind"] for r in reviews}
    if KIND_ADJUDICATION in kinds:
        return "adjudicated"
    if len(reviews) >= 2:
        return "both_reviewed"
    if len(reviews) == 1:
        return "first_reviewed"
    return "awaiting_review"


def build_case_view(conn: sqlite3.Connection, dossier: sqlite3.Row, case_row: sqlite3.Row | None) -> dict[str, Any]:
    """组装案卷复核视图：进度、原始意见、分歧、终局结论。case_row 为 None 表示尚未有任何提交。"""
    item_rows = _dossier_items(conn, dossier["id"])
    reviews = _existing_reviews(conn, case_row["id"]) if case_row is not None else []
    decisions = _item_decisions(conn, item_rows, reviews)

    total = len(item_rows)
    final_items = [d for d in decisions if d["status"] == "final"]
    disputed_items = [d for d in decisions if d["status"] == "disputed"]
    pending_items = [d for d in decisions if d["status"] == "pending"]
    all_final = len(final_items) == total and total > 0
    final_payload = json.loads(case_row["final_json"]) if case_row is not None and case_row["final_json"] else None

    if final_payload is not None:
        stage = "completed"
    elif any(r["kind"] == KIND_ADJUDICATION for r in reviews) and disputed_items:
        # 已有部分裁决但仍有未覆盖分歧（正常事务内不会停留；仅作防御）
        stage = "adjudicating"
    elif disputed_items:
        stage = "awaiting_adjudication"
    else:
        stage = _progress_stage(reviews)

    return {
        "dossier_id": dossier["dossier_id"],
        "digest": dossier["digest"],
        "stage": stage,                                         # awaiting_review/first_reviewed/
                                                                # both_reviewed/awaiting_adjudication/
                                                                # adjudicating/completed
        "review_count": len(reviews),
        "total_items": total,
        "final_item_count": len(final_items),
        "disputed_item_count": len(disputed_items),
        "pending_item_count": len(pending_items),
        "all_items_final": all_final,
        "reviewers": [
            {"seq": r["seq"], "kind": r["kind"], "reviewer": r["reviewer"],
             "review_no": r["review_no"], "submitted_at": r["created_at"]}
            for r in reviews
        ],
        "items": decisions,
        "disputed_samples": [d["sample_code"] for d in disputed_items],
        "conclusion": final_payload,                            # 案卷终局结论（未终局为 null）
        "finalized_at": case_row["finalized_at"] if case_row is not None else None,
    }


def _submission_view(review_row: sqlite3.Row, opinions: list[sqlite3.Row]) -> dict[str, Any]:
    return {
        "review_no": review_row["review_no"],
        "seq": review_row["seq"],
        "kind": review_row["kind"],
        "reviewer": review_row["reviewer"],
        "submitted_at": review_row["created_at"],
        "items": [
            {"sample_code": o["sample_code"], "passed": bool(o["passed"]), "reason": o["reason"]}
            for o in opinions
        ],
    }


def _validate_full_list(
    data: ReviewIn, item_rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    """校验完整清单（前两次独立复核）：重复份项 / 未知份项 / 缺项。返回问题明细（空=通过）。"""
    issues: list[dict[str, Any]] = []
    code_to_index = {it["sample_code"]: it["item_index"] for it in item_rows}
    seen: dict[str, int] = {}
    for idx, it in enumerate(data.items):
        if it.sample_code in seen:
            issues.append({
                "index": idx, "sample_code": it.sample_code,
                "code": "duplicate_sample_in_review",
                "message": f"清单内样本编号重复：{it.sample_code}"
                           f"（第 {seen[it.sample_code] + 1} 份与第 {idx + 1} 份）",
            })
        else:
            seen[it.sample_code] = idx
        if it.sample_code not in code_to_index:
            issues.append({
                "index": idx, "sample_code": it.sample_code,
                "code": "unknown_sample_in_review",
                "message": f"样本 {it.sample_code} 不在案卷封存清单中",
            })
    missing = [code for code in code_to_index if code not in seen]
    for code in sorted(missing, key=lambda c: code_to_index[c]):
        issues.append({
            "sample_code": code, "code": "missing_sample_in_review",
            "message": f"清单缺少卷内样本：{code}",
        })
    return issues


def _validate_adjudication_list(
    data: ReviewIn, item_rows: list[sqlite3.Row], disputed: set[str]
) -> list[dict[str, Any]]:
    """校验裁决清单：重复份项 / 未知份项 / 越权份项（非分歧）/ 漏裁分歧份项。"""
    issues: list[dict[str, Any]] = []
    code_to_index = {it["sample_code"]: it["item_index"] for it in item_rows}
    seen: dict[str, int] = {}
    submitted_codes: list[str] = []
    for idx, it in enumerate(data.items):
        if it.sample_code in seen:
            issues.append({
                "index": idx, "sample_code": it.sample_code,
                "code": "duplicate_sample_in_review",
                "message": f"裁决清单内样本编号重复：{it.sample_code}",
            })
        else:
            seen[it.sample_code] = idx
        submitted_codes.append(it.sample_code)
        if it.sample_code not in code_to_index:
            issues.append({
                "index": idx, "sample_code": it.sample_code,
                "code": "unknown_sample_in_review",
                "message": f"样本 {it.sample_code} 不在案卷封存清单中",
            })
        elif it.sample_code not in disputed:
            issues.append({
                "index": idx, "sample_code": it.sample_code,
                "code": "adjudication_not_allowed",
                "message": f"样本 {it.sample_code} 不是分歧份项，第三人只能裁决有分歧的份项",
            })
    for code in sorted(disputed, key=lambda c: code_to_index[c]):
        if code not in seen:
            issues.append({
                "sample_code": code, "code": "missing_adjudication",
                "message": f"裁决清单缺少分歧份项：{code}",
            })
    return issues


# --------------------------------------------------------------------------- 提交

def submit_review(conn: sqlite3.Connection, dossier_id: str, data: ReviewIn) -> tuple[dict[str, Any], bool]:
    """提交一次复核/裁决。返回 (提交结果视图, 是否幂等重试)。

    全部业务校验先于任何写入；失败整体回滚，不留下复核案、提交或逐份意见。
    """
    request_hash = _request_fingerprint(data)

    # 1) 案卷必须存在
    dossier = _get_dossier_or_404(conn, dossier_id)
    item_rows = _dossier_items(conn, dossier["id"])

    # 2) 摘要核对（复核只依据案卷封存的版本快照）。同复核单号重试先做幂等判定：
    #    原提交本就核对通过，故重试不因摘要比对口径被拦。
    case_row = conn.execute(
        "SELECT * FROM review_case WHERE dossier_pk = ?", (dossier["id"],)
    ).fetchone()
    existing_no = conn.execute(
        "SELECT rv.* FROM review rv"
        " JOIN review_case rc ON rv.case_pk = rc.id"
        " WHERE rv.review_no = ? AND rc.dossier_pk = ?",
        (data.review_no, dossier["id"]),
    ).fetchone()
    if existing_no is not None:
        if existing_no["request_hash"] != request_hash:
            raise DomainError(
                409,
                "review_conflict",
                f"复核单号 {data.review_no} 已使用但提交内容不一致（同复核单号异内容冲突），意见不可改",
                {"review_no": data.review_no,
                 "existing_request_hash": existing_no["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        opinions = _opinions_by_review(conn, existing_no["id"])
        return {
            "submission": _submission_view(existing_no, opinions),
            "review": build_case_view(conn, dossier, case_row),
        }, True

    # 同复核单号在别的案卷使用 → 全局唯一冲突
    other = conn.execute(
        "SELECT 1 FROM review WHERE review_no = ?", (data.review_no,)
    ).fetchone()
    if other is not None:
        raise DomainError(
            409,
            "review_conflict",
            f"复核单号 {data.review_no} 已被其他案卷占用（复核单号全局唯一）",
            {"review_no": data.review_no},
        )

    if data.expected_digest.strip().lower() != dossier["digest"].strip().lower():
        raise DomainError(
            422,
            "review_digest_mismatch",
            "提交的案卷摘要与封存摘要不一致，拒绝写入（复核只依据案卷封存的版本快照，"
            "请重新获取案卷清单与快照后再提交）",
            {"dossier_id": dossier_id,
             "expected_digest": data.expected_digest,
             "stored_digest": dossier["digest"]},
        )

    # 3) 阶段判定（基于事务内已提交状态；IMMEDIATE 串行，并发按已提交状态判定）
    reviews = _existing_reviews(conn, case_row["id"]) if case_row is not None else []
    if case_row is not None and case_row["finalized_at"] is not None:
        raise DomainError(
            409,
            "review_stage_rejected",
            "案卷已形成终局结论，意见提交后不可改，拒绝越阶段提交",
            {"dossier_id": dossier_id, "stage": "completed", "finalized_at": case_row["finalized_at"]},
        )

    review_kinds = [r["kind"] for r in reviews]
    adj_done = KIND_ADJUDICATION in review_kinds
    n_reviews = sum(1 for k in review_kinds if k == KIND_REVIEW)

    if adj_done:
        raise DomainError(
            409,
            "review_stage_rejected",
            "第三人裁决已提交，案卷复核流程已结束，拒绝越阶段提交",
            {"dossier_id": dossier_id, "stage": "adjudicated"},
        )

    if n_reviews < 2:
        kind = KIND_REVIEW
        seq = n_reviews + 1
        # 重复复核人：两名复核人必须不同（同一复核人不得提交两次）
        prior_reviewers = {r["reviewer"] for r in reviews if r["kind"] == KIND_REVIEW}
        if data.reviewer in prior_reviewers:
            raise DomainError(
                409,
                "duplicate_reviewer",
                f"复核人 {data.reviewer} 已提交过该案卷的独立复核，两名复核人必须不同",
                {"dossier_id": dossier_id, "reviewer": data.reviewer,
                 "prior_reviewers": sorted(prior_reviewers)},
            )
        issues = _validate_full_list(data, item_rows)
        if issues:
            raise DomainError(
                422,
                "review_rejected",
                "独立复核清单必须恰好覆盖卷内每份样本（不缺项、不重复份项、不越卷），整单拒绝且不留记录",
                {"dossier_id": dossier_id, "items": issues},
            )
    else:
        # 前两份已到齐 → 仅允许第三人裁决
        kind = KIND_ADJUDICATION
        seq = 3
        r1 = next(r for r in reviews if r["seq"] == 1)
        r2 = next(r for r in reviews if r["seq"] == 2)
        prior_reviewers = {r1["reviewer"], r2["reviewer"]}
        if data.reviewer in prior_reviewers:
            raise DomainError(
                409,
                "duplicate_reviewer",
                f"裁决人 {data.reviewer} 参与过该案卷的前两次复核，分歧份项只允许未参与前两次复核的第三人裁决",
                {"dossier_id": dossier_id, "reviewer": data.reviewer,
                 "prior_reviewers": sorted(prior_reviewers)},
            )
        disputed = _disputed_samples(conn, r1["id"], r2["id"], item_rows)
        if not disputed:
            raise DomainError(
                409,
                "review_stage_rejected",
                "两份独立复核逐份意见一致、无分歧份项，无需第三人裁决（案卷结论已可形成）",
                {"dossier_id": dossier_id, "stage": "both_reviewed"},
            )
        issues = _validate_adjudication_list(data, item_rows, disputed)
        if issues:
            raise DomainError(
                422,
                "review_rejected",
                "裁决清单必须恰好覆盖全部分歧份项（不缺项、不重复、不裁决非分歧份项），整单拒绝且不留记录",
                {"dossier_id": dossier_id, "items": issues,
                 "disputed_samples": sorted(disputed)},
            )

    # 4) 全部校验通过后才写入（复核案惰性创建；头、提交、逐份意见同一事务）。
    created_at = utcnow_iso()
    if case_row is None:
        cc = conn.execute(
            "INSERT INTO review_case (dossier_pk, digest, final_json, finalized_at, created_at)"
            " VALUES (?,?,NULL,NULL,?)",
            (dossier["id"], dossier["digest"], created_at),
        )
        case_pk = cc.lastrowid
    else:
        case_pk = case_row["id"]

    try:
        rc = conn.execute(
            "INSERT INTO review (case_pk, review_no, seq, kind, reviewer, request_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (case_pk, data.review_no, seq, kind, data.reviewer, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发兜底：唯一键竞争（同 seq/同复核人/同复核单号）→ 按已提交状态拒绝，事务回滚
        raise DomainError(
            409,
            "review_conflict",
            f"复核提交与并发事务冲突（复核单号/次序/复核人已被占用），按已提交状态判定",
            {"dossier_id": dossier_id, "review_no": data.review_no},
        )
    review_pk = rc.lastrowid

    code_to_item = {it["sample_code"]: it for it in item_rows}
    for it in data.items:
        ditem = code_to_item[it.sample_code]
        conn.execute(
            "INSERT INTO review_opinion"
            " (review_pk, dossier_item_pk, sample_code, item_index, passed, reason)"
            " VALUES (?,?,?,?,?,?)",
            (review_pk, ditem["id"], it.sample_code, ditem["item_index"],
             1 if it.passed else 0, it.reason.strip() if it.reason else None),
        )

    # 5) 重算逐份终局；全部份项终局 → 一次性固化案卷结论（任一份不通过则案卷不通过）。
    case_row = conn.execute("SELECT * FROM review_case WHERE id = ?", (case_pk,)).fetchone()
    reviews_after = _existing_reviews(conn, case_pk)
    decisions = _item_decisions(conn, item_rows, reviews_after)
    if all(d["status"] == "final" for d in decisions):
        final_passed = all(d["final"]["passed"] for d in decisions)
        final_payload = {
            "passed": final_passed,
            "status": "passed" if final_passed else "rejected",
            "reason": "全部份项终局通过" if final_passed
                      else "存在终局不通过份项，案卷不通过",
            "failed_samples": [d["sample_code"] for d in decisions if not d["final"]["passed"]],
        }
        conn.execute(
            "UPDATE review_case SET final_json = ?, finalized_at = ? WHERE id = ? AND finalized_at IS NULL",
            (json.dumps(final_payload, ensure_ascii=False, separators=(",", ":")), created_at, case_pk),
        )

    case_row = conn.execute("SELECT * FROM review_case WHERE id = ?", (case_pk,)).fetchone()
    review_row = conn.execute("SELECT * FROM review WHERE id = ?", (review_pk,)).fetchone()
    return {
        "submission": _submission_view(review_row, _opinions_by_review(conn, review_pk)),
        "review": build_case_view(conn, dossier, case_row),
    }, False


def get_review(conn: sqlite3.Connection, dossier_id: str) -> dict[str, Any]:
    """案卷复核查询：复核进度、原始意见、分歧及终局结论（未提交过也可查，阶段 awaiting_review）。"""
    dossier = _get_dossier_or_404(conn, dossier_id)
    case_row = conn.execute(
        "SELECT * FROM review_case WHERE dossier_pk = ?", (dossier["id"],)
    ).fetchone()
    return build_case_view(conn, dossier, case_row)
