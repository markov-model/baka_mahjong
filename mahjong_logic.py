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
    """面子（刻子のみ）の再帰チェック。
    このゲームの牌は1〜13の各値が「1筒/9筒/1索/9索/1萬/9萬/東南西北/白發中」に
    固定でマッピングされており、どの隣接する2つの値も同じ種類（筒/索/萬）の
    連番にはならない（各種類は老頭牌1・9のみで、2〜8が存在しない）ため、
    実際の麻雀としての順子は成立し得ない。よって刻子のみを判定する。"""
    for t in range(1, 14):
        if counts[t] > 0:
            if counts[t] >= 3:
                counts[t] -= 3
                if is_mentsu_all(counts):
                    counts[t] += 3
                    return True
                counts[t] += 3
            return False
    return True

# ====================================================
# 牌の種類分け（TILE_MAP: script.js と対応）
# 1:1筒 2:9筒 / 3:1索 4:9索 / 5:1萬 6:9萬 / 7:東 8:南 9:西 10:北 / 11:白 12:發 13:中
# ====================================================
PINZU_TILES = (1, 2)
SOUZU_TILES = (3, 4)
MANZU_TILES = (5, 6)
SUIT_GROUPS = (PINZU_TILES, SOUZU_TILES, MANZU_TILES)
WIND_TILES = (7, 8, 9, 10)
DRAGON_TILES = (11, 12, 13)
HONOR_TILES = WIND_TILES + DRAGON_TILES
TERMINAL_TILES = PINZU_TILES + SOUZU_TILES + MANZU_TILES  # このゲームでは老頭牌のみなので么九牌=全牌

WIND_NAMES = {7: '東', 8: '南', 9: '西', 10: '北'}
DRAGON_NAMES = {11: '白', 12: '發', 13: '中'}

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

def evaluate_hand(
    hand_tiles,
    meld_kans=[],
    kan_count=0,
    ankan_tiles=[],
    open_meld_tiles=[],
    dora_indicators=[],
    riichi=False,
    double_riichi=False,
    ippatsu=False,
    is_tsumo=False,
    is_rinshan=False,
    is_haitei=False,
    is_houtei=False,
    is_tenhou=False,
    is_chiihou=False,
    is_renhou=False,
    seat_wind=None,
    round_wind=7,
    winning_tile=None
):
    """手牌の役判定および点数計算

    meld_kans: 副露牌を実枚数分展開したリスト（ポンは3枚、カン/暗槓は4枚）
    kan_count: 実際のカン（明槓+暗槓）の副露数
    ankan_tiles: 暗槓の牌のみを実枚数分展開したリスト（三暗刻/四暗刻の面前判定用）
    open_meld_tiles: ポン・明槓（鳴いた副露）の牌のみを実枚数分展開したリスト（面前判定用）
    riichi / double_riichi / ippatsu: リーチ・ダブルリーチ・一発
    is_tsumo: 自摸和了かどうか（ツモ・嶺上開花・海底摸月の判定に使用）
    is_rinshan: 槓の直後の嶺上牌で和了したか
    is_haitei / is_houtei: 海底摸月（ツモ）／河底撈魚（ロン）
    is_tenhou / is_chiihou / is_renhou: 天和・地和・人和
    seat_wind / round_wind: 自風・場風の牌番号（7〜10）。役牌・連風牌の判定に使用
    winning_tile: 和了牌（国士無双十三面待ちの判定に使用）
    """
    all_tiles = hand_tiles + meld_kans
    counts = Counter(all_tiles)
    hand_counts = Counter(hand_tiles)
    unique_tiles = len(counts)

    yaku = []
    yakuman_count = 0

    # 国士無双 (1~13が全種類1枚以上)
    if unique_tiles == 13 and all(counts[i] >= 1 for i in range(1, 14)):
        # 和了牌が「既に1枚持っていた牌」＝2枚目なら、和了前の13枚は全種類1枚ずつ
        # (=13面待ち)。和了牌で初めて13種類目が揃ったのなら単騎ではない通常の国士。
        if winning_tile is not None and counts[winning_tile] == 2:
            return True, ["🀄 国士無双十三面待ち (ダブル役満)"], YAKUMAN_BASE_SCORE * 2
        return True, ["🀄 国士無双 (役満)"], YAKUMAN_BASE_SCORE

    # 清老頭 (老頭牌＝1・2・3・4・5・6のみ。7以降の字牌が混ざらない)
    if all(t in TERMINAL_TILES for t in all_tiles):
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

    # ---- ここから役満（複数成立する場合は加算し、最後にまとめて倍率計算する）----

    # 字一色 (字牌＝風牌・三元牌のみ)
    if all(t in HONOR_TILES for t in all_tiles):
        yakuman_count += 1
        yaku.append("🀄 字一色 (役満)")

    # 白一色・緑一色（發のみ）・赤一色（中のみ）：単一の字牌だけで揃えた特殊役満
    if unique_tiles == 1:
        only_tile = next(iter(counts))
        if only_tile == 11:
            yakuman_count += 1
            yaku.append("🀄 白一色 (役満)")
        elif only_tile == 12:
            yakuman_count += 1
            yaku.append("🀄 緑一色 (役満)")
        elif only_tile == 13:
            yakuman_count += 1
            yaku.append("🀄 赤一色 (役満)")

    # 大四喜 / 小四喜 (風牌=7〜10)
    wind_triplets = sum(1 for t in WIND_TILES if counts[t] >= 3)
    wind_pair = sum(1 for t in WIND_TILES if counts[t] == 2)

    if wind_triplets == 4:
        yakuman_count += 2
        yaku.append("🀄 大四喜 (ダブル役満)")
    elif wind_triplets == 3 and wind_pair == 1:
        yakuman_count += 1
        yaku.append("🀄 小四喜 (役満)")

    # 大三元 (三元牌=11〜13)
    dragon_triplets = sum(1 for t in DRAGON_TILES if counts[t] >= 3)
    if dragon_triplets == 3:
        yakuman_count += 1
        yaku.append("🀄 大三元 (役満)")

    # 四槓子
    if kan_count >= 4:
        yakuman_count += 2
        yaku.append("🀄 四槓子 (ダブル役満)")

    # 四暗刻（暗刻＝手牌のみ、または暗槓の牌で構成された刻子。鳴いた牌は含めない）
    concealed_counts = Counter(hand_tiles) + Counter(ankan_tiles)
    concealed_triplets = sum(1 for c in concealed_counts.values() if c >= 3)
    if concealed_triplets == 4:
        yakuman_count += 1
        yaku.append("🀄 四暗刻 (役満)")

    # 天和 / 地和 / 人和
    if is_tenhou:
        yakuman_count += 1
        yaku.append("🀄 天和 (役満)")
    elif is_chiihou:
        yakuman_count += 1
        yaku.append("🀄 地和 (役満)")
    elif is_renhou:
        yakuman_count += 1
        yaku.append("🀄 人和 (役満)")

    # ---- ここから通常役 ----
    total_han = 0

    # 清一色／混一色（このゲームの牌はすべて么九牌のため、混老頭は常に成立する前提でここでは扱わない）
    present_suits = [s for s in SUIT_GROUPS if any(counts[t] for t in s)]
    has_honors = any(counts[t] for t in HONOR_TILES)
    if len(present_suits) == 1 and not has_honors:
        total_han += 6
        yaku.append("清一色 (6翻)")
    elif len(present_suits) == 1 and has_honors:
        total_han += 3
        yaku.append("混一色 (3翻)")

    # 混老頭（老頭牌+字牌のみで構成。このゲームは么九牌しか存在しないため常に成立するが、
    # ルール名として明示しておく）
    total_han += 2
    yaku.append("混老頭 (2翻)")

    if is_chiitoi:
        total_han += 2
        yaku.append("七対子 (2翻)")

    total_triplets = sum(1 for c in counts.values() if c >= 3)
    if not is_chiitoi and total_triplets > 0 and yakuman_count == 0:
        total_han += 2
        yaku.append("対々和 (2翻)")

    # 三暗刻
    if concealed_triplets == 3 and yakuman_count == 0:
        total_han += 2
        yaku.append("三暗刻 (2翻)")

    # 三色同刻（1筒+1索+1萬の刻子、または9筒+9索+9萬の刻子）
    if (counts[1] >= 3 and counts[3] >= 3 and counts[5] >= 3) or (counts[2] >= 3 and counts[4] >= 3 and counts[6] >= 3):
        total_han += 2
        yaku.append("三色同刻 (2翻)")

    # 役牌（三元牌の刻子は常に1翻。風牌の刻子は場風・自風と一致する分だけ加算され、
    # 両方一致（＝親の東等）すると連風牌として2翻になる）
    for dragon in DRAGON_TILES:
        if counts[dragon] >= 3:
            total_han += 1
            yaku.append(f"役牌({DRAGON_NAMES[dragon]}) (1翻)")

    for wind in WIND_TILES:
        if counts[wind] >= 3:
            bonus = 0
            if seat_wind is not None and wind == seat_wind:
                bonus += 1
            if wind == round_wind:
                bonus += 1
            if bonus == 2:
                total_han += 2
                yaku.append(f"連風牌({WIND_NAMES[wind]}) (2翻)")
            elif bonus == 1:
                total_han += 1
                yaku.append(f"役牌({WIND_NAMES[wind]}) (1翻)")

    # 小三元（大三元に満たない、三元牌2刻子+アタマ1つ）
    dragon_pair = sum(1 for t in DRAGON_TILES if counts[t] == 2)
    if dragon_triplets == 2 and dragon_pair == 1 and yakuman_count == 0:
        total_han += 2
        yaku.append("小三元 (2翻)")

    # 面前ツモ（ポン・明槓が無ければ面前。暗槓は面前扱いのため含めない）
    is_closed_hand = len(open_meld_tiles) == 0
    if is_tsumo and is_closed_hand and yakuman_count == 0:
        total_han += 1
        yaku.append("ツモ (1翻)")

    # 嶺上開花・海底摸月・河底撈魚
    if is_rinshan and yakuman_count == 0:
        total_han += 1
        yaku.append("嶺上開花 (1翻)")
    if is_tsumo and is_haitei and yakuman_count == 0:
        total_han += 1
        yaku.append("海底摸月 (1翻)")
    if (not is_tsumo) and is_houtei and yakuman_count == 0:
        total_han += 1
        yaku.append("河底撈魚 (1翻)")

    # リーチ／ダブルリーチ／一発
    if double_riichi and yakuman_count == 0:
        total_han += 2
        yaku.append("ダブルリーチ (2翻)")
    elif riichi and yakuman_count == 0:
        total_han += 1
        yaku.append("リーチ (1翻)")

    if ippatsu and riichi and yakuman_count == 0:
        total_han += 1
        yaku.append("一発 (1翻)")

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
