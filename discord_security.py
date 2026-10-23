import nacl.exceptions
import nacl.signing


def verify_discord_signature(
    request_body: bytes,
    signature: str,
    timestamp: str,
    public_key: str | None,
) -> bool:
    """Verify the signature on a Discord interaction request."""
    print(f"簽章驗證：開始；body_bytes={len(request_body)}", flush=True)
    if not public_key:
        print("簽章驗證失敗：DISCORD_PUBLIC_KEY 未設定", flush=True)
        return False

    if not signature or not timestamp:
        print(
            "簽章驗證失敗：必要 header 缺漏；"
            f"signature_present={bool(signature)} timestamp_present={bool(timestamp)}",
            flush=True,
        )
        return False

    try:
        print("簽章驗證：解析公鑰與簽章格式", flush=True)
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(public_key))
        message = timestamp.encode() + request_body
        print("簽章驗證：比對請求簽章", flush=True)
        verify_key.verify(message, bytes.fromhex(signature))
        print("簽章驗證成功", flush=True)
        return True
    except ValueError as error:
        print(
            f"簽章驗證失敗：Hex 格式錯誤（{type(error).__name__}）",
            flush=True,
        )
        return False
    except nacl.exceptions.BadSignatureError:
        print("簽章驗證失敗：簽章與公鑰不符", flush=True)
        return False
    except Exception as error:
        print(
            f"簽章驗證發生未預期錯誤：{type(error).__name__}",
            flush=True,
        )
        return False
