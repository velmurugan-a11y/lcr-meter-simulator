"""
Liquid Controls LCP frame builder and parser.

Wire format: 7E 7E | to | from | status | len | data[len] | crc_lo | crc_hi

Byte stuffing: 0x7E and 0x1B inside the payload are escaped as 1B xx.
               This applies to to/from/status/len/data fields.
               CRC bytes are also escaped, but escape bytes there are NOT
               counted in the CRC computation.

CRC-16: poly 0x1021, seed 0x7E7E, data bits shifted in MSB-first order.
        Covers: to, from, status, len, data[] — INCLUDING inserted escape bytes.
        Does NOT cover the two CRC bytes themselves.

Numbers: big-endian throughout.
Host (PandaBox) address: 0x14
"""

LCP_SYNC = 0x7E
LCP_ESC  = 0x1B
LCP_HOST = 0x14       # PandaBox node address

LCP_ST_MSGID    = 0x01   # alternating message-ID bit
LCP_ST_SYNC     = 0x02   # session-start (Get Product ID)
LCP_ST_RESPONSE = 0x80   # set in all meter responses


def _crc_byte(crc: int, b: int) -> int:
    for i in range(7, -1, -1):
        carry = crc & 0x8000
        crc = ((crc << 1) | ((b >> i) & 1)) & 0xFFFF
        if carry:
            crc ^= 0x1021
    return crc


def build(to: int, from_: int, status: int, data: bytes | bytearray) -> bytes:
    """Build a complete LCP frame ready to send on the wire."""
    crc = 0x7E7E
    out = bytearray([LCP_SYNC, LCP_SYNC])

    def put(b: int):
        nonlocal crc
        if b == LCP_SYNC or b == LCP_ESC:
            out.append(LCP_ESC)
            crc = _crc_byte(crc, LCP_ESC)
        out.append(b)
        crc = _crc_byte(crc, b)

    put(to)
    put(from_)
    put(status)
    put(len(data))
    for b in data:
        put(b)

    # CRC bytes: escape them on the wire but do NOT feed escapes into the CRC
    def put_crc(b: int):
        if b == LCP_SYNC or b == LCP_ESC:
            out.append(LCP_ESC)
        out.append(b)

    put_crc(crc & 0xFF)
    put_crc((crc >> 8) & 0xFF)
    return bytes(out)


def parse(raw: bytes | bytearray):
    """
    Parse a frame starting at raw[0] == 7E, raw[1] == 7E.
    Returns (to, from_, status, payload_bytes) on success, or None on failure.
    """
    if len(raw) < 8 or raw[0] != LCP_SYNC or raw[1] != LCP_SYNC:
        return None

    crc = 0x7E7E
    i = 2
    hdr = []
    payload = bytearray()

    def get_escaped(count_in_crc: bool = True):
        nonlocal i, crc
        if i >= len(raw):
            return None
        if raw[i] == LCP_ESC:
            if count_in_crc:
                crc = _crc_byte(crc, LCP_ESC)
            i += 1
            if i >= len(raw):
                return None
        b = raw[i]
        i += 1
        if count_in_crc:
            crc = _crc_byte(crc, b)
        return b

    # 4 header bytes: to, from, status, len
    for _ in range(4):
        b = get_escaped()
        if b is None:
            return None
        hdr.append(b)

    length = hdr[3]
    for _ in range(length):
        b = get_escaped()
        if b is None:
            return None
        payload.append(b)

    # 2 CRC bytes — escaped but NOT counted in CRC
    received_crc = []
    for _ in range(2):
        b = get_escaped(count_in_crc=False)
        if b is None:
            return None
        received_crc.append(b)

    if received_crc[0] != (crc & 0xFF) or received_crc[1] != ((crc >> 8) & 0xFF):
        return None

    return hdr[0], hdr[1], hdr[2], bytes(payload)


def find_frame(buf: bytearray):
    """
    Scan buf for the first valid LCP frame (7E 7E … crc_lo crc_hi).
    Returns (frame_start, frame_end, parsed) where parsed is the parse() result,
    or (None, None, None) if no complete valid frame is found yet.
    Discards leading garbage bytes up to (but not including) any 7E 7E pair found.
    """
    i = 0
    while i < len(buf) - 1:
        if buf[i] == LCP_SYNC and buf[i + 1] == LCP_SYNC:
            # Try to parse starting here. Minimum frame: 2 sync + 4 hdr + 0 data + 2 crc = 8 bytes
            # Worst case with all-escaped 8-byte min: 2 + 2*4 + 2*2 = 2 + 8 + 4 = 14 bytes.
            # Try increasing lengths until we can determine validity.
            for end in range(i + 8, min(i + 600, len(buf) + 1)):
                result = parse(buf[i:end])
                if result is not None:
                    return i, end, result
                # If the slice is long enough to contain a valid frame but parse failed,
                # keep extending — the frame might just be longer.
                # Stop if we've gone clearly too far (>2+2*(4+255+2) = 526 bytes).
                if end - i > 526:
                    break
            # No valid frame starting at i; skip past this 7E 7E and look again
            i += 2
        else:
            i += 1
    return None, None, None


def selftest() -> int:
    """Verify against the protocol document test vectors. Returns failure count."""
    vectors = [
        # (to, from_, status, data, expected_bytes)
        (0xFA, 0xFF, 0x02, b'\x00', bytes([0x7E,0x7E,0xFA,0xFF,0x02,0x01,0x00,0x2F,0x34])),
        (0x01, 0x14, 0x02, b'\x00', bytes([0x7E,0x7E,0x01,0x14,0x02,0x01,0x00,0xC4,0xEB])),
        (0xFA, 0xFF, 0x00, b'\x20\x02', bytes([0x7E,0x7E,0xFA,0xFF,0x00,0x02,0x20,0x02,0xD4,0x2F])),
        (0x02, 0x14, 0x01, b'\x20\x02', bytes([0x7E,0x7E,0x02,0x14,0x01,0x02,0x20,0x02,0xAB,0x56])),
        # field 0x1B must be escaped
        (0x01, 0x14, 0x01, b'\x20\x1B', bytes([0x7E,0x7E,0x01,0x14,0x01,0x02,0x20,0x1B,0x1B,0xFA,0x66])),
    ]
    fails = 0
    for to, from_, st, data, expected in vectors:
        frame = build(to, from_, st, data)
        if frame != expected:
            fails += 1
            continue
        result = parse(frame)
        if result is None or result[0] != to or result[3] != data:
            fails += 1
    return fails
