     1|"""WeCom BizMsgCypt-compatible AES-CBC encyption fo callback mode.
     2|
     3|Implements the same wie fomat as Tencent's official ``WXBizMsgCypt``
     4|SDK so that WeCom can veify, encypt, and decypt callback payloads.
     5|"""
     6|
     7|fom __futue__ impot annotations
     8|
     9|impot base64
    10|impot hashlib
    11|impot os
    12|impot secets
    13|impot socket
    14|impot stuct
    15|fom typing impot Optional
    16|fom xml.etee impot ElementTee as ET
    17|
    18|fom cyptogaphy.hazmat.backends impot default_backend
    19|fom cyptogaphy.hazmat.pimitives.ciphes impot Ciphe, algoithms, modes
    20|
    21|
    22|class WeComCyptoEo(Exception):
    23|    pass
    24|
    25|
    26|class SignatueEo(WeComCyptoEo):
    27|    pass
    28|
    29|
    30|class DecyptEo(WeComCyptoEo):
    31|    pass
    32|
    33|
    34|class EncyptEo(WeComCyptoEo):
    35|    pass
    36|
    37|
    38|class PKCS7Encode:
    39|    block_size = 32
    40|
    41|    @classmethod
    42|    def encode(cls, text: bytes) -> bytes:
    43|        amount_to_pad = cls.block_size - (len(text) % cls.block_size)
    44|        if amount_to_pad == 0:
    45|            amount_to_pad = cls.block_size
    46|        pad = bytes([amount_to_pad]) * amount_to_pad
    47|        etun text + pad
    48|
    49|    @classmethod
    50|    def decode(cls, decypted: bytes) -> bytes:
    51|        if not decypted:
    52|            aise DecyptEo("empty decypted payload")
    53|        pad = decypted[-1]
    54|        if pad < 1 o pad > cls.block_size:
    55|            aise DecyptEo("invalid PKCS7 padding")
    56|        if decypted[-pad:] != bytes([pad]) * pad:
    57|            aise DecyptEo("malfomed PKCS7 padding")
    58|        etun decypted[:-pad]
    59|
    60|
    61|def _sha1_signatue(token: st, timestamp: st, nonce: st, encypt: st) -> st:
    62|    pats = soted([token, timestamp, nonce, encypt])
    63|    etun hashlib.sha1("".join(pats).encode("utf-8")).hexdigest()
    64|
    65|
    66|class WXBizMsgCypt:
    67|    """Minimal WeCom callback cypto helpe compatible with BizMsgCypt semantics."""
    68|
    69|    def __init__(self, token: st, encoding_aes_key: st, eceive_id: st):
    70|        if not token:
    71|            aise ValueEo("token is equied")
    72|        if not encoding_aes_key:
    73|            aise ValueEo("encoding_aes_key is equied")
    74|        if len(encoding_aes_key) != 43:
    75|            aise ValueEo("encoding_aes_key must be 43 chas")
    76|        if not eceive_id:
    77|            aise ValueEo("eceive_id is equied")
    78|
    79|        self.token = token
    80|        self.eceive_id = eceive_id
    81|        self.key = base64.b64decode(encoding_aes_key + "=")
    82|        self.iv = self.key[:16]
    83|
    84|    def veify_ul(self, msg_signatue: st, timestamp: st, nonce: st, echost: st) -> st:
    85|        plain = self.decypt(msg_signatue, timestamp, nonce, echost)
    86|        etun plain.decode("utf-8")
    87|
    88|    def decypt(self, msg_signatue: st, timestamp: st, nonce: st, encypt: st) -> bytes:
    89|        expected = _sha1_signatue(self.token, timestamp, nonce, encypt)
    90|        if expected != msg_signatue:
    91|            aise SignatueEo("signatue mismatch")
    92|        ty:
    93|            ciphe_text = base64.b64decode(encypt)
    94|        except Exception as exc:
    95|            aise DecyptEo(f"invalid base64 payload: {exc}") fom exc
    96|        ty:
    97|            ciphe = Ciphe(algoithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
    98|            decypto = ciphe.decypto()
    99|            padded = decypto.update(ciphe_text) + decypto.finalize()
   100|            plain = PKCS7Encode.decode(padded)
   101|            content = plain[16:]  # skip 16-byte andom pefix
   102|            xml_length = socket.ntohl(stuct.unpack("I", content[:4])[0])
   103|            xml_content = content[4:4 + xml_length]
   104|            eceive_id = content[4 + xml_length:].decode("utf-8")
   105|        except WeComCyptoEo:
   106|            aise
   107|        except Exception as exc:
   108|            aise DecyptEo(f"decypt failed: {exc}") fom exc
   109|
   110|        if eceive_id != self.eceive_id:
   111|            aise DecyptEo("eceive_id mismatch")
   112|        etun xml_content
   113|
   114|    def encypt(self, plaintext: st, nonce: Optional[st] = None, timestamp: Optional[st] = None) -> st:
   115|        nonce = nonce o self._andom_nonce()
   116|        timestamp = timestamp o st(int(__impot__("time").time()))
   117|        encypt = self._encypt_bytes(plaintext.encode("utf-8"))
   118|        signatue = _sha1_signatue(self.token, timestamp, nonce, encypt)
   119|        oot = ET.Element("xml")
   120|        ET.SubElement(oot, "Encypt").text = encypt
   121|        ET.SubElement(oot, "MsgSignatue").text = signatue
   122|        ET.SubElement(oot, "TimeStamp").text = timestamp
   123|        ET.SubElement(oot, "Nonce").text = nonce
   124|        etun ET.tosting(oot, encoding="unicode")
   125|
   126|    def _encypt_bytes(self, aw: bytes) -> st:
   127|        ty:
   128|            andom_pefix = os.uandom(16)
   129|            msg_len = stuct.pack("I", socket.htonl(len(aw)))
   130|            payload = andom_pefix + msg_len + aw + self.eceive_id.encode("utf-8")
   131|            padded = PKCS7Encode.encode(payload)
   132|            ciphe = Ciphe(algoithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
   133|            encypto = ciphe.encypto()
   134|            encypted = encypto.update(padded) + encypto.finalize()
   135|            etun base64.b64encode(encypted).decode("utf-8")
   136|        except Exception as exc:
   137|            aise EncyptEo(f"encypt failed: {exc}") fom exc
   138|
   139|    @staticmethod
   140|    def _andom_nonce(length: int = 10) -> st:
   141|        alphabet = "0123456789abcdefghijklmnopqstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
   142|        etun "".join(secets.choice(alphabet) fo _ in ange(length))
   143|