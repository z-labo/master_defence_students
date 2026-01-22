# app.py
import os
import json
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
import dropbox


# -----------------------------
# Config
# -----------------------------
DROPBOX_TOKEN = os.environ.get("DROPBOX_TOKEN")  # Render의 Environment에 설정
DROPBOX_BASE_FOLDER = os.environ.get("DROPBOX_BASE_FOLDER", "")  # 기본값
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://z-labo.github.io"  # GitHub Pages 도메인
).split(",")

if not DROPBOX_TOKEN:
    raise RuntimeError("환경변수 DROPBOX_TOKEN 이 설정되어 있지 않습니다.")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": [o.strip() for o in ALLOWED_ORIGINS]}})


def get_dbx() -> dropbox.Dropbox:
    return dropbox.Dropbox(DROPBOX_TOKEN)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_payload(payload: dict) -> tuple[bool, str]:
    # 최소 검증 (서버가 깨지지 않게)
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"
    if "evaluatorName" not in payload or not isinstance(payload["evaluatorName"], str) or not payload["evaluatorName"].strip():
        return False, "Evaluator name is required"
    if "results" not in payload or not isinstance(payload["results"], list) or len(payload["results"]) == 0:
        return False, "results must be a non-empty list"

    for r in payload["results"]:
        if not isinstance(r, dict):
            return False, "each result must be an object"
        if "presenterId" not in r or not isinstance(r["presenterId"], str) or not r["presenterId"].strip():
            return False, "presenterId is required"
        if "score" not in r:
            return False, "score is required"

        score = r["score"]
        if not isinstance(score, int) or score < 0 or score > 5:
            return False, "score must be an integer 0..5"

        # comment는 선택
        if "comment" in r and r["comment"] is not None and not isinstance(r["comment"], str):
            return False, "comment must be a string"

    return True, ""


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": utc_now_iso()})


@app.post("/submit_vote")
def submit_vote():
    payload = request.get_json(silent=True)

    ok, msg = validate_payload(payload)
    if not ok:
        return jsonify({"error": msg}), 400

    evaluator_name = payload["evaluatorName"].strip()

    # 저장 파일명: evaluatorName별로 덮어쓰기(HTML의 "final vote만" 정책과 일치)
    # 예: /vote_results/evaluatorName.json
    base = (DROPBOX_BASE_FOLDER or "").rstrip("/")
    folder = f"{base}/vote_results" if base else "/vote_results"
    dropbox_path = f"{folder}/{evaluator_name}.json"

    # 서버에서 저장 시각을 별도로 찍어두면 추후 감사/디버깅에 유리
    payload_server = dict(payload)
    payload_server["serverReceivedAt"] = utc_now_iso()

    data_bytes = json.dumps(payload_server, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        dbx = get_dbx()
        dbx.files_upload(
            data_bytes,
            dropbox_path,
            mode=dropbox.files.WriteMode.overwrite,
            mute=True
        )
    except Exception as e:
        # Render 로그에 에러가 남도록 문자열 포함
        return jsonify({"error": "dropbox upload failed", "detail": str(e)}), 500

    return jsonify({"ok": True, "path": dropbox_path})


@app.route("/", methods=["GET", "POST", "OPTIONS"])
def root():
    if request.method == "OPTIONS":
        return ("", 204)  # preflight 통과
    if request.method == "GET":
        return "OK", 200  # 헬스체크용
    # POST로 들어오면 기존 submit_vote 로직을 호출하거나,
    # 아니면 명확히 404/400을 주되 CORS 헤더는 after_request로 붙게 두기
    return jsonify({"ok": False, "error": "POST / is not supported. Use /submit_vote"}), 400


def load_all_votes_from_dropbox():

    dbx = get_dbx()
    records = []

    # 폴더 목록 가져오기
    folder = f"{DROPBOX_BASE_FOLDER.rstrip('/')}/vote_results"
    res = dbx.files_list_folder(folder)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)

    for e in entries:
        # 파일만 대상으로, 확장자가 .json 인 것만
        if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith(".json"):
            try:
                meta, resp = dbx.files_download(e.path_lower)
                content = resp.content.decode("utf-8")
                data = json.loads(content)
                records.append(data)
            except Exception as ex:
                print("JSON parse error:", e.path_lower, repr(ex))
                continue

    return records


def aggregate_votes(records):
    latest = {}
    all_evaluators = set()

    for rec in records:
        evaluator_name = rec.get("evaluatorName")
        ts = rec.get("serverReceivedAt") or rec.get("timestamp") or ""
        results = rec.get("results") or []

        if not evaluator_name:
            continue

        all_evaluators.add(evaluator_name)

        for entry in results:
            pid = entry.get("presenterId")
            pname = entry.get("presenter")
            score = entry.get("score")
            comment = entry.get("comment") or ""

            if not pid:
                continue

            key = (evaluator_name, pid)
            prev = latest.get(key)
            if (prev is None) or (ts > prev[0]):
                latest[key] = (ts, score, comment, pname)

    # 참가자별 집계
    all_presenters = {}

    for (evaluator_name, pid), (ts, score, comment, pname) in latest.items():
        if score is None:
            continue
        try:
            s = float(score)
        except Exception:
            continue

        p = all_presenters.setdefault(pid, {
            "presenterId": pid,
            "presenter": pname,
            "totalScore": 0.0,
            "voteCount": 0,
            "details": []
        })

        if pname:
            p["presenter"] = pname

        p["totalScore"] += s
        p["voteCount"] += 1
        p["details"].append({
            "evaluatorName": evaluator_name,
            "score": s,
            "comment": comment,
            "timestamp": ts
        })

    result_list = []

    for pid, info in all_presenters.items():
        cnt = info["voteCount"]
        avg = info["totalScore"] / cnt if cnt > 0 else 0.0
        info["avgScore"] = round(avg, 3)
        result_list.append(info)

    result_list.sort(key=lambda x: (-x["avgScore"], -x["voteCount"], x["presenterId"]))

    return {
        "ok": True,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "all_presenters": result_list,
        "totalEvaluators": len(all_evaluators)
    }


@app.route("/api/results", methods=["GET"])
def api_results():
    try:
        records = load_all_votes_from_dropbox()
        agg = aggregate_votes(records)

        raw = request.args.get("raw", "").lower() in ("1", "true", "yes")

        if raw:
            # 필요 최소만 내려서 payload를 줄임(원본 전체도 가능)
            raw_votes = []
            for r in records:
                raw_votes.append({
                    "evaluatorName": r.get("evaluatorName", ""),
                    "timestamp": r.get("serverReceivedAt") or r.get("timestamp") or "",
                    "results": r.get("results") or []
                })
            agg["rawVotes"] = raw_votes

        return jsonify(agg)
    except Exception as e:
        print("Aggregate error:", repr(e))
        return jsonify({
            "ok": False,
            "error": "aggregate_failed",
            "detail": repr(e)     # ★ 디버깅용 상세 메시지
        }), 500


if __name__ == "__main__":
    # 로컬 테스트용
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
