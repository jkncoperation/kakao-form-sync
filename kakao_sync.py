import json
import time
import requests

from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "config.json"
CHECKPOINT_FILE = BASE_DIR / "checkpoint.json"


# ============================================================
# 공통
# ============================================================

def parse_dt(dt_str):
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_checkpoint():
    if not CHECKPOINT_FILE.exists():
        return {}
    with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_checkpoint(data):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# 카카오 비즈니스폼+
# ============================================================

def get_page(config, form_id, cursor_id=None):
    url = (
        "https://apis.moment.kakao.com"
        "/openapi/v4/adAccounts/bizFormPlus/report"
    )

    headers = {
        "Authorization": f"Bearer {config['access_token']}",
        "adAccountId": str(config["ad_account_id"]),
    }

    params = {
        "formId": form_id,
        "size": 1000,  # [최적화1] 100 → 1000 (API 허용 최대값), 호출 수 1/10
    }

    if cursor_id:
        params["cursorId"] = cursor_id

    for attempt in range(5):
        response = requests.get(
            url, headers=headers, params=params, timeout=30
        )

        # 호출 한도 초과(429)는 대기 후 재시도 (커서 문제와 무관)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 0) or 0) or (
                5 * (2 ** attempt)
            )
            print(f"호출 한도 초과(429) → {wait}초 대기 후 재시도 ({attempt + 1}/5)")
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError("429 재시도 한도 초과: 다음 실행 회차에서 재개됩니다")


def get_all_rows(config, form_id, start_cursor=None):
    """
    페이지를 순회하며 접수 데이터를 수집.

    [최적화2] 커서 이어읽기:
    API의 커서(cursorId)는 applyId 기반이므로, 체크포인트의
    last_apply_id를 시작 커서로 넣으면 그 이후 데이터만 조회된다.
    → 매 실행이 사실상 1회 호출로 끝남.

    [안전장치] 시작 커서로 조회가 실패하면(만료된 applyId 등)
    커서 없이 처음부터 전체 조회로 자동 전환한다.
    이후 신규 판별은 기존 체크포인트 로직이 동일하게 수행하므로
    중복 전송은 어느 경로에서도 발생하지 않는다.
    """
    all_rows = []
    cursor_id = start_cursor
    page = 1

    if start_cursor:
        print(f"FORM {form_id} | 커서 이어읽기 시작: applyId {start_cursor} 이후")

    while True:
        try:
            result = get_page(config, form_id, cursor_id)
        except requests.HTTPError as e:
            # 429는 get_page 안에서 재시도 처리됨.
            # 여기 도달하는 HTTPError 중 400번대(잘못된 커서 등)만
            # 전체 조회로 전환하고, 500번대는 그대로 실패시켜
            # 다음 실행 회차가 이어받게 한다.
            status = e.response.status_code if e.response is not None else 0
            if (
                page == 1
                and start_cursor is not None
                and 400 <= status < 500
                and status != 429
            ):
                print(
                    f"FORM {form_id} | 커서 조회 실패 → 전체 조회로 전환"
                )
                start_cursor = None
                cursor_id = None
                continue
            raise

        rows = result.get("data", [])
        has_next = result.get("hasNext", False)
        next_cursor = result.get("nextCursor")

        print(
            f"FORM {form_id} | PAGE {page} | "
            f"rows {len(rows)} | hasNext {has_next}"
        )

        all_rows.extend(rows)

        if not has_next:
            break

        cursor_id = next_cursor
        page += 1

    return all_rows


# ============================================================
# Zapier
# ============================================================

def send_to_zapier(webhook_url, row):
    response = requests.post(webhook_url, json=row, timeout=30)
    response.raise_for_status()
    return response


# ============================================================
# 폼 처리
# ============================================================

def process_form(config, form, checkpoint_data):
    form_id = str(form["form_id"])
    form_name = form["form_name"]

    # [폼별 웹훅] 폼에 zapier_webhook이 지정돼 있으면 그것을,
    # 없으면 기존 공용 웹훅(config 최상위)을 사용
    webhook_url = form.get("zapier_webhook") or config["zapier_webhook"]

    print()
    print("=" * 80)
    print(f"폼 처리 시작 : {form_name}")
    if form.get("zapier_webhook"):
        print("(전용 웹훅 사용)")
    print("=" * 80)

    if form_id not in checkpoint_data:
        checkpoint_data[form_id] = {
            "last_submitted_at": "2000-01-01 00:00:00",
            "last_apply_id": 0,
        }

    last_submitted_at = checkpoint_data[form_id]["last_submitted_at"]
    last_apply_id = checkpoint_data[form_id]["last_apply_id"]

    checkpoint_dt = parse_dt(last_submitted_at)

    # 접수 이력이 있는 폼이면 마지막 applyId부터 이어읽기
    start_cursor = last_apply_id if last_apply_id > 0 else None

    rows = get_all_rows(config, form_id, start_cursor)

    if not rows:
        print("조회 데이터 없음 (신규 없음)")
        return

    rows.sort(
        key=lambda x: (parse_dt(x["submittedAt"]), x["applyId"])
    )

    new_rows = []

    for row in rows:
        row_dt = parse_dt(row["submittedAt"])

        is_new = False

        if row_dt > checkpoint_dt:
            is_new = True
        elif row_dt == checkpoint_dt and row["applyId"] > last_apply_id:
            is_new = True

        if is_new:
            row["form_id"] = form_id
            row["form_name"] = form_name
            new_rows.append(row)

    print(f"신규 DB 수 : {len(new_rows)}")

    if not new_rows:
        return

    success_count = 0
    last_success_row = None

    for row in new_rows:
        try:
            send_to_zapier(webhook_url, row)
            success_count += 1
            last_success_row = row
            print(
                f"전송성공 | {row['applyId']} | {row.get('name', '')}"
            )
        except Exception as e:
            # [중복 방지] 실패 지점에서 즉시 중단.
            # 성공한 건까지는 아래에서 체크포인트에 반영되므로
            # 다음 실행은 '실패한 건부터' 이어서 재시도한다.
            # (계속 진행하면 순서가 어긋나고, 전체 재전송 시 중복 발생)
            print(f"전송실패 | {row['applyId']} | {e}")
            print("이후 건은 다음 실행에서 이어서 재시도")
            break

    # 성공한 건이 하나라도 있으면 그 지점까지 체크포인트 저장
    if last_success_row is not None:
        checkpoint_data[form_id] = {
            "last_submitted_at": last_success_row["submittedAt"],
            "last_apply_id": last_success_row["applyId"],
        }
        save_checkpoint(checkpoint_data)

        if success_count == len(new_rows):
            print(f"{form_name} 체크포인트 저장 완료")
        else:
            print(
                f"{form_name} 일부 전송 실패 - "
                f"{success_count}/{len(new_rows)}건까지 체크포인트 저장"
            )
    else:
        print(f"{form_name} 전송 실패 - 다음 실행에서 재시도")


# ============================================================
# MAIN
# ============================================================

def main():
    config = load_config()
    checkpoint_data = load_checkpoint()

    forms = config.get("forms", [])

    for form in forms:
        process_form(config, form, checkpoint_data)
        time.sleep(1)

    print()
    print("=" * 80)
    print("전체 작업 완료")
    print("=" * 80)


if __name__ == "__main__":
    main()
