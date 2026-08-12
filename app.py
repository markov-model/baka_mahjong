# eventlet を使う場合は、他の何よりも先に monkey_patch を行う必要がある。
# これを怠ると、time.sleep() 等の標準ライブラリ呼び出しが eventlet のイベントループ全体を
# ブロックしてしまい、バックグラウンドタスク（ロン/鳴き待ちタイマー等）からの
# 状態更新が他のクライアントに届かない・遅延する不具合の原因になる。
try:
    import eventlet
    eventlet.monkey_patch()
except ImportError:
    pass

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import time
import random
import os

# app.py の先頭に追加
from mahjong_logic import evaluate_hand, format_big_number


app = Flask(__name__)
app.config['SECRET_KEY'] = 'mahjong_secret_key_2026'

socketio = SocketIO(app, cors_allowed_origins="*")

rooms = {}
KOKUSHI_TILES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
WIND_TO_TILE = {'東': 7, '南': 8, '西': 9, '北': 10}
ROUND_WIND_TILE = 7  # 東風戦なので場風は常に東

# 得点スケール（実際の麻雀に近いスケール）
INITIAL_SCORE = 100000      # 初期持ち点: 10万点
CHOMBO_PENALTY = 32000      # チョンボ罰符: 役満と同スケール
NAGASHI_MANGAN_SCORE = 8000  # 流しマンガンは満貫相当
NOTEN_PENALTY_TOTAL = 3000   # 流局時のテンパイ料（聴牌者で総取り、ノーテン者で均等負担）
RIICHI_STICK_COST = 10000   # リーチ棒: リーチ宣言時に場に供託する点数

# バグ報告ログ（メモリ上に保持。サーバー再起動で消える簡易版）
bug_reports = []

def create_initial_deck(num_players=4):
    # 52枚×人数分（1種類あたり 4×人数枚）。4人なら従来通り 13種 x 16枚 = 208枚
    copies_per_tile = 1 * max(1, num_players)
    deck = [tile for tile in KOKUSHI_TILES for _ in range(copies_per_tile)]
    random.shuffle(deck)
    return deck

def apply_chombo(room, room_id, offender, reason=""):
    """チョンボ罰符処理。offenderから罰符を引き、他家へ均等に分配する。
    socketio.emit を使うため、リクエストコンテキスト外（バックグラウンドスレッド）からも呼び出せる。
    reason はバグ報告機能用に、直近のチョンボ理由として offender に記録する。"""
    other_players = [p for p in room['players'] if p['id'] != offender['id']]
    offender['score'] -= CHOMBO_PENALTY
    if other_players:
        share = CHOMBO_PENALTY // len(other_players)
        for p in other_players:
            p['score'] += share
    offender['last_chombo_info'] = {
        'reason': reason,
        'hand': list(offender['hand']),
        'melds': [dict(m) for m in offender['melds']],
    }
    socketio.emit('system_msg', {'message': f"🚨 チョンボ！ {offender['name']} さんの錯和です（-{format_big_number(CHOMBO_PENALTY)}点）"}, room=room_id)

def _assign_winds(room):
    """dealer_idx を起点に、席順(players配列の並び)から風(東南西北)を割り当てる"""
    winds = ['東', '南', '西', '北']
    dealer_idx = room.get('dealer_idx', 0)
    for i, p in enumerate(room['players']):
        p['wind'] = winds[(i - dealer_idx) % 4]

def _deal_new_hand(room):
    """山をシャッフルし直し、現在の dealer_idx を起家として配牌する"""
    room['status'] = 'playing'
    room['deck'] = create_initial_deck(len(room['players']))
    room['dora_indicators'] = [room['deck'].pop()] if room['deck'] else []
    room['last_discard'] = None
    room['last_discard_player'] = None
    room['discard_seq'] = 0
    room['winning_tile'] = None
    room['ron_claims'] = []
    room['any_call_happened'] = False    # ポン/明槓が一度でも成立したか（ダブルリーチ・天和地和人和の判定用）
    room['first_decision_pending'] = True  # まだ誰も打牌していない（天和判定用）

    for p in room['players']:
        p['hand'] = [room['deck'].pop() for _ in range(13)]
        p['hand'].sort()
        p['kawa'] = []
        p['melds'] = []
        p['discards_called'] = False  # 流しマンガン判定用：この局で自分の捨て牌が鳴かれたか
        p['riichi'] = False
        p['drawn_tile'] = None          # 直近でツモった牌（河・手牌表示のツモ切り判定用）
        p['pending_riichi_discard'] = False  # リーチ宣言直後の次の打牌かどうか
        p['riichi_tile_index'] = None   # 河の中でリーチ宣言牌にあたるインデックス（横向き表示用のみ。鳴かれるとNoneに戻ることがある）
        p['riichi_committed'] = False   # リーチ宣言牌を実際に打牌済みか（鳴かれてもリセットされない、取消可否・ツモ切り固定の判定用）
        p['rinshan_chance'] = False     # 槓の直後の嶺上牌をツモった直後かどうか（嶺上開花判定用）
        p['ippatsu_active'] = False     # リーチ後、一発が生きているか
        p['double_riichi'] = False      # ダブルリーチが成立しているか
        p['temp_furiten'] = False       # 同巡内フリテン（ロンできたのに見送った）

    room['current_turn'] = room['dealer_idx']
    active_player = room['players'][room['current_turn']]
    if room['deck']:
        drawn = room['deck'].pop()
        active_player['hand'].append(drawn)
        active_player['hand'].sort()
        active_player['drawn_tile'] = drawn

def _advance_to_next_hand(room, room_id):
    """現在の局を終える。親が連荘（自分の和了、または流局時に聴牌）していれば
    同じ親のまま本場だけ増やして局を続ける。そうでなければ親を次の席に送り、
    東風戦（参加人数分の局）を全て消化していれば対局終了とする。
    スレッド（山切れによる流局）からも呼ばれるため、socketio.emit のみを使う。"""
    if room.get('renchan'):
        room['honba'] = room.get('honba', 0) + 1
        _deal_new_hand(room)
        honba_text = f" {room['honba']}本場" if room['honba'] > 0 else ""
        socketio.emit('system_msg', {'message': f"🔄 親継続！ 東{room.get('hand_number', 1)}局{honba_text}が始まります！"}, room=room_id)
        broadcast_state(room_id)
        _auto_discard_if_disconnected(room, room_id)
        return

    room['hand_number'] = room.get('hand_number', 1) + 1
    room['honba'] = 0
    max_hands = room.get('max_hands', len(room['players']))

    if room['hand_number'] > max_hands:
        room['status'] = 'match_over'
        ranking = sorted(room['players'], key=lambda p: p['score'], reverse=True)
        socketio.emit('match_result', {
            'ranking': [{'name': p['name'], 'score_str': format_big_number(p['score'])} for p in ranking]
        }, room=room_id)
        socketio.emit('system_msg', {'message': '🏁 東風戦、終了です！お疲れ様でした。'}, room=room_id)
        broadcast_state(room_id)
        return

    room['dealer_idx'] = (room.get('dealer_idx', 0) + 1) % len(room['players'])
    _assign_winds(room)
    _deal_new_hand(room)

    socketio.emit('system_msg', {'message': f"🔄 東{room['hand_number']}局が始まります！"}, room=room_id)
    broadcast_state(room_id)
    _auto_discard_if_disconnected(room, room_id)

def _get_winning_tiles(player):
    """13枚の手牌（+副露）に対し、1〜13の牌を1枚ずつ加えてアガリになるものを総当たりで列挙する（＝待ち牌一覧）"""
    meld_tiles, kan_count, ankan_tiles, open_meld_tiles = _expand_melds(player)
    winning = []
    for t in KOKUSHI_TILES:
        is_agari, _, _ = evaluate_hand(
            player['hand'] + [t],
            meld_kans=meld_tiles,
            kan_count=kan_count,
            ankan_tiles=ankan_tiles,
            open_meld_tiles=open_meld_tiles
        )
        if is_agari:
            winning.append(t)
    return winning

def _check_tenpai(player):
    """聴牌しているかどうか（待ち牌が1つでもあるか）"""
    return len(_get_winning_tiles(player)) > 0

def _is_furiten(player):
    """フリテン判定。以下のいずれかに該当すればロン不可（ツモは可能）：
    ・自分の待ち牌が、これまでの自分の捨て牌の中に含まれている（一度でも該当するとその局の間ずっと有効）
    ・直前の他家の捨て牌でロンできたのに見送った（同巡の一時的フリテン。次に自分が打牌するまで、
      またはリーチ中はその局の間ずっと有効）"""
    if player.get('temp_furiten'):
        return True
    winning_tiles = _get_winning_tiles(player)
    return any(t in player['kawa'] for t in winning_tiles)

def _reveal_riichi_hands(room, room_id):
    """流局時、リーチ宣言していた全プレイヤーの手牌を公開する。
    聴牌していなければ「リーチしたのに聴牌でない」＝不正リーチとしてチョンボを適用する。"""
    revealed = []
    for p in room['players']:
        if not p.get('riichi'):
            continue
        is_tenpai = _check_tenpai(p)
        revealed.append({'name': p['name'], 'hand': list(p['hand']), 'is_tenpai': is_tenpai})
        if not is_tenpai:
            apply_chombo(room, room_id, p, reason="リーチ宣言していたが流局時にノーテンだった（不正リーチ）")
    return revealed

def _handle_exhaustive_draw(room, room_id):
    """山切れによる流局処理。まずリーチ宣言者の手牌を公開してノーテンなら罰する。
    次に流しマンガン成立者を判定し、いなければ聴牌/ノーテン料を精算する。
    Ron/Tsumoと同様に game_over 状態で一旦停止し、次局への進行はプレイヤーの「次の局へ」操作を待つ。
    スレッド（wait_and_advance）から呼ばれるため socketio.emit のみを使う。"""
    room['status'] = 'game_over'
    room['winning_tile'] = None
    dealer_id = room['players'][room.get('dealer_idx', 0)]['id']

    riichi_reveal = _reveal_riichi_hands(room, room_id)

    # 流しマンガン：自分の捨て牌が一度も鳴かれていない（このゲームの牌は全て么九牌相当のため条件はこれのみ）
    nagashi_players = [p for p in room['players'] if p['kawa'] and not p.get('discards_called')]

    if nagashi_players:
        nagashi_ids = {p['id'] for p in nagashi_players}
        room['renchan'] = dealer_id in nagashi_ids  # 親が流しマンガン成立＝連荘
        score = NAGASHI_MANGAN_SCORE
        other_count = max(1, len(room['players']) - 1)

        for winner in nagashi_players:
            per_player_score = score // other_count
            for p in room['players']:
                if p['id'] != winner['id']:
                    p['score'] -= per_player_score
            winner['score'] += score

        results = [{
            'name': p['name'],
            'is_nagashi': p['id'] in nagashi_ids,
            'score_str': format_big_number(p['score'])
        } for p in room['players']]

        names = '・'.join(p['name'] for p in nagashi_players)
        socketio.emit('draw_result', {
            'type': 'nagashi_mangan',
            'players': results,
            'riichi_reveal': riichi_reveal
        }, room=room_id)
        socketio.emit('system_msg', {'message': f"🌊 流局！ 流しマンガン成立（{names} さんに{format_big_number(score)}点）"}, room=room_id)
    else:
        tenpai_players = [p for p in room['players'] if _check_tenpai(p)]
        tenpai_ids = {p['id'] for p in tenpai_players}
        noten_players = [p for p in room['players'] if p['id'] not in tenpai_ids]
        room['renchan'] = dealer_id in tenpai_ids  # 親が聴牌のまま流局＝連荘

        if tenpai_players and noten_players:
            pay_each = NOTEN_PENALTY_TOTAL // len(noten_players)
            gain_each = NOTEN_PENALTY_TOTAL // len(tenpai_players)
            for p in noten_players:
                p['score'] -= pay_each
            for p in tenpai_players:
                p['score'] += gain_each

        results = [{
            'name': p['name'],
            'is_tenpai': p['id'] in tenpai_ids,
            'score_str': format_big_number(p['score'])
        } for p in room['players']]

        socketio.emit('draw_result', {
            'type': 'normal',
            'players': results,
            'riichi_reveal': riichi_reveal
        }, room=room_id)
        socketio.emit('system_msg', {'message': '🌊 流局（山切れ）。テンパイ料を精算しました。'}, room=room_id)

    broadcast_state(room_id)

def _finalize_ron_claims(room, room_id):
    """3秒のロン受付ウィンドウが閉じた時点で、成立している全てのロン宣言をまとめて精算する
    （ダブロン・トリプルロン対応）。スレッドから呼ばれるため socketio.emit のみを使う。"""
    claims = room.get('ron_claims', [])
    room['ron_claims'] = []
    room['status'] = 'game_over'

    loser = next((p for p in room['players'] if p['id'] == room['last_discard_player']), None)
    winning_tile = room['last_discard']
    loser_name = loser['name'] if loser else "他家"

    winners_payload = []
    for claim in claims:
        winner = next((p for p in room['players'] if p['id'] == claim['id']), None)
        if not winner:
            continue
        winner['hand'].append(winning_tile)
        winner['hand'].sort()
        if loser:
            loser['score'] -= claim['score']
        winner['score'] += claim['score']
        winners_payload.append({
            'winner': winner['name'],
            'yaku': claim['yaku'],
            'score_str': format_big_number(claim['score'])
        })

    room['winning_tile'] = winning_tile if winners_payload else None

    if winners_payload:
        # 供託されているリーチ棒を和了者へ渡す（ダブロン・トリプルロンの場合は均等分配）
        sticks = room.get('riichi_sticks', 0)
        if sticks > 0:
            stick_total = sticks * RIICHI_STICK_COST
            winners = [p for p in room['players'] if p['id'] in {c['id'] for c in claims}]
            if winners:
                share = stick_total // len(winners)
                for w in winners:
                    w['score'] += share
                room['riichi_sticks'] = 0

    dealer_id = room['players'][room.get('dealer_idx', 0)]['id']
    room['renchan'] = any(claim['id'] == dealer_id for claim in claims)

    if len(winners_payload) >= 2:
        names = '・'.join(w['winner'] for w in winners_payload)
        label = 'トリプルロン' if len(winners_payload) >= 3 else 'ダブロン'
        socketio.emit('win_result', {
            'type': label,
            'loser': loser_name,
            'winning_tile': winning_tile,
            'winners': winners_payload
        }, room=room_id)
        socketio.emit('system_msg', {'message': f"🀄 {label}！ {names} さんが {loser_name} さんからアガリました！"}, room=room_id)
    elif len(winners_payload) == 1:
        w = winners_payload[0]
        socketio.emit('win_result', {
            'winner': w['winner'],
            'loser': loser_name,
            'yaku': claims[0]['yaku'],
            'score_str': w['score_str'],
            'type': 'ロン',
            'winning_tile': winning_tile
        }, room=room_id)
        socketio.emit('system_msg', {'message': f"🀄 ロン！ {w['winner']} さんが {loser_name} さんからアガリました！（{w['score_str']}点）"}, room=room_id)

    broadcast_state(room_id)

def _all_disconnected(room):
    return all(p.get('disconnected') for p in room['players'])

def _perform_discard(room, room_id, player, tile_to_remove):
    """打牌を確定し、ロン/鳴き待ちタイマーを開始する（人間の打牌・切断者の自動打牌の共通処理）"""
    player['hand'].remove(tile_to_remove)
    player['kawa'].append(tile_to_remove)
    player['drawn_tile'] = None
    player['rinshan_chance'] = False  # 嶺上牌をツモった直後のチャンスは、打牌したら消える
    room['first_decision_pending'] = False  # 誰かが1回でも打牌したら天和のチャンスは消える

    if player.get('pending_riichi_discard'):
        # リーチ宣言後、最初の打牌＝リーチ宣言牌として河の中の位置を記録する（横向き表示用）
        player['riichi_tile_index'] = len(player['kawa']) - 1
        player['pending_riichi_discard'] = False
        # riichi_committed は宣言牌を鳴かれても絶対にリセットしない（取消可否・ツモ切り固定判定の正本）
        player['riichi_committed'] = True
        # リーチ棒を場に供託する（宣言牌を実際に切った時点で確定。取消時はここに到達しないので供託されない）
        player['score'] -= RIICHI_STICK_COST
        room['riichi_sticks'] = room.get('riichi_sticks', 0) + 1
    elif player.get('riichi'):
        # リーチ宣言牌以降の打牌＝次巡に入った、ということなので一発は消える
        player['ippatsu_active'] = False

    if not player.get('riichi'):
        # 同巡内フリテンは自分の打牌で解消する。ただしリーチ中は待ちを変えられないため
        # 一度フリテンになったらその局の間ずっと有効（実際の麻雀のルール通り）
        player['temp_furiten'] = False

    room['last_discard'] = tile_to_remove
    room['last_discard_player'] = player['id']
    room['ron_claims'] = []

    room['status'] = 'waiting_action'
    # この打牌固有の連番を発行する。牌の「数字」ではなく連番で照合することで、
    # 同じ数字の牌が別タイミングで捨てられた際に誤って別局面のターンを
    # 進めてしまう競合（ターンずれ）を防ぐ。
    room['discard_seq'] += 1
    current_seq = room['discard_seq']
    broadcast_state(room_id)

    # socketio.start_background_task を使う（生の threading.Thread だと、本番の
    # eventlet 動作モードでイベントループと協調できず、更新が届かないことがある）
    socketio.start_background_task(wait_and_advance, room_id, current_seq)

def wait_and_advance(target_room_id, expected_seq):
    time.sleep(3.0)
    target_room = rooms.get(target_room_id)
    if not target_room:
        return

    if target_room['status'] == 'waiting_action' and target_room['discard_seq'] == expected_seq:
        # ロンできたはずなのに見送った（宣言しなかった）プレイヤーは同巡内フリテンにする
        discarded_tile = target_room['last_discard']
        discarder_id = target_room['last_discard_player']
        claimed_ids = {c['id'] for c in target_room.get('ron_claims', [])}
        for p in target_room['players']:
            if p['id'] == discarder_id or p['id'] in claimed_ids:
                continue
            if discarded_tile in _get_winning_tiles(p):
                p['temp_furiten'] = True

        if target_room.get('ron_claims'):
            # ロン受付ウィンドウが閉じた：成立している宣言をまとめて精算する（ダブロン・トリプルロン対応）
            _finalize_ron_claims(target_room, target_room_id)
            return

        target_room['status'] = 'playing'
        target_room['last_discard'] = None
        target_room['last_discard_player'] = None

        if not target_room['deck']:
            # 山が尽きた：流局処理（流しマンガン／聴牌判定）を行い、結果を演出する
            _handle_exhaustive_draw(target_room, target_room_id)
            return

        target_room['current_turn'] = (target_room['current_turn'] + 1) % len(target_room['players'])
        next_player = target_room['players'][target_room['current_turn']]
        drawn = target_room['deck'].pop()
        next_player['hand'].append(drawn)
        next_player['hand'].sort()
        next_player['drawn_tile'] = drawn

        broadcast_state(target_room_id)
        _auto_discard_if_disconnected(target_room, target_room_id)

def _auto_discard_if_disconnected(room, room_id):
    """手番のプレイヤーが切断済みなら、先頭の牌を自動で打牌して進行を止めない。
    全員切断済みの場合は対局を止める（無駄なスレッド連鎖を防ぐ）。"""
    if room['status'] != 'playing':
        return
    if _all_disconnected(room):
        return
    current_player = room['players'][room['current_turn']]
    if not current_player.get('disconnected') or not current_player['hand']:
        return
    # ツモ切り（直近でツモった牌をそのまま切る）を優先する。手牌に無ければ先頭の牌を切る
    drawn = current_player.get('drawn_tile')
    tile_to_discard = drawn if drawn in current_player['hand'] else current_player['hand'][0]
    _perform_discard(room, room_id, current_player, tile_to_discard)

def check_agari(hand):
    if len(hand) != 14:
        return False
    unique_tiles = set(hand)
    if len(unique_tiles) != 13:
        return False
    for tile in unique_tiles:
        if tile not in KOKUSHI_TILES:
            return False
    return True

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return ('', 204)

def _relative_direction(room, caller, discarder):
    """caller から見て discarder がどの方向の席か（上家/対面/下家）を返す"""
    n = len(room['players'])
    if n <= 1:
        return '不明'
    caller_idx = room['players'].index(caller)
    discarder_idx = room['players'].index(discarder)
    if discarder_idx == (caller_idx - 1) % n:
        return '上家'
    elif discarder_idx == (caller_idx + 1) % n:
        return '下家'
    else:
        return '対面'

def _remove_last_kawa_tile(player, tile):
    """河（捨て牌）の中から鳴かれた牌を1枚（末尾優先）取り除く。
    リーチ宣言牌の位置を記録している場合は、取り除いた位置に応じて追従・無効化する。"""
    if not player:
        return
    for i in range(len(player['kawa']) - 1, -1, -1):
        if player['kawa'][i] == tile:
            player['kawa'].pop(i)
            riichi_idx = player.get('riichi_tile_index')
            if riichi_idx is not None:
                if i == riichi_idx:
                    player['riichi_tile_index'] = None
                elif i < riichi_idx:
                    player['riichi_tile_index'] = riichi_idx - 1
            return

def _expand_melds(player):
    """副露を実枚数分（ポン=3枚, カン/暗槓=4枚）展開し、役判定に必要な内訳を返す。
    戻り値: (全副露牌のフラットリスト, カン数, 暗槓牌のフラットリスト, ポン/明槓牌のフラットリスト)"""
    meld_tiles = []
    kan_count = 0
    ankan_tiles = []
    open_meld_tiles = []
    for m in player['melds']:
        if m['type'] == 'ankan':
            meld_tiles.extend([m['tile']] * 4)
            kan_count += 1
            ankan_tiles.extend([m['tile']] * 4)
        elif m['type'] == 'kan':
            meld_tiles.extend([m['tile']] * 4)
            kan_count += 1
            open_meld_tiles.extend([m['tile']] * 4)
        else:  # pon
            meld_tiles.extend([m['tile']] * 3)
            open_meld_tiles.extend([m['tile']] * 3)
    return meld_tiles, kan_count, ankan_tiles, open_meld_tiles

@socketio.on('create_room')
def handle_create_room(data):
    username = data.get('username', 'ホスト')
    room_id = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ', k=4))
    
    rooms[room_id] = {
        'room_id': room_id,
        'status': 'waiting',
        'deck': [],
        'dora_indicators': [],
        'last_discard': None,
        'last_discard_player': None,
        'current_turn': 0,
        'discard_seq': 0,
        'dealer_idx': 0,
        'hand_number': 1,
        'max_hands': 1,
        'ron_claims': [],
        'players': [{
            'id': request.sid,
            'name': username,
            'is_host': True,
            'hand': [],
            'kawa': [],
            'melds': [],
            'score': INITIAL_SCORE,
            'wind': '東',
            'disconnected': False,
            'discards_called': False
        }]
    }
    # room=room_id でのブロードキャスト（system_msg/win_result等）を受け取れるように
    # Socket.IO のルームへ参加させる（デフォルトでは自分のsid名のルームにしか入っていない）
    join_room(room_id)
    emit('room_joined', {'room_id': room_id, 'is_host': True})
    send_room_update(room_id)

@socketio.on('join_room')
def handle_join_room(data):
    username = data.get('username', 'ゲスト')
    room_id = data.get('room_id', '').upper()

    if room_id not in rooms:
        emit('error_msg', {'message': 'ルームが見つかりません'})
        return

    room = rooms[room_id]

    # 再接続：同名かつ切断中の既存プレイヤーがいれば、新規プレイヤーとして追加せず
    # そのプレイヤー席に復帰させる（新しいsidへ差し替え、席・手牌・得点はそのまま維持）
    reconnecting_player = next(
        (p for p in room['players'] if p['name'] == username and p.get('disconnected')),
        None
    )
    if reconnecting_player:
        reconnecting_player['id'] = request.sid
        reconnecting_player['disconnected'] = False
        join_room(room_id)
        emit('room_joined', {'room_id': room_id, 'is_host': reconnecting_player['is_host']})
        socketio.emit('system_msg', {'message': f"🔄 {username} さんが再接続しました"}, room=room_id)
        broadcast_state(room_id)
        return

    if room['status'] != 'waiting':
        # 再接続（同名かつ切断中）以外の新規参加は、対局が始まっている部屋では受け付けない。
        # 受け付けてしまうと座席インデックスに依存した進行ロジックが壊れる。
        emit('error_msg', {'message': 'この部屋は既に対局中です'})
        return

    if len(room['players']) >= 4:
        emit('error_msg', {'message': '満員です'})
        return

    winds = ['東', '南', '西', '北']
    current_count = len(room['players'])
    
    new_player = {
        'id': request.sid,
        'name': username,
        'is_host': False,
        'hand': [],
        'kawa': [],
        'melds': [],
        'score': INITIAL_SCORE,
        'wind': winds[current_count] if current_count < 4 else '風',
        'disconnected': False,
        'discards_called': False
    }
    room['players'].append(new_player)
    join_room(room_id)
    emit('room_joined', {'room_id': room_id, 'is_host': False})
    send_room_update(room_id)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for room_id, room in list(rooms.items()):
        player = next((p for p in room['players'] if p['id'] == sid), None)
        if not player:
            continue

        if room['status'] == 'waiting':
            # 対局開始前：座席インデックスへの依存がまだ無いので、そのまま除名する
            room['players'].remove(player)
            if not room['players']:
                del rooms[room_id]
            else:
                if player['is_host'] and not any(p['is_host'] for p in room['players']):
                    room['players'][0]['is_host'] = True
                send_room_update(room_id)
        else:
            # 対局中：current_turn/dealer_idx が座席インデックス依存のため除名せず、
            # 切断フラグのみ立てて手番が来たら自動打牌に切り替える
            player['disconnected'] = True

            if player['is_host']:
                # ホストが対局中に切断：他の接続中プレイヤーへホスト権限を移す
                # （移らないと対局終了後、誰も「次の対局を始める」を押せなくなる）
                successor = next((p for p in room['players'] if not p.get('disconnected')), None)
                if successor:
                    player['is_host'] = False
                    successor['is_host'] = True
                    socketio.emit('system_msg', {'message': f"👑 ホストが切断したため、{successor['name']} さんがホストを引き継ぎました"}, room=room_id)

            socketio.emit('system_msg', {'message': f"⚠️ {player['name']} さんが切断しました（自動打牌に切り替わります）"}, room=room_id)
            broadcast_state(room_id)
            _auto_discard_if_disconnected(room, room_id)
        break

@socketio.on('start_game')
def handle_start_game(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room or room['status'] not in ['waiting', 'game_over', 'match_over']:
        return

    # 新しい対局（東風戦）を開始する：席をランダム化し、持ち点・親・局数をリセットする
    random.shuffle(room['players'])
    for p in room['players']:
        p['score'] = INITIAL_SCORE

    room['dealer_idx'] = 0
    room['hand_number'] = 1
    room['max_hands'] = len(room['players'])
    room['honba'] = 0
    room['renchan'] = False
    room['riichi_sticks'] = 0  # リーチ棒の供託本数（対局開始時のみリセット。局をまたいで繰り越す）
    _assign_winds(room)
    _deal_new_hand(room)

    emit('system_msg', {'message': f"🀄 東風戦スタート！ 東1局（全{room['max_hands']}局）"}, room=room_id)
    broadcast_state(room_id)
    _auto_discard_if_disconnected(room, room_id)

@socketio.on('reset_game')
def handle_reset_game(data):
    """現在の局を終え、次局へ進める（最終局であれば対局終了・結果表示）"""
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room:
        return
    _advance_to_next_hand(room, room_id)

@socketio.on('discard_tile')
def handle_discard_tile(data):
    room_id = data.get('room_id')
    tile = data.get('tile')
    room = rooms.get(room_id)
    if not room or room['status'] != 'playing':
        return

    current_player = room['players'][room['current_turn']]
    if current_player['id'] != request.sid:
        return

    try:
        tile_to_remove = int(tile) if str(tile).isdigit() else tile
        if tile_to_remove not in current_player['hand']:
            return
    except ValueError:
        return

    drawn_tile = current_player.get('drawn_tile')
    # リーチ中はツモ切り（ツモった牌をそのまま切る）のみ許可する。
    # ただし「リーチ宣言直後、まだ何も切っていない最初の1回」だけは、
    # どの牌を切るか自由に選べる（riichi_tile_index が None＝まだ宣言牌を切っていない状態）
    is_riichi_locked = (
        current_player.get('riichi')
        and current_player.get('riichi_committed')
        and drawn_tile is not None
    )
    if is_riichi_locked and tile_to_remove != drawn_tile:
        return

    _perform_discard(room, room_id, current_player, tile_to_remove)

# ロンは即座に確定させず、3秒のロン受付ウィンドウが閉じるまで宣言を蓄積する（ダブロン・トリプルロン対応）。
# 精算は wait_and_advance のタイマー満了時に _finalize_ron_claims が一括で行う。
@socketio.on('action_ron')
def handle_action_ron(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room or room['status'] != 'waiting_action':
        return

    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not clicker or clicker['id'] == room['last_discard_player']:
        return

    if any(c['id'] == clicker['id'] for c in room.get('ron_claims', [])):
        return  # 二重宣言は無視

    if _is_furiten(clicker):
        # フリテン中のロン宣言はチョンボとして罰符を適用する
        apply_chombo(room, room_id, clicker, reason="フリテン中にロンを宣言した")
        broadcast_state(room_id)
        return

    winning_tile = room['last_discard']
    meld_tiles, kan_count, ankan_tiles, open_meld_tiles = _expand_melds(clicker)
    is_houtei = len(room['deck']) == 0
    is_renhou = (not room.get('any_call_happened')) and len(clicker['kawa']) == 0
    is_agari, yaku, score = evaluate_hand(
        clicker['hand'] + [winning_tile],
        meld_kans=meld_tiles,
        kan_count=kan_count,
        ankan_tiles=ankan_tiles,
        open_meld_tiles=open_meld_tiles,
        dora_indicators=room['dora_indicators'],
        riichi=clicker.get('riichi', False),
        double_riichi=clicker.get('double_riichi', False),
        ippatsu=clicker.get('ippatsu_active', False),
        is_tsumo=False,
        is_houtei=is_houtei,
        is_renhou=is_renhou,
        seat_wind=WIND_TO_TILE.get(clicker.get('wind')),
        round_wind=ROUND_WIND_TILE,
        winning_tile=winning_tile
    )

    if is_agari:
        room.setdefault('ron_claims', []).append({'id': clicker['id'], 'yaku': yaku, 'score': score})
        emit('system_msg', {'message': f"🀄 {clicker['name']} さんがロンを宣言しました！"}, room=room_id)
        broadcast_state(room_id)
    else:
        # 誤ロン（チョンボ）：局は終了させず、罰符のみ適用して続行する
        apply_chombo(room, room_id, clicker, reason="ロンを宣言したが手牌がアガリ形になっていなかった（誤ロン）")
        broadcast_state(room_id)

# 既存の handle_action_tsumo を以下に置き換え
@socketio.on('action_tsumo')
def handle_action_tsumo(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room or room['status'] != 'playing':
        return

    current_player = room['players'][room['current_turn']]
    if current_player['id'] != request.sid:
        return

    if current_player.get('drawn_tile') is None:
        # ツモっていないのにツモ宣言（例：ポン直後は打牌が必要でロンでしか和了できない）：チョンボ
        apply_chombo(room, room_id, current_player, reason="ツモっていないのにツモを宣言した（ポン直後など）")
        broadcast_state(room_id)
        return

    meld_tiles, kan_count, ankan_tiles, open_meld_tiles = _expand_melds(current_player)
    is_dealer = (room['current_turn'] == room.get('dealer_idx', 0))
    is_haitei = len(room['deck']) == 0
    is_tenhou = is_dealer and room.get('first_decision_pending', False) and not room.get('any_call_happened')
    is_chiihou = (not is_dealer) and len(current_player['kawa']) == 0 and not room.get('any_call_happened')
    is_agari, yaku, score = evaluate_hand(
        current_player['hand'],
        meld_kans=meld_tiles,
        kan_count=kan_count,
        ankan_tiles=ankan_tiles,
        open_meld_tiles=open_meld_tiles,
        dora_indicators=room['dora_indicators'],
        riichi=current_player.get('riichi', False),
        double_riichi=current_player.get('double_riichi', False),
        ippatsu=current_player.get('ippatsu_active', False),
        is_tsumo=True,
        is_rinshan=current_player.get('rinshan_chance', False),
        is_haitei=is_haitei,
        is_tenhou=is_tenhou,
        is_chiihou=is_chiihou,
        seat_wind=WIND_TO_TILE.get(current_player.get('wind')),
        round_wind=ROUND_WIND_TILE,
        winning_tile=current_player.get('drawn_tile')
    )

    if is_agari:
        room['status'] = 'game_over'
        room['renchan'] = is_dealer  # 親が自分でツモった＝連荘

        # ツモアガリ：他家全員から点数を均等徴収（簡易的に総スコアを加算）
        other_count = len(room['players']) - 1
        per_player_score = score // max(1, other_count)
        
        for p in room['players']:
            if p['id'] != current_player['id']:
                p['score'] -= per_player_score
        current_player['score'] += score

        sticks = room.get('riichi_sticks', 0)
        if sticks > 0:
            current_player['score'] += sticks * RIICHI_STICK_COST
            room['riichi_sticks'] = 0

        emit('win_result', {
            'winner': current_player['name'],
            'loser': '全員 (ツモ)',
            'yaku': yaku,
            'score_str': format_big_number(score),
            'type': 'ツモ'
        }, room=room_id)
        
        emit('system_msg', {'message': f"🀄 ツモ！ {current_player['name']} さんのツモアガリです！（{format_big_number(score)}点）"}, room=room_id)
    else:
        # 誤ツモ（チョンボ）：局は終了させず、罰符のみ適用して続行する
        apply_chombo(room, room_id, current_player, reason="ツモを宣言したが手牌がアガリ形になっていなかった（誤ツモ）")

    broadcast_state(room_id)

@socketio.on('action_pon')
def handle_action_pon(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room or room['status'] != 'waiting_action':
        return

    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not clicker or clicker['id'] == room['last_discard_player']:
        return

    if room.get('ron_claims'):
        emit('system_msg', {'message': "❌ ロンが宣言されているため鳴けません"}, to=request.sid)
        return

    target_tile = room['last_discard']
    if clicker['hand'].count(target_tile) >= 2:
        room['status'] = 'playing'
        clicker['hand'].remove(target_tile)
        clicker['hand'].remove(target_tile)

        loser = next((p for p in room['players'] if p['id'] == room['last_discard_player']), None)
        _remove_last_kawa_tile(loser, target_tile)
        if loser:
            loser['discards_called'] = True
        direction = _relative_direction(room, clicker, loser) if loser else '不明'

        clicker['melds'].append({'tile': target_tile, 'type': 'pon', 'from': direction})

        room['current_turn'] = room['players'].index(clicker)
        room['last_discard'] = None
        room['any_call_happened'] = True
        for p in room['players']:
            p['ippatsu_active'] = False  # 他家の鳴きが入ったので、全員の一発が消える

        emit('system_msg', {'message': f"📣 ポン！ {clicker['name']} さんが{loser['name'] if loser else ''}から鳴きました"}, room=room_id)
    else:
        # 誤ポン（そもそも鳴ける牌が無いのに宣言した）：チョンボとして罰符を適用する
        apply_chombo(room, room_id, clicker, reason="ポンできる牌がないのにポンを宣言した（誤ポン）")

    broadcast_state(room_id)

@socketio.on('action_kan')
def handle_action_kan(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room:
        return

    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not clicker:
        return

    target_tile = room['last_discard']

    if room['status'] == 'waiting_action' and clicker['id'] != room['last_discard_player']:
        if room.get('ron_claims'):
            emit('system_msg', {'message': "❌ ロンが宣言されているため鳴けません"}, to=request.sid)
            return
        if clicker['hand'].count(target_tile) == 3:
            room['status'] = 'playing'
            for _ in range(3):
                clicker['hand'].remove(target_tile)

            loser = next((p for p in room['players'] if p['id'] == room['last_discard_player']), None)
            _remove_last_kawa_tile(loser, target_tile)
            if loser:
                loser['discards_called'] = True
            direction = _relative_direction(room, clicker, loser) if loser else '不明'

            clicker['melds'].append({'tile': target_tile, 'type': 'kan', 'from': direction})

            room['current_turn'] = room['players'].index(clicker)
            room['any_call_happened'] = True
            for p in room['players']:
                p['ippatsu_active'] = False  # 他家の鳴きが入ったので、全員の一発が消える
            if room['deck']:
                drawn = room['deck'].pop()
                clicker['hand'].append(drawn)
                clicker['hand'].sort()
                clicker['drawn_tile'] = drawn
                clicker['rinshan_chance'] = True  # 嶺上牌をツモった直後（嶺上開花のチャンス）
            if room['deck']:
                # カンドラ：新しいドラ表示牌をめくる
                room['dora_indicators'].append(room['deck'].pop())
            room['last_discard'] = None
            emit('system_msg', {'message': f"🔔 カン！ {clicker['name']} さんが{loser['name'] if loser else ''}から明槓しました"}, room=room_id)
            broadcast_state(room_id)
            return
        else:
            # 誤カン（そもそも明槓できる牌が無いのに宣言した）：チョンボとして罰符を適用する
            apply_chombo(room, room_id, clicker, reason="明槓できる牌がないのにカンを宣言した（誤カン）")
            broadcast_state(room_id)
            return

    if room['status'] == 'playing' and room['players'][room['current_turn']]['id'] == request.sid:
        kan_tile = None
        for t in set(clicker['hand']):
            # 5枚以上同じ牌を持っている場合も暗槓できる（4枚使用し、残りは手牌に残す）
            if clicker['hand'].count(t) >= 4:
                kan_tile = t
                break

        # 加槓：既にポンしている牌と同じ牌を、今の手牌からもう1枚足してカンに昇格させる
        kakan_meld = None
        if kan_tile is None:
            kakan_meld = next(
                (m for m in clicker['melds'] if m['type'] == 'pon' and m['tile'] in clicker['hand']),
                None
            )

        if kan_tile is not None:
            for _ in range(4):
                clicker['hand'].remove(kan_tile)
            clicker['melds'].append({'tile': kan_tile, 'type': 'ankan', 'from': '自分'})

            if room['deck']:
                drawn = room['deck'].pop()
                clicker['hand'].append(drawn)
                clicker['hand'].sort()
                clicker['drawn_tile'] = drawn
                clicker['rinshan_chance'] = True  # 嶺上牌をツモった直後（嶺上開花のチャンス）
            if room['deck']:
                # カンドラ：新しいドラ表示牌をめくる
                room['dora_indicators'].append(room['deck'].pop())
            emit('system_msg', {'message': f"🔔 カン！ {clicker['name']} さんが暗槓しました"}, room=room_id)
            broadcast_state(room_id)
        elif kakan_meld is not None:
            clicker['hand'].remove(kakan_meld['tile'])
            kakan_meld['type'] = 'kan'  # ポン済みの副露をカンへ昇格（明槓と同じ扱い）

            if room['deck']:
                drawn = room['deck'].pop()
                clicker['hand'].append(drawn)
                clicker['hand'].sort()
                clicker['drawn_tile'] = drawn
                clicker['rinshan_chance'] = True  # 嶺上牌をツモった直後（嶺上開花のチャンス）
            if room['deck']:
                # カンドラ：新しいドラ表示牌をめくる
                room['dora_indicators'].append(room['deck'].pop())
            emit('system_msg', {'message': f"🔔 カン！ {clicker['name']} さんが加槓しました"}, room=room_id)
            broadcast_state(room_id)
        else:
            # 誤カン（そもそも暗槓・加槓できる牌が無いのに宣言した）：チョンボとして罰符を適用する
            apply_chombo(room, room_id, clicker, reason="暗槓・加槓できる牌がないのにカンを宣言した（誤カン）")
            broadcast_state(room_id)

@socketio.on('action_riichi')
def handle_action_riichi(data):
    """リーチ宣言。ボタンは常に押せる（聴牌チェックはしない）。
    副露が既にある場合は締め切り（リーチは面前手のみ）のため即チョンボ。
    妥当性（聴牌していたか）はロン/ツモ時の誤和了判定や、流局時の手牌公開で事後的にチョンボとして精算される。"""
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room or room['status'] not in ('playing', 'waiting_action'):
        return

    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not clicker or clicker.get('riichi'):
        return

    if clicker['melds']:
        # 副露があるのにリーチ：即チョンボ
        apply_chombo(room, room_id, clicker, reason="副露（ポン・カン）した手でリーチを宣言した")
        broadcast_state(room_id)
        return

    clicker['riichi'] = True
    clicker['pending_riichi_discard'] = True
    clicker['ippatsu_active'] = True
    # ダブルリーチ：自分がまだ一度も打牌しておらず(=最初の手番)、かつ誰の鳴きも発生していなければ成立
    clicker['double_riichi'] = (len(clicker['kawa']) == 0 and not room.get('any_call_happened'))
    emit('system_msg', {'message': f"❗ {clicker['name']} さんがリーチを宣言しました！"}, room=room_id)
    broadcast_state(room_id)

@socketio.on('action_cancel_riichi')
def handle_action_cancel_riichi(data):
    """リーチ宣言の取り消し。宣言牌（リーチ後最初の打牌）をまだ切っていない間だけ可能。
    riichi_committed は一度打牌すると鳴かれても絶対にリセットされないフラグなので、
    これをもって「宣言牌を切り済みか」を判定する（riichi_tile_index は鳴かれるとNoneに
    戻ることがあり、それだけで判定すると宣言牌が鳴かれた後にリーチを取り消せてしまうバグになる）。"""
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room:
        return

    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not clicker or not clicker.get('riichi') or clicker.get('riichi_committed'):
        return

    clicker['riichi'] = False
    clicker['pending_riichi_discard'] = False
    clicker['ippatsu_active'] = False
    clicker['double_riichi'] = False
    emit('system_msg', {'message': f"↩️ {clicker['name']} さんがリーチを取り消しました"}, room=room_id)
    broadcast_state(room_id)

@socketio.on('action_chombo')
def handle_action_chombo(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room:
        return
    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if clicker:
        apply_chombo(room, room_id, clicker, reason="自己申告チョンボ")
        broadcast_state(room_id)

@socketio.on('action_bug_report')
def handle_action_bug_report(data):
    """バグ報告ボタン。押した時点のプレイヤーの手牌・副露・直近のチョンボ理由をログに記録する。"""
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room:
        return
    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not clicker:
        return

    report = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'room_id': room_id,
        'hand_number': room.get('hand_number'),
        'status': room.get('status'),
        'player_name': clicker['name'],
        'hand': list(clicker['hand']),
        'kawa': list(clicker['kawa']),
        'melds': [dict(m) for m in clicker['melds']],
        'riichi': clicker.get('riichi', False),
        'dora_indicators': list(room.get('dora_indicators', [])),
        'last_chombo_info': clicker.get('last_chombo_info'),
    }
    bug_reports.append(report)
    print(f"🐛 [BUG REPORT] {report}")
    emit('system_msg', {'message': "🐛 バグ報告を受け付けました。ご協力ありがとうございます！"}, to=request.sid)

def send_room_update(room_id):
    room = rooms.get(room_id)
    if not room:
        return
    players_data = [{'name': p['name'], 'is_host': p['is_host'], 'wind': p['wind']} for p in room['players']]
    for p in room['players']:
        socketio.emit('update_room', {
            'player_count': len(room['players']),
            'players': players_data,
            'is_host': p['is_host']
        }, to=p['id'])

def broadcast_state(room_id):
    room = rooms.get(room_id)
    if not room:
        return

    for idx, target_player in enumerate(room['players']):
        others_info = []
        num_players = len(room['players'])
        for offset in range(1, num_players):
            other_idx = (idx + offset) % num_players
            op = room['players'][other_idx]
            others_info.append({
                'name': op['name'],
                'hand_count': len(op['hand']),
                'kawa': op['kawa'],
                'melds': op['melds'],
                'score': op['score'],
                'score_str': format_big_number(op['score']),
                'wind': op['wind'],
                'is_turn': (room['current_turn'] == other_idx) and (room['status'] == 'playing'),
                'is_dealer': (other_idx == room.get('dealer_idx', 0)),
                'disconnected': op.get('disconnected', False),
                'riichi': op.get('riichi', False),
                'riichi_tile_index': op.get('riichi_tile_index')
            })

        is_my_turn = (room['current_turn'] == idx) and (room['status'] == 'playing')

        socketio.emit('state_update', {
            'room_id': room_id,
            'status': room['status'],
            'hand_number': room.get('hand_number', 1),
            'max_hands': room.get('max_hands', len(room['players'])),
            'honba': room.get('honba', 0),
            'riichi_sticks': room.get('riichi_sticks', 0),
            'deck_count': len(room['deck']),
            'dora_indicators': room['dora_indicators'],
            'last_discard': room['last_discard'],
            'winning_tile': room.get('winning_tile'),
            'is_my_turn': is_my_turn,
            'is_waiting_action': room['status'] == 'waiting_action',
            'my_hand': target_player['hand'],
            'my_kawa': target_player['kawa'],
            'my_melds': target_player['melds'],
            'my_score': target_player['score'],
            'my_score_str': format_big_number(target_player['score']),
            'my_wind': target_player['wind'],
            'my_is_dealer': (idx == room.get('dealer_idx', 0)),
            'my_riichi': target_player.get('riichi', False),
            'my_is_host': target_player.get('is_host', False),
            'my_drawn_tile': target_player.get('drawn_tile'),
            'my_riichi_tile_index': target_player.get('riichi_tile_index'),
            'my_riichi_committed': target_player.get('riichi_committed', False),
            'my_furiten': _is_furiten(target_player),
            'others': others_info
        }, to=target_player['id'])

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
