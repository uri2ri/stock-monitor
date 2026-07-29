"""
kakao.py – 카카오톡 나에게 보내기
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
MAX_TEXT_LEN = 200


def _refresh_access_token() -> str:
    """refresh_token으로 access_token을 갱신한다.

    - 새 refresh_token이 응답에 포함되면 경고 로그를 남긴다.
      (GitHub Actions Secret 자동 갱신 불가)
    """
    data = {
        "grant_type": "refresh_token",
        "client_id": os.environ["KAKAO_REST_API_KEY"],
        "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
    }
    # client_secret이 있으면 포함
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET")
    if client_secret:
        data["client_secret"] = client_secret

    resp = requests.post(TOKEN_URL, data=data, timeout=15)
    resp.raise_for_status()
    body = resp.json()

    access_token = body["access_token"]
    logger.info("카카오 access_token 갱신 완료")

    # 새 refresh_token이 오면 경고
    # 값 자체는 로그에 남기지 않는다 (public 저장소의 Actions 로그는 공개됨).
    # 재발급이 필요하면 OAuth 인증을 다시 거쳐야 한다.
    if "refresh_token" in body:
        new_token = body["refresh_token"]
        logger.warning(
            "⚠️  카카오 refresh_token이 갱신되었습니다 (끝 4자리: …%s). "
            "OAuth 인증을 다시 받아 GitHub Secrets의 "
            "KAKAO_REFRESH_TOKEN을 수동으로 교체하세요.",
            new_token[-4:],
        )

    return access_token


def send_kakao_message(text: str) -> None:
    """카카오톡 나에게 보내기 (기본 텍스트 템플릿).

    - 텍스트는 200자 제한 → 초과 시 잘라서 보냄
    - access_token은 refresh_token으로 매번 갱신
    """
    access_token = _refresh_access_token()

    # 200자 제한 처리
    if len(text) > MAX_TEXT_LEN:
        text = text[: MAX_TEXT_LEN - 1] + "…"

    import json

    template = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": "https://www.notion.so",
            "mobile_web_url": "https://www.notion.so",
        },
    }

    resp = requests.post(
        SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template)},
        timeout=15,
    )
    resp.raise_for_status()
    logger.info("카카오톡 전송 완료 (%d자)", len(text))
