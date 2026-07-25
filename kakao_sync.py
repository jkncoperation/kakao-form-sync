import json
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
    return datetime.strptime(
        dt_str,
        "%Y-%m-%d %H:%M:%S"
    )


def load_config():
    with open(
        CONFIG_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def load_checkpoint():

    if not CHECKPOINT_FILE.exists():
        return {}

    with open(
        CHECKPOINT_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def save_checkpoint(data):

    with open(
        CHECKPOINT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 카카오 비즈니스폼+
# ============================================================

def get_page(
    config,
    form_id,
    cursor_id=None
):

    url = (
        "https://apis.moment.kakao.com"
        "/openapi/v4/adAccounts/bizFormPlus/report"
    )

    headers = {
        "Authorization":
            f"Bearer {config['access_token']}",
        "adAccountId":
            str(config["ad_account_id"])
    }

    params = {
        "formId":
            form_id,
        "size":
            100
    }

    if cursor_id:
        params["cursorId"] = cursor_id

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def get_all_rows(
    config,
    form_id
):

    all_rows = []

    cursor_id = None

    page = 1

    while True:

        result = get_page(
            config,
            form_id,
            cursor_id
        )

        rows = result.get(
            "data",
            []
        )

        has_next = result.get(
            "hasNext",
            False
        )

        next_cursor = result.get(
            "nextCursor"
        )

        print(
            f"FORM {form_id} | PAGE {page}"
        )

        print(
            f"rows      : {len(rows)}"
        )

        print(
            f"hasNext   : {has_next}"
        )

        print(
            f"nextCursor: {next_cursor}"
        )

        print(
            "-" * 80
        )

        all_rows.extend(
            rows
        )

        if not has_next:
            break

        cursor_id = next_cursor

        page += 1

    return all_rows


# ============================================================
# Zapier
# ============================================================

def send_to_zapier(
    webhook_url,
    row
):

    response = requests.post(
        webhook_url,
        json=row,
        timeout=30
    )

    response.raise_for_status()

    return response


# ============================================================
# 폼 처리
# ============================================================

def process_form(
    config,
    form,
    checkpoint_data
):

    form_id = str(
        form["form_id"]
    )

    form_name = (
        form["form_name"]
    )

    print()
    print("=" * 80)
    print(
        f"폼 처리 시작 : {form_name}"
    )
    print("=" * 80)

    if form_id not in checkpoint_data:

        checkpoint_data[form_id] = {
            "last_submitted_at":
                "2000-01-01 00:00:00",
            "last_apply_id":
                0
        }

    last_submitted_at = (
        checkpoint_data[
            form_id
        ][
            "last_submitted_at"
        ]
    )

    last_apply_id = (
        checkpoint_data[
            form_id
        ][
            "last_apply_id"
        ]
    )

    rows = get_all_rows(
        config,
        form_id
    )

    if not rows:

        print(
            "조회 데이터 없음"
        )

        return

    rows.sort(
        key=lambda x: (
            parse_dt(
                x["submittedAt"]
            ),
            x["applyId"]
        )
    )

    checkpoint_dt = parse_dt(
        last_submitted_at
    )

    new_rows = []

    for row in rows:

        row_dt = parse_dt(
            row["submittedAt"]
        )

        is_new = False

        if row_dt > checkpoint_dt:
            is_new = True

        elif (
            row_dt == checkpoint_dt
            and row["applyId"]
            > last_apply_id
        ):
            is_new = True

        if is_new:

            row["form_id"] = (
                form_id
            )

            row["form_name"] = (
                form_name
            )

            new_rows.append(
                row
            )

    print(
        f"신규 DB 수 : "
        f"{len(new_rows)}"
    )

    if not new_rows:
        return

    newest_row = (
        new_rows[-1]
    )

    success_count = 0

    for row in new_rows:

        try:

            send_to_zapier(
                config[
                    "zapier_webhook"
                ],
                row
            )

            success_count += 1

            print(
                f"전송성공 | "
                f"{row['applyId']} | "
                f"{row.get('name','')}"
            )

        except Exception as e:

            print(
                f"전송실패 | "
                f"{row['applyId']} | "
                f"{e}"
            )

    if success_count == len(
        new_rows
    ):

        checkpoint_data[
            form_id
        ] = {
            "last_submitted_at":
                newest_row[
                    "submittedAt"
                ],
            "last_apply_id":
                newest_row[
                    "applyId"
                ]
        }

        save_checkpoint(
            checkpoint_data
        )

        print(
            f"{form_name} "
            f"체크포인트 저장 완료"
        )

    else:

        print(
            f"{form_name} "
            f"일부 전송 실패"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    config = load_config()

    checkpoint_data = (
        load_checkpoint()
    )

    forms = config.get(
        "forms",
        []
    )

    for form in forms:

        process_form(
            config,
            form,
            checkpoint_data
        )

    print()
    print("=" * 80)
    print("전체 작업 완료")
    print("=" * 80)


if __name__ == "__main__":
    main()