     1|"""WeCom BizMsgCrypt-compatible AES-CBC encryption for callback mode.
     2|
     3|Implements the same wire format as Tencent's official ``WXBizMsgCrypt``
     4|SDK so that WeCom can verify, encrypt, and decrypt callback payloads.
     5|"""
     6|
     7|from __future__ import annotations
     8|
     9|import base64
    10|import hashlib
    11|import os
    12|import secrets
    13|import socket
    14|import struct
    15|from typing import Optional
    16|from xml.etree import ElementTree as ET
    17|
    18|from cryptography.hazmat.backends import default_backend
    19|from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    20|
    21|
    22|class WeComCryptoError(Exception):
    23|    pass
    24|
    25|
    26|class SignatureError(WeComCryptoError):
    27|    pass
    28|
    29|
    30|class DecryptError(WeComCryptoError):
    31|    pass
    32|
    33|
    34|class EncryptError(WeComCryptoError):
    35|    pass
    36|
    37|
    38|class PKCS7Encoder:
    39|    block_size = 32
    40|
    41|    @classmethod
    42|    def encode(cls, text: bytes) -> bytes:
    43|        amount_to_pad = cls.block_size - (len(text) % cls.block_size)
    44|        if amount_to_pad == 0:
    45|            amount_to_pad = cls.block_size
    46|        pad = bytes([amount_to_pad]) * amount_to_pad
    47|        return text + pad
    48|
    49|    @classmethod
    50|    def decode(cls, decrypted: bytes) -> bytes:
    51|        if not decrypted:
    52|            raise DecryptError("empty decrypted payload")
    53|        pad = decrypted[-1]
    54|        if pad < 1 or pad > cls.block_size:
    55|            raise DecryptError("invalid PKCS7 padding")
    56|        if decrypted[-pad:] != bytes([pad]) * pad:
    57|            raise DecryptError("malformed PKCS7 padding")
    58|        return decrypted[:-pad]
    59|
    60|
    61|def _sha1_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    62|    parts = sorted([token, timestamp, nonce, encrypt])
    63|    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    64|
    65|
    66|class WXBizMsgCrypt:
    67|    """Minimal WeCom callback crypto helper compatible with BizMsgCrypt semantics."""
    68|
    69|    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
    70|        if not token:
    71|            raise ValueError("token is required")
    72|        if not encoding_aes_key:
    73|            raise ValueError("encoding_aes_key is required")
    74|        if len(encoding_aes_key) != 43:
    75|            raise ValueError("encoding_aes_key must be 43 chars")
    76|        if not receive_id:
    77|            raise ValueError("receive_id is required")
    78|
    79|        self.token = token
    80|        self.receive_id = receive_id
    81|        self.key = base64.b64decode(encoding_aes_key + "=")
    82|        self.iv = self.key[:16]
    83|
    84|    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
    85|        plain = self.decrypt(msg_signature, timestamp, nonce, echostr)
    86|        return plain.decode("utf-8")
    87|
    88|    def decrypt(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bytes:
    89|        expected = _sha1_signature(self.token, timestamp, nonce, encrypt)
    90|        if expected != msg_signature:
    91|            raise SignatureError("signature mismatch")
    92|        try:
    93|            cipher_text = base64.b64decode(encrypt)
    94|        except Exception as exc:
    95|            raise DecryptError(f"invalid base64 payload: {exc}") from exc
    96|        try:
    97|            cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
    98|            decryptor = cipher.decryptor()
    99|            padded = decryptor.update(cipher_text) + decryptor.finalize()
   100|            plain = PKCS7Encoder.decode(padded)
   101|            content = plain[16:]  # skip 16-byte random prefix
   102|            xml_length = socket.ntohl(struct.unpack("I", content[:4])[0])
   103|            xml_content = content[4:4 + xml_length]
   104|            receive_id = content[4 + xml_length:].decode("utf-8")
   105|        except WeComCryptoError:
   106|            raise
   107|        except Exception as exc:
   108|            raise DecryptError(f"decrypt failed: {exc}") from exc
   109|
   110|        if receive_id != self.receive_id:
   111|            raise DecryptError("receive_id mismatch")
   112|        return xml_content
   113|
   114|    def encrypt(self, plaintext: str, nonce: Optional[str] = None, timestamp: Optional[str] = None) -> str:
   115|        nonce = nonce or self._random_nonce()
   116|        timestamp = timestamp or str(int(__import__("time").time()))
   117|        encrypt = self._encrypt_bytes(plaintext.encode("utf-8"))
   118|        signature = _sha1_signature(self.token, timestamp, nonce, encrypt)
   119|        root = ET.Element("xml")
   120|        ET.SubElement(root, "Encrypt").text = encrypt
   121|        ET.SubElement(root, "MsgSignature").text = signature
   122|        ET.SubElement(root, "TimeStamp").text = timestamp
   123|        ET.SubElement(root, "Nonce").text = nonce
   124|        return ET.tostring(root, encoding="unicode")
   125|
   126|    def _encrypt_bytes(self, raw: bytes) -> str:
   127|        try:
   128|            random_prefix = os.urandom(16)
   129|            msg_len = struct.pack("I", socket.htonl(len(raw)))
   130|            payload = random_prefix + msg_len + raw + self.receive_id.encode("utf-8")
   131|            padded = PKCS7Encoder.encode(payload)
   132|            cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
   133|            encryptor = cipher.encryptor()
   134|            encrypted = encryptor.update(padded) + encryptor.finalize()
   135|            return base64.b64encode(encrypted).decode("utf-8")
   136|        except Exception as exc:
   137|            raise EncryptError(f"encrypt failed: {exc}") from exc
   138|
   139|    @staticmethod
   140|    def _random_nonce(length: int = 10) -> str:
   141|        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
   142|        return "".join(secrets.choice(alphabet) for _ in range(length))
   143|