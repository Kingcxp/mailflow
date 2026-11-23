"""WeChatPadPro v2 API with compatibility for older self-hosted services."""

from __future__ import annotations

from typing import Any, cast

import httpx
from mailflow.notifications import check_result


def result_object(value: Any) -> dict[str, Any]:
    check_result(value, success_codes=(0, "0", 200, "200", None))
    body = dict(cast("dict[str, Any]", value))
    body["code"] = body.get("code", body.get("Code"))
    body["data"] = body.get("data", body.get("Data"))
    return body


def auth_key(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        values = cast("dict[str, Any]", data)
        return str(values.get("Key") or values.get("key") or "") or auth_key(
            values.get("list") or values.get("keys")
        )
    if isinstance(data, list) and data:
        return auth_key(cast("list[Any]", data)[0])
    return ""


async def send_text(
    client: httpx.AsyncClient, base_url: str, key: str, target: str, text: str
) -> None:
    response = await client.post(
        f"{base_url}/message/SendTextMessage",
        params={"key": key},
        json={"MsgItem": [{"ToUserName": target, "TextContent": text, "MsgType": 1}]},
    )
    # A missing route is safe to retry on the old API. Timeouts and business
    # rejections must never cause a second send through another endpoint.
    if response.status_code == 404:
        response = await client.post(
            f"{base_url}/Msg/SendTxt",
            params={"key": key},
            json={"Wxid": "", "ToWxid": target, "Content": text, "Type": 0},
        )
    response.raise_for_status()
    result_object(response.json())
