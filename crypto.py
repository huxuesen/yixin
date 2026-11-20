"""AES encryption of the login password (AesUtil.encrypt in the APK).

Verified against the APK + a captured login request (2026-11-20):
- Class: com.msunsoft.net.crypto.AesUtil (classes6.dex), javax.crypto.
- Key: "6939382616803157" (16 ASCII bytes -> AES-128)
- IV:  "0000000000000000" (16 ASCII '0' bytes, 0x30 -- NOT zero bytes)
- Mode: AES/CBC/PKCS7Padding (PKCS5Padding is identical for AES)
- Output: Base64 NO_WRAP (padding kept)
- Plaintext: UTF-8
Test vector: encrypt("abc123") == "eL09BmOas+pbVh7+3tuyYg=="
"""
AES_KEY = b"6939382616803157"
AES_IV = b"0000000000000000"


def encrypt_password(password):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    import base64
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    ct = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
    return base64.b64encode(ct).decode()
