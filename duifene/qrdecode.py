"""二维码解码：把服务端下发的二维码图还原成文本（可选依赖）。

只在二维码签到时用到：服务端不再接受活动 ID 直签，必须先取教师端实时二维码
并解出其中的 ``state`` 令牌。依赖缺失时抛 :class:`QrDecodeUnavailable`，让上层
给出「装依赖」的明确提示，而不是静默失败。
"""

from __future__ import annotations

import base64
import binascii


class QrDecodeUnavailable(RuntimeError):
    """缺少 Pillow / pyzbar，无法在本机解码二维码。"""


def decode_image_base64(data: str) -> str | None:
    """解码 base64 图片中的二维码，返回其文本内容；无二维码返回 None。

    Raises:
        QrDecodeUnavailable: 未安装 Pillow 或 pyzbar。
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except Exception as exc:  # noqa: BLE001 - 依赖缺失形态因平台而异
        raise QrDecodeUnavailable(
            "二维码解码需要 Pillow 与 pyzbar：pip install Pillow pyzbar"
        ) from exc
    try:
        raw = base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        with Image.open(_bytes_io(raw)) as image:
            results = decode(image.convert("RGB"))
    except Exception:  # noqa: BLE001 - 图片损坏或解码库异常
        return None
    if not results:
        return None
    payload = results[0].data
    return payload.decode("utf-8", "replace").strip() or None


def _bytes_io(raw: bytes):
    """把字节包装成 PIL 可读的流对象。"""
    import io

    return io.BytesIO(raw)


def available() -> bool:
    """本机是否具备解码能力（供界面提示用，不抛异常）。"""
    try:
        import PIL  # noqa: F401
        import pyzbar  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True
