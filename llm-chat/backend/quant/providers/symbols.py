"""股票代码格式归一化 — 内部统一使用带交易所后缀的格式（000001.SZ / 600000.SH）"""
from __future__ import annotations


def to_internal(code: str) -> str:
    """6 位代码（或已带后缀）→ 内部格式 000001.SZ / 600000.SH / AAPL.US。"""
    code = str(code).strip().upper()
    if "." in code:
        return code
    if not code:
        return ""
    
    # 字母开头通常是美股或港股，这里先统一归为 .US
    if code[0].isalpha():
        return f"{code}.US"

    if not code.isdigit():
        return ""
        
    code = code.zfill(6)
    if code.startswith(("60", "68", "90", "11", "13", "50", "51", "52", "56", "58")):
        return f"{code}.SH"
    if code.startswith(("00", "30", "20", "15", "16", "18")):
        return f"{code}.SZ"
    if code.startswith(("8", "4", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def to_akshare_code(symbol: str) -> str:
    """带后缀格式 → AKShare 代码（000001.SZ → 000001; AAPL.US → AAPL）"""
    if symbol.endswith(".US"):
        return symbol.split(".")[0]
    return symbol.split(".")[0] if "." in symbol else symbol


def market_of(symbol: str) -> str:
    """提取交易所后缀（SH / SZ / BJ / US）"""
    return symbol.split(".")[-1].upper() if "." in symbol else ""
