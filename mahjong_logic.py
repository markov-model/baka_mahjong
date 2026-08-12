from collections import Counter

def get_tile_name(tile):
    """数字牌 (1~13) を 1筒~13筒 の文字列に変換"""
    if isinstance(tile, int) and 1 <= tile <= 13:
        return f"{tile}筒"
    return str(tile)

def format_big_number(num):
    """大数単位（万・億・兆・京…無量大数）へ変換"""
    if num == 0: 
        return "0"
    units = ["", "万", "億", "兆", "京", "垓", "𥝱", "穣", "溝", "澗", "正", "載", "極", "恒河沙", "阿僧祇", "那由他", "不可不思議", "無量大数"]
    unit_idx = 0
    res = ""
    is_negative = num < 0
    num = abs(num)
    
    while num > 0 and unit_idx < len(units):
        part = num % 10000
        if part > 0:
            res = f"{part}{units[unit_idx]}" + res
        num //= 10000
        unit_idx += 1
    return ("-" if is_negative else "") + (res if res else "0")

def is_mentsu_all(counts):
    """面子（刻子・順子）の再帰チェック"""
    for t in range(1, 14):
        if counts[t] > 0:
            if counts[t] >= 3:
                counts[t] -= 3
                if is_mentsu_all(counts):
                    counts[t] += 3
                    return True
                counts[t] += 3
            if t <= 11 and counts[t+1] >= 1 and counts[t+2] >= 1:
                counts[t] -= 1
                counts[t+1] -= 1
                counts[t+2] -= 1
                if is_mentsu_all(counts):
                    counts[t] += 1
                    counts[t+1] += 1
                    counts[t+2] += 1
                    return True
                counts[t] += 1
                counts[t+1] += 1
                counts[t+2] += 1
            return False
    return True

YAKUMAN_BASE_SCORE = 32000  # 役満の基本点。ダブル役満=64000、トリプル=128000...と役満数ごとに倍になる

def _regular_score(total_han):
    """役満未満の通常手の点数（実際の麻雀の満貫～数え役満の刻みを踏襲した早見表）"""
    if total_han <= 1:
        return 1000
    if total_han == 2:
        return 2000
    if total_han == 3:
        return 4000
    if total_han <= 5:
        return 8000    # 満貫
    if total_han <= 7:
        return 12000   # 跳満
    if total_han <= 10:
        return 16000   # 倍満
    if total_han <= 12:
        return 24000   # 三倍満
    return YAKUMAN_BASE_SCORE  # 13翻以上は数え役満扱い（呼び出し元でyakuman_countに変換される）

def evaluate_hand(hand_tiles, meld_kans=[], kan_count=0, dora_indicators=[], riichi=False):
    """手牌の役判定および点数計算
    meld_kans: 副露牌を実枚数分展開したリスト（ポンは3枚、カン/暗槓は4枚）
    kan_count: 実際のカン（明槓+暗槓）の副露数
    riichi: リーチが成立しているか（1翻加算）
    """
    all_tiles = hand_tiles + meld_kans
    counts = Counter(all_tiles)
    hand_counts = Counter(hand_tiles)
    unique_tiles = len(counts)

    yaku = []
    yakuman_count = 0

    # 国士無双 (1~13筒が全種揃っている)
    if unique_tiles == 13 and all(counts[i] >= 1 for i in range(1, 14)):
        return True, ["🀄 国士無双 (役満)"], YAKUMAN_BASE_SCORE

    # 清老頭 (1筒と13筒のみ)
    if all(t in (1, 13) for t in all_tiles):
        return True, ["🀄 清老頭 (役満)"], YAKUMAN_BASE_SCORE

    # 七対子 (14枚で異なる7組の対子)
    is_chiitoi = (len(hand_tiles) == 14 and len(hand_counts) == 7 and all(c == 2 for c in hand_counts.values()))

    # 標準面子手
    is_standard_win = False
    temp_hand_counts = hand_counts.copy()
    for head in range(1, 14):
        if temp_hand_counts[head] >= 2:
            temp_hand_counts[head] -= 2
            if is_mentsu_all(temp_hand_counts.copy()):
                is_standard_win = True
                temp_hand_counts[head] += 2
                break
            temp_hand_counts[head] += 2

    if not (is_standard_win or is_chiitoi):
        return False, [], 0

    # 大四喜 / 小四喜 (1~4筒を風牌とみなす)
    sushi_triplets = sum(1 for t in (1, 2, 3, 4) if counts[t] >= 3)
    sushi_pair = sum(1 for t in (1, 2, 3, 4) if counts[t] == 2)
    
    if sushi_triplets == 4:
        yakuman_count += 2
        yaku.append("🀄 大四喜 (ダブル役満)")
    elif sushi_triplets == 3 and sushi_pair == 1:
        yakuman_count += 1
        yaku.append("🀄 小四喜 (役満)")

    # 大三元 (11~13筒を三元牌とみなす)
    sangen_triplets = sum(1 for t in (11, 12, 13) if counts[t] >= 3)
    if sangen_triplets == 3:
        yakuman_count += 1
        yaku.append("🀄 大三元 (役満)")

    # 四槓子
    if kan_count >= 4:
        yakuman_count += 2
        yaku.append("🀄 四槓子 (ダブル役満)")

    # 四暗刻
    triplets = sum(1 for c in counts.values() if c >= 3)
    if triplets == 4 and kan_count == 0:
        yakuman_count += 1
        yaku.append("🀄 四暗刻 (役満)")

    # 通常役
    total_han = 6
    yaku.append("清一色 (6翻)")

    honroto_tiles = (1, 13, 2, 3, 4, 11, 12, 13)
    if all(t in honroto_tiles for t in all_tiles):
        total_han += 2
        yaku.append("混老頭 (2翻)")

    if is_chiitoi:
        total_han += 2
        yaku.append("七対子 (2翻)")
    elif triplets > 0 and yakuman_count == 0:
        total_han += 2
        yaku.append("対々和 (2翻)")

    if riichi:
        total_han += 1
        yaku.append("リーチ (1翻)")

    dora_count = sum(all_tiles.count(d_tile) for d_tile in dora_indicators)
    if dora_count > 0:
        total_han += dora_count
        yaku.append(f"ドラ x{dora_count} ({dora_count}翻)")

    if total_han >= 13 and yakuman_count == 0:
        additional_yakuman = total_han // 13
        yakuman_count += additional_yakuman
        yaku.append(f"🀄 数え役満 ({total_han}翻)")

    if yakuman_count == 0:
        score = _regular_score(total_han)
    else:
        score = YAKUMAN_BASE_SCORE * (2 ** (yakuman_count - 1))

    return True, yaku, score