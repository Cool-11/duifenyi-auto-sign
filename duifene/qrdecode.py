"""二维码解码：把服务端下发的二维码图还原成文本（可选依赖）。

只在二维码签到时用到：服务端不再接受活动 ID 直签，必须先取教师端实时二维码
并解出其中的 ``state`` 令牌。依赖缺失时抛 :class:`QrDecodeUnavailable`，让上层
给出「装依赖」的明确提示，而不是静默失败。
"""

from __future__ import annotations

import base64
import binascii
import io
from functools import lru_cache


class QrDecodeUnavailable(RuntimeError):
    """缺少 Pillow / pyzbar，无法在本机解码二维码。"""


@lru_cache(maxsize=1)
def available() -> bool:
    """本机是否具备解码能力（供界面提示用，不抛异常）。

    结果缓存：依赖不会在运行期变化，避免每次签到都重复 import。
    """
    try:
        import PIL  # noqa: F401
        import pyzbar  # noqa: F401
    except Exception:  # noqa: BLE001 - 依赖缺失形态因平台而异
        return False
    return True


def decode_image_base64(data: str) -> str | None:
    """解码 base64 图片中的二维码，返回其文本内容；无二维码返回 None。

    Args:
        data: 纯 base64（不含 data URI 前缀），空白字符会被忽略。

    Raises:
        QrDecodeUnavailable: 未安装 Pillow 或 pyzbar。
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except Exception as exc:  # noqa: BLE001 - 依赖缺失形态因平台而异
        raise QrDecodeUnavailable(
            "二维码解码需要 Pillow 与 pyzbar：pip install -r requirements.txt"
        ) from exc
    try:
        raw = base64.b64decode("".join(str(data).split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            results = decode(image.convert("RGB"))
    except Exception:  # noqa: BLE001 - 图片损坏或解码库异常
        return None
    # 一张图可能含多个码，取第一个能解出文本的
    for result in results:
        text = result.data.decode("utf-8", "replace").strip()
        if text:
            return text
    return None
