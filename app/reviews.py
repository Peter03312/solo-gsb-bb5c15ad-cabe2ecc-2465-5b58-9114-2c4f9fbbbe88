"""已封存案卷的双人独立复核与第三人裁决（质控）。

规则（与 README 一致）：
  * 复核只依据案卷封存的版本快照：提交前必须核对案卷摘要（digest），
    与留存摘要不一致（或留存摘要重算不过）一律拒绝写入；
  * 每卷由两名**不同**复核人各提交一次逐份**完整清单**（review，按提交先后记 seq=0/1），
    意见提交后不可改；缺项、重复份项、卷外份项、重复复核人、越阶段提交
    整单拒绝，事务回滚不留下部分记录；
  * 逐份意见（通过/不通过）一致即形成该份终局结论；有分歧的份项只允许**未参与
    前两次复核的第三人**裁决（adjudication，一次覆盖全部分歧份项），裁决后案卷终局；
    任一份不通过则案卷不通过；
  * 复核单号 / 裁决单号全局唯一：同单号同内容重试 → 幂等返回原结果（200），
    同单号异内容 → 409 冲突；
  * 所有写事务由调用方以 BEGIN IMMEDIATE 开启，并发提交按已提交状态串行判定。
"""
import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .dossiers import compute_digest
from .schemas import AdjudicationIn, ReviewIn
from .service import DomainError, _fingerprint

# --------------------------------------------------------------------------- 指纹 / 摘要


def _review_fingerprint(dossier_id: str, data: ReviewIn) -> str:
    """复核请求指纹：对 items 提交顺序不敏感（按样本编号归一化排序）。"""
    items = sorted(
        ({"sample_code": it.sample_code, "passed": it.passed, "fail_reason": it.fail_reason}
         for it in data.items),
        key=lambda x: x["sample_code"],
    )
    return _fingerprint({
        "dossier_id": dossier_id,
        "review_id": data.review_id,
        "reviewer": data.reviewer,
        "digest": data.digest,
        "items": items,
    })


def _adjudication_fingerprint(dossier_id: str, data: AdjudicationIn) -> str:
    items = sorted(
        ({"sample_code": it.sample_code, "passed": it.passed, "fail_reason": it.fail_reason}
         for it in data.items),
        key=lambda x: x["sample_code"],
    )
    return _fingerprint({
        "dossier_id": dossier_id,
        "adjudication_id": data.adjudication_id,
        "adjudicator": data.adjudicator,
        "digest": data.digest,
        "items": items,
    })


def _get_dossier(conn: sqlite3.Connection, dossier_id: str) -> sqlite3.Row:
    dossier = conn.execute(
        "SELECT * FROM dossier WHERE dossier_id = ?", (dossier_id,)
    ).fetchone()
    if dossier is None:
        raise DomainError(404, "dossier_not_found", f"案卷不存在: {dossier_id}")
    return dossier


def _dossier_items(conn: sqlite3.Connection, dossier_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM dossier_item WHERE dossier_pk = ? ORDER BY item_index ASC",
        (dossier_pk,),
    ).fetchall()


def _check_digest(conn: sqlite3.Connection, dossier: sqlite3.Row, incoming: str) -> None:
    """提交前核对案卷摘要：客户端摘要须等于留存摘要，留存摘要也须能按库存快照重算通过。

    任一不一致即拒绝写入（客户端拿到的不是封存版本，或库存内容被篡改）。
    """
    rows = _dossier_items(conn, dossier["id"])
    canonical_items = [
        {"sample_code": r["sample_code"], "version": r["version"],
         "snapshot": json.loads(r["snapshot_json"])}
        for r in rows
    ]
    recomputed = compute_digest(dossier["dossier_id"], canonical_items)
    stored = dossier["digest"]
    if incoming != stored or recomputed != stored:
        raise DomainError(
            409,
            "digest_mismatch",
            f"案卷 {dossier['dossier_id']} 摘要核对不一致，拒绝写入"
            "（请以案卷封存的版本快照为准重新复核）",
            {"dossier_id": dossier["dossier_id"],
             "submitted_digest": incoming,
             "stored_digest": stored,
             "recomputed_digest": recomputed},
        )


def _validate_item_coverage(
    incoming: list[Any],
    dossier_codes: list[str],
    *,
    duplicate_code: str,
    missing_code: str,
    unknown_code: str,
    scope: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """校验提交清单对案卷清单的覆盖关系：不重复、不缺项、无卷外份项。

    返回 (样本编号 -> 提交项, 错误明细列表)。错误明细供 422 details.items 返回。
    scope 仅用于错误文案（"复核" / "裁决"）。
    """
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    by_code: dict[str, dict[str, Any]] = {}
    for idx, it in enumerate(incoming):
        code = it.sample_code
        if code in seen:
            errors.append({
                "index": idx, "sample_code": code, "code": duplicate_code,
                "message": f"{scope}清单中样本编号重复：{code}",
            })
            continue
        seen.add(code)
        if code not in dossier_codes:
            errors.append({
                "index": idx, "sample_code": code, "code": unknown_code,
                "message": f"样本 {code} 不在案卷封存清单中（{scope}只依据封存版本快照）",
            })
            continue
        by_code[code] = it
    for code in dossier_codes:
        if code not in seen:
            errors.append({
                "index": None, "sample_code": code, "code": missing_code,
                "message": f"{scope}清单缺项：缺少卷内样本 {code}（须提交完整清单）",
            })
    return by_code, errors


# --------------------------------------------------------------------------- 双人复核提交


def submit_review(conn: sqlite3.Connection, dossier_id: str, data: ReviewIn) -> tuple[dict[str, Any], bool]:
    """一名复核人对一卷提交一次完整逐份意见。返回 (复核进度视图, 是否幂等重试)。"""
    request_hash = _review_fingerprint(dossier_id, data)

    # 0) 同复核单号：同内容幂等 / 异内容冲突。优先于一切业务校验——
    #    已提交意见不可改，重试即使案卷已进入下一阶段也返回原结果。
    existing_review = conn.execute(
        "SELECT * FROM dossier_review WHERE review_id = ?", (data.review_id,)
    ).fetchone()
    if existing_review is not None:
        if existing_review["request_hash"] != request_hash:
            raise DomainError(
                409, "review_conflict",
                f"复核单号 {data.review_id} 已使用但提交内容不一致（意见提交后不可改）",
                {"review_id": data.review_id,
                 "existing_request_hash": existing_review["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        dossier = conn.execute(
            "SELECT * FROM dossier WHERE id = ?", (existing_review["dossier_pk"],)
        ).fetchone()
        return review_progress(conn, dossier), True

    dossier = _get_dossier(conn, dossier_id)

    # 1) 摘要核对（含库存重算）：不一致拒绝写入。
    _check_digest(conn, dossier, data.digest)

    # 2) 阶段校验：每卷至多两名复核人；裁决终局后亦不得再追加复核。
    prior_reviews = conn.execute(
        "SELECT * FROM dossier_review WHERE dossier_pk = ? ORDER BY seq ASC",
        (dossier["id"],),
    ).fetchall()
    prior_adj = conn.execute(
        "SELECT 1 FROM dossier_adjudication WHERE dossier_pk = ? LIMIT 1",
        (dossier["id"],),
    ).fetchone()
    if len(prior_reviews) >= 2 or prior_adj is not None:
        raise DomainError(
            409, "review_stage_violation",
            f"案卷 {dossier_id} 的双人复核阶段已结束（已提交 {len(prior_reviews)} 次复核"
            + ("，且第三人裁决已终局" if prior_adj is not None else "") + "），越阶段提交拒绝",
            {"dossier_id": dossier_id, "reviews_submitted": len(prior_reviews),
             "adjudicated": prior_adj is not None},
        )

    # 3) 复核人必须不同（重复复核人整单拒绝）。
    prior_reviewers = [r["reviewer"] for r in prior_reviews]
    if data.reviewer in prior_reviewers:
        raise DomainError(
            422, "review_rejected",
            f"复核人 {data.reviewer} 已提交过案卷 {dossier_id} 的复核，"
            "每卷须由两名不同复核人独立复核",
            {"dossier_id": dossier_id, "reviewer": data.reviewer,
             "items": [{"index": None, "sample_code": None, "code": "duplicate_reviewer",
                        "message": f"复核人重复：{data.reviewer}（前序复核人: {', '.join(prior_reviewers)}）"}]},
        )

    # 4) 清单覆盖校验：缺项 / 重复份项 / 卷外份项（此刻尚未写入，失败不留记录）。
    dossier_rows = _dossier_items(conn, dossier["id"])
    dossier_codes = [r["sample_code"] for r in dossier_rows]
    by_code, errors = _validate_item_coverage(
        data.items, dossier_codes,
        duplicate_code="duplicate_sample_in_review",
        missing_code="missing_review_item",
        unknown_code="unknown_review_item",
        scope="复核",
    )
    if errors:
        raise DomainError(
            422, "review_rejected",
            f"案卷 {dossier_id} 复核清单校验失败，整单拒绝且不留下部分记录",
            {"dossier_id": dossier_id, "review_id": data.review_id, "items": errors},
        )

    # 5) 复核头与逐份意见同一事务落库（seq 取已提交次数；唯一键兜底并发）。
    seq = len(prior_reviews)
    created_at = utcnow_iso()
    try:
        rc = conn.execute(
            "INSERT INTO dossier_review"
            " (review_id, dossier_pk, dossier_id, reviewer, seq, digest_checked, request_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (data.review_id, dossier["id"], dossier_id, data.reviewer, seq,
             data.digest, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发竞争：单号已被他事务占用，或 seq 槽位被抢先——按已提交状态重新判定。
        raced = conn.execute(
            "SELECT * FROM dossier_review WHERE review_id = ?", (data.review_id,)
        ).fetchone()
        if raced is not None:
            if raced["request_hash"] == request_hash:
                d2 = conn.execute("SELECT * FROM dossier WHERE id = ?", (raced["dossier_pk"],)).fetchone()
                return review_progress(conn, d2), True
            raise DomainError(409, "review_conflict",
                              f"复核单号 {data.review_id} 并发写入且内容冲突")
        raise DomainError(
            409, "review_stage_violation",
            f"案卷 {dossier_id} 复核名额在并发下已被占满，按已提交状态拒绝",
            {"dossier_id": dossier_id},
        )
    review_pk = rc.lastrowid
    for row in dossier_rows:
        it = by_code[row["sample_code"]]
        conn.execute(
            "INSERT INTO dossier_review_item"
            " (review_pk, dossier_pk, sample_code, item_index, passed, fail_reason)"
            " VALUES (?,?,?,?,?,?)",
            (review_pk, dossier["id"], row["sample_code"], row["item_index"],
             1 if it.passed else 0, it.fail_reason),
        )

    dossier = conn.execute("SELECT * FROM dossier WHERE id = ?", (dossier["id"],)).fetchone()
    return review_progress(conn, dossier), False


# --------------------------------------------------------------------------- 第三人裁决提交


def submit_adjudication(
    conn: sqlite3.Connection, dossier_id: str, data: AdjudicationIn
) -> tuple[dict[str, Any], bool]:
    """第三人对前两次复核的分歧份项提交一次完整裁决。返回 (复核进度视图, 是否幂等重试)。"""
    request_hash = _adjudication_fingerprint(dossier_id, data)

    # 0) 同裁决单号幂等 / 冲突优先。
    existing = conn.execute(
        "SELECT * FROM dossier_adjudication WHERE adjudication_id = ?", (data.adjudication_id,)
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise DomainError(
                409, "adjudication_conflict",
                f"裁决单号 {data.adjudication_id} 已使用但提交内容不一致（裁决终局不可改）",
                {"adjudication_id": data.adjudication_id,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        dossier = conn.execute(
            "SELECT * FROM dossier WHERE id = ?", (existing["dossier_pk"],)
        ).fetchone()
        return review_progress(conn, dossier), True

    dossier = _get_dossier(conn, dossier_id)

    # 1) 摘要核对。
    _check_digest(conn, dossier, data.digest)

    # 2) 阶段：必须已有两次不同复核，且尚未裁决。
    reviews = conn.execute(
        "SELECT * FROM dossier_review WHERE dossier_pk = ? ORDER BY seq ASC",
        (dossier["id"],),
    ).fetchall()
    existing_adj = conn.execute(
        "SELECT * FROM dossier_adjudication WHERE dossier_pk = ?", (dossier["id"],)
    ).fetchone()
    if existing_adj is not None:
        raise DomainError(
            409, "review_stage_violation",
            f"案卷 {dossier_id} 已由第三人裁决终局，不得再次裁决（越阶段提交拒绝）",
            {"dossier_id": dossier_id},
        )
    if len(reviews) < 2:
        raise DomainError(
            409, "review_stage_violation",
            f"案卷 {dossier_id} 尚未完成两名复核人独立复核（当前 {len(reviews)} 次），"
            "分歧裁决只能在两次复核之后提交",
            {"dossier_id": dossier_id, "reviews_submitted": len(reviews)},
        )

    # 4) 计算分歧份项集合；无分歧则裁决阶段从未开启（或已因意见一致而终局），越阶段拒绝。
    opinions = _load_opinions(conn, dossier["id"])
    dossier_rows = _dossier_items(conn, dossier["id"])

    def _is_disagreement(code: str) -> bool:
        info = _item_opinion_pair(opinions, code)
        return info is not None and info["disagreement"]

    disagreement_codes = [r["sample_code"] for r in dossier_rows if _is_disagreement(r["sample_code"])]
    if not disagreement_codes:
        raise DomainError(
            409, "review_stage_violation",
            f"案卷 {dossier_id} 两名复核人意见一致、全部份项已终局，无分歧可裁决"
            "（裁决只针对有分歧的份项）",
            {"dossier_id": dossier_id, "disagreement_samples": []},
        )

    # 5) 裁决人不得参与前两次复核。
    prior_reviewers = {r["reviewer"] for r in reviews}
    if data.adjudicator in prior_reviewers:
        raise DomainError(
            422, "adjudication_rejected",
            f"裁决人 {data.adjudicator} 参与过案卷 {dossier_id} 的前两次复核，"
            "分歧只能由未参与前两次复核的第三人裁决",
            {"dossier_id": dossier_id, "adjudicator": data.adjudicator,
             "items": [{"index": None, "sample_code": None, "code": "adjudicator_must_be_third",
                        "message": f"裁决人不得为前两次复核人: {', '.join(sorted(prior_reviewers))}"}]},
        )

    # 6) 裁决清单必须恰好覆盖分歧集合（缺项 / 重复 / 裁决无分歧份项整单拒绝）。
    by_code, errors = _validate_item_coverage(
        data.items, disagreement_codes,
        duplicate_code="duplicate_sample_in_adjudication",
        missing_code="missing_adjudication_item",
        unknown_code="item_without_disagreement",
        scope="裁决",
    )
    if errors:
        raise DomainError(
            422, "adjudication_rejected",
            f"案卷 {dossier_id} 裁决清单校验失败，整单拒绝且不留下部分记录",
            {"dossier_id": dossier_id, "adjudication_id": data.adjudication_id,
             "disagreement_samples": disagreement_codes, "items": errors},
        )

    # 7) 裁决头与逐份裁决同一事务落库（每卷至多一条裁决，唯一键兜底并发）。
    created_at = utcnow_iso()
    try:
        ac = conn.execute(
            "INSERT INTO dossier_adjudication"
            " (adjudication_id, dossier_pk, dossier_id, adjudicator, digest_checked, request_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (data.adjudication_id, dossier["id"], dossier_id, data.adjudicator,
             data.digest, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        raced = conn.execute(
            "SELECT * FROM dossier_adjudication WHERE adjudication_id = ?", (data.adjudication_id,)
        ).fetchone()
        if raced is not None:
            if raced["request_hash"] == request_hash:
                d2 = conn.execute("SELECT * FROM dossier WHERE id = ?", (raced["dossier_pk"],)).fetchone()
                return review_progress(conn, d2), True
            raise DomainError(409, "adjudication_conflict",
                              f"裁决单号 {data.adjudication_id} 并发写入且内容冲突")
        raise DomainError(
            409, "review_stage_violation",
            f"案卷 {dossier_id} 已在并发下被第三人裁决终局，按已提交状态拒绝",
            {"dossier_id": dossier_id},
        )
    adj_pk = ac.lastrowid
    for row in dossier_rows:
        if row["sample_code"] not in by_code:
            continue
        it = by_code[row["sample_code"]]
        conn.execute(
            "INSERT INTO dossier_adjudication_item"
            " (adjudication_pk, dossier_pk, sample_code, item_index, passed, fail_reason)"
            " VALUES (?,?,?,?,?,?)",
            (adj_pk, dossier["id"], row["sample_code"], row["item_index"],
             1 if it.passed else 0, it.fail_reason),
        )

    dossier = conn.execute("SELECT * FROM dossier WHERE id = ?", (dossier["id"],)).fetchone()
    return review_progress(conn, dossier), False


# --------------------------------------------------------------------------- 查询


def _load_opinions(conn: sqlite3.Connection, dossier_pk: int) -> dict[str, list[dict[str, Any]]]:
    """sample_code -> 各次复核意见（按 seq 升序）。"""
    rows = conn.execute(
        "SELECT ri.sample_code AS sample_code, ri.passed AS passed, ri.fail_reason AS fail_reason,"
        "       r.review_id AS review_id, r.reviewer AS reviewer, r.seq AS seq,"
        "       r.created_at AS created_at"
        " FROM dossier_review_item ri JOIN dossier_review r ON ri.review_pk = r.id"
        " WHERE r.dossier_pk = ? ORDER BY r.seq ASC, ri.item_index ASC",
        (dossier_pk,),
    ).fetchall()
    opinions: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        opinions.setdefault(r["sample_code"], []).append({
            "review_id": r["review_id"], "reviewer": r["reviewer"], "seq": r["seq"],
            "passed": bool(r["passed"]), "fail_reason": r["fail_reason"],
            "created_at": r["created_at"],
        })
    return opinions


def _item_opinion_pair(opinions: dict[str, list[dict[str, Any]]], code: str) -> dict[str, Any] | None:
    """两次意见齐备时返回 {agreement, disagreement, pair}；否则 None。"""
    pair = opinions.get(code, [])
    if len(pair) < 2:
        return None
    return {"pair": pair, "agreement": pair[0]["passed"] == pair[1]["passed"],
            "disagreement": pair[0]["passed"] != pair[1]["passed"]}


def _review_view(conn: sqlite3.Connection, review: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM dossier_review_item WHERE review_pk = ? ORDER BY item_index ASC",
        (review["id"],),
    ).fetchall()
    return {
        "review_id": review["review_id"],
        "seq": review["seq"],
        "reviewer": review["reviewer"],
        "digest_checked": review["digest_checked"],
        "created_at": review["created_at"],
        "items": [
            {"index": r["item_index"], "sample_code": r["sample_code"],
             "passed": bool(r["passed"]), "fail_reason": r["fail_reason"]}
            for r in items
        ],
    }


def _adjudication_view(conn: sqlite3.Connection, adj: sqlite3.Row | None) -> dict[str, Any] | None:
    if adj is None:
        return None
    items = conn.execute(
        "SELECT * FROM dossier_adjudication_item WHERE adjudication_pk = ? ORDER BY item_index ASC",
        (adj["id"],),
    ).fetchall()
    return {
        "adjudication_id": adj["adjudication_id"],
        "adjudicator": adj["adjudicator"],
        "digest_checked": adj["digest_checked"],
        "created_at": adj["created_at"],
        "items": [
            {"index": r["item_index"], "sample_code": r["sample_code"],
             "passed": bool(r["passed"]), "fail_reason": r["fail_reason"]}
            for r in items
        ],
    }


def review_progress(conn: sqlite3.Connection, dossier: sqlite3.Row) -> dict[str, Any]:
    """组装案卷复核进度视图：复核进度、原始意见、分歧明细、第三人裁决与终局结论。

    stage 取值：
      awaiting_first      尚无复核
      awaiting_second     已有一次复核，等待第二名不同复核人
      awaiting_adjudication 两次复核存在分歧，等待第三人裁决
      completed           全部份项终局（含案卷通过/不通过结论）
    """
    dossier_rows = _dossier_items(conn, dossier["id"])
    dossier_codes = [r["sample_code"] for r in dossier_rows]

    review_rows = conn.execute(
        "SELECT * FROM dossier_review WHERE dossier_pk = ? ORDER BY seq ASC",
        (dossier["id"],),
    ).fetchall()
    reviews = [_review_view(conn, r) for r in review_rows]
    adj_row = conn.execute(
        "SELECT * FROM dossier_adjudication WHERE dossier_pk = ?", (dossier["id"],)
    ).fetchone()
    adjudication = _adjudication_view(conn, adj_row)
    adj_by_code: dict[str, dict[str, Any]] = {}
    if adjudication is not None:
        adj_by_code = {it["sample_code"]: it for it in adjudication["items"]}

    opinions = _load_opinions(conn, dossier["id"])

    disagreements: list[dict[str, Any]] = []
    final_items: list[dict[str, Any]] = []
    finalized = 0
    for row in dossier_rows:
        code = row["sample_code"]
        pair_info = _item_opinion_pair(opinions, code)
        pair = pair_info["pair"] if pair_info else opinions.get(code, [])

        if pair_info is not None and pair_info["disagreement"]:
            disagreements.append({
                "index": row["item_index"], "sample_code": code,
                "opinions": pair,
                "adjudication": adj_by_code.get(code),
            })

        if pair_info is not None and pair_info["agreement"]:
            finalized += 1
            passed = pair[0]["passed"]
            final_items.append({
                "index": row["item_index"], "sample_code": code,
                "passed": passed,
                "fail_reason": pair[0]["fail_reason"] if not passed else None,
                "decided_by": "review",
                "decided_by_id": None,
                "opinions": pair,
            })
        elif code in adj_by_code:
            finalized += 1
            adj_item = adj_by_code[code]
            final_items.append({
                "index": row["item_index"], "sample_code": code,
                "passed": adj_item["passed"],
                "fail_reason": adj_item["fail_reason"],
                "decided_by": "adjudication",
                "decided_by_id": adjudication["adjudication_id"],
                "opinions": pair + [{
                    "review_id": adjudication["adjudication_id"],
                    "reviewer": adjudication["adjudicator"], "seq": "adjudication",
                    "passed": adj_item["passed"], "fail_reason": adj_item["fail_reason"],
                    "created_at": adjudication["created_at"],
                }],
            })

    if len(reviews) == 0:
        stage = "awaiting_first"
    elif len(reviews) == 1:
        stage = "awaiting_second"
    elif disagreements and adjudication is None:
        stage = "awaiting_adjudication"
    else:
        stage = "completed"

    completed = stage == "completed"
    failed_final = [it for it in final_items if not it["passed"]]
    conclusion = None
    if completed:
        if disagreements:
            decided_at = adjudication["created_at"]
        else:
            decided_at = reviews[-1]["created_at"]
        conclusion = {
            "passed": len(failed_final) == 0,
            "decided_at": decided_at,
            "total_items": len(dossier_codes),
            "passed_items": len(dossier_codes) - len(failed_final),
            "failed_items": len(failed_final),
            "failed_samples": [it["sample_code"] for it in failed_final],
            "adjudicated_samples": [it["sample_code"] for it in disagreements],
        }

    return {
        "dossier_id": dossier["dossier_id"],
        "digest": dossier["digest"],
        "item_count": len(dossier_codes),
        "stage": stage,
        "completed": completed,
        "progress": {
            "reviews_submitted": len(reviews),
            "required_reviews": 2,
            "reviewers": [r["reviewer"] for r in reviews],
            "disagreement_count": len(disagreements),
            "finalized_items": finalized,
            "total_items": len(dossier_codes),
            "adjudication_submitted": adjudication is not None,
        },
        "reviews": reviews,
        "disagreements": disagreements,
        "adjudication": adjudication,
        "final_items": final_items,
        "conclusion": conclusion,
    }


def get_review_progress(conn: sqlite3.Connection, dossier_id: str) -> dict[str, Any]:
    dossier = _get_dossier(conn, dossier_id)
    return review_progress(conn, dossier)


def get_single_review(conn: sqlite3.Connection, review_id: str) -> dict[str, Any]:
    """按复核单号查询：返回该次原始意见及其所属案卷的当前复核进度。"""
    review = conn.execute(
        "SELECT * FROM dossier_review WHERE review_id = ?", (review_id,)
    ).fetchone()
    if review is None:
        raise DomainError(404, "review_not_found", f"复核单号不存在: {review_id}")
    dossier = conn.execute(
        "SELECT * FROM dossier WHERE id = ?", (review["dossier_pk"],)
    ).fetchone()
    return {"review": _review_view(conn, review), "progress": review_progress(conn, dossier)}
