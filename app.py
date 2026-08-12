from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import threading
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

# 得点スケール（実際の麻雀に近いスケール）
INITIAL_SCORE = 100000      # 初期持ち点: 10万点
CHOMBO_PENALTY = 32000      # チョンボ罰符: 役満と同スケール
NAGASHI_MANGAN_SCORE = 8000  # 流しマンガンは満貫相当
NOTEN_PENALTY_TOTAL = 3000   # 流局時のテンパイ料（聴牌者で総取り、ノーテン者で均等負担）

def create_initial_deck():
    # 1〜13筒 x 16枚 = 計208枚
    deck = [tile for tile in KOKUSHI_TILES for _ in range(16)]
    random.shuffle(deck)
    return deck

def apply_chombo(room, room_id, offender):
    """チョンボ罰符処理。offenderから罰符を引き、他家へ均等に分配する。
    socketio.emit を使うため、リクエストコンテキスト外（バックグラウンドスレッド）からも呼び出せる。"""
    other_players = [p for p in room['players'] if p['id'] != offender['id']]
    offender['score'] -= CHOMBO_PENALTY
    if other_players:
        share = CHOMBO_PENALTY // len(other_players)
        for p in other_players:
            p['score'] += share
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
    room['deck'] = create_initial_deck()
    room['dora_indicators'] = [room['deck'].pop()] if room['deck'] else []
    room['last_discard'] = None
    room['last_discard_player'] = None
    room['discard_seq'] = 0
    room['winning_tile'] = None
    room['ron_claims'] = []

    for p in room['players']:
        p['hand'] = [room['deck'].pop() for _ in range(13)]
        p['hand'].sort()
        p['kawa'] = []
        p['melds'] = []
        p['discards_called'] = False  # 流しマンガン判定用：この局で自分の捨て牌が鳴かれたか
        p['riichi'] = False

    room['current_turn'] = room['dealer_idx']
    active_player = room['players'][room['current_turn']]
    if room['deck']:
        active_player['hand'].append(room['deck'].pop())
        active_player['hand'].sort()

def _advance_to_next_hand(room, room_id):
    """現在の局を終える。東風戦（参加人数分の局）を全て消化していれば対局終了、
    そうでなければ親を次の席に送って新しい局を配る。
    スレッド（山切れによる流局）からも呼ばれるため、socketio.emit のみを使う。"""
    room['hand_number'] = room.get('hand_number', 1) + 1
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

def _check_tenpai(player):
    """13枚の手牌（+副露）が聴牌かどうかを、1〜13の牌を1枚ずつ加えてアガリになるか総当たりで判定する"""
    meld_tiles, kan_count = _expand_melds(player)
    for t in KOKUSHI_TILES:
        is_agari, _, _ = evaluate_hand(player['hand'] + [t], meld_kans=meld_tiles, kan_count=kan_count)
        if is_agari:
            return True
    return False

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
            apply_chombo(room, room_id, p)
    return revealed

def _handle_exhaustive_draw(room, room_id):
    """山切れによる流局処理。まずリーチ宣言者の手牌を公開してノーテンなら罰する。
    次に流しマンガン成立者を判定し、いなければ聴牌/ノーテン料を精算する。
    Ron/Tsumoと同様に game_over 状態で一旦停止し、次局への進行はプレイヤーの「次の局へ」操作を待つ。
    スレッド（wait_and_advance）から呼ばれるため socketio.emit のみを使う。"""
    room['status'] = 'game_over'
    room['winning_tile'] = None

    riichi_reveal = _reveal_riichi_hands(room, room_id)

    # 流しマンガン：自分の捨て牌が一度も鳴かれていない（このゲームの牌は全て么九牌相当のため条件はこれのみ）
    nagashi_players = [p for p in room['players'] if p['kawa'] and not p.get('discards_called')]

    if nagashi_players:
        nagashi_ids = {p['id'] for p in nagashi_players}
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

    threading.Thread(target=wait_and_advance, args=(room_id, current_seq), daemon=True).start()

def wait_and_advance(target_room_id, expected_seq):
    time.sleep(3.0)
    target_room = rooms.get(target_room_id)
    if not target_room:
        return

    if target_room['status'] == 'waiting_action' and target_room['discard_seq'] == expected_seq:
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
        next_player['hand'].append(target_room['deck'].pop())
        next_player['hand'].sort()

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
    _perform_discard(room, room_id, current_player, current_player['hand'][0])

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
    """河（捨て牌）の中から鳴かれた牌を1枚（末尾優先）取り除く"""
    if not player:
        return
    for i in range(len(player['kawa']) - 1, -1, -1):
        if player['kawa'][i] == tile:
            player['kawa'].pop(i)
            return

def _expand_melds(player):
    """副露を実枚数分（ポン=3枚, カン/暗槓=4枚）展開し、役判定用のカン数も併せて返す"""
    meld_tiles = []
    kan_count = 0
    for m in player['melds']:
        if m['type'] in ('kan', 'ankan'):
            meld_tiles.extend([m['tile']] * 4)
            kan_count += 1
        else:
            meld_tiles.extend([m['tile']] * 3)
    return meld_tiles, kan_count

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

    winning_tile = room['last_discard']
    meld_tiles, kan_count = _expand_melds(clicker)
    is_agari, yaku, score = evaluate_hand(
        clicker['hand'] + [winning_tile],
        meld_kans=meld_tiles,
        kan_count=kan_count,
        dora_indicators=room['dora_indicators'],
        riichi=clicker.get('riichi', False)
    )

    if is_agari:
        room.setdefault('ron_claims', []).append({'id': clicker['id'], 'yaku': yaku, 'score': score})
        emit('system_msg', {'message': f"🀄 {clicker['name']} さんがロンを宣言しました！"}, room=room_id)
        broadcast_state(room_id)
    else:
        # 誤ロン（チョンボ）：局は終了させず、罰符のみ適用して続行する
        apply_chombo(room, room_id, clicker)
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

    meld_tiles, kan_count = _expand_melds(current_player)
    is_agari, yaku, score = evaluate_hand(
        current_player['hand'],
        meld_kans=meld_tiles,
        kan_count=kan_count,
        dora_indicators=room['dora_indicators'],
        riichi=current_player.get('riichi', False)
    )

    if is_agari:
        room['status'] = 'game_over'

        # ツモアガリ：他家全員から点数を均等徴収（簡易的に総スコアを加算）
        other_count = len(room['players']) - 1
        per_player_score = score // max(1, other_count)
        
        for p in room['players']:
            if p['id'] != current_player['id']:
                p['score'] -= per_player_score
        current_player['score'] += score
        
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
        apply_chombo(room, room_id, current_player)

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
        
        emit('system_msg', {'message': f"📣 ポン！ {clicker['name']} さんが{loser['name'] if loser else ''}から鳴きました"}, room=room_id)
    else:
        emit('system_msg', {'message': f"❌ ポンできません"}, to=request.sid)

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
            if room['deck']:
                clicker['hand'].append(room['deck'].pop())
                clicker['hand'].sort()
            room['last_discard'] = None
            emit('system_msg', {'message': f"🔔 カン！ {clicker['name']} さんが{loser['name'] if loser else ''}から明槓しました"}, room=room_id)
            broadcast_state(room_id)
            return

    if room['status'] == 'playing' and room['players'][room['current_turn']]['id'] == request.sid:
        kan_tile = None
        for t in set(clicker['hand']):
            if clicker['hand'].count(t) == 4:
                kan_tile = t
                break
        
        if kan_tile is not None:
            for _ in range(4):
                clicker['hand'].remove(kan_tile)
            clicker['melds'].append({'tile': kan_tile, 'type': 'ankan', 'from': '自分'})
            
            if room['deck']:
                clicker['hand'].append(room['deck'].pop())
                clicker['hand'].sort()
            emit('system_msg', {'message': f"🔔 カン！ {clicker['name']} さんが暗槓しました"}, room=room_id)
            broadcast_state(room_id)
        else:
            emit('system_msg', {'message': f"❌ カンできる牌がありません"}, to=request.sid)

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
        apply_chombo(room, room_id, clicker)
        broadcast_state(room_id)
        return

    clicker['riichi'] = True
    emit('system_msg', {'message': f"❗ {clicker['name']} さんがリーチを宣言しました！"}, room=room_id)
    broadcast_state(room_id)

@socketio.on('action_chombo')
def handle_action_chombo(data):
    room_id = data.get('room_id')
    room = rooms.get(room_id)
    if not room:
        return
    clicker = next((p for p in room['players'] if p['id'] == request.sid), None)
    if clicker:
        apply_chombo(room, room_id, clicker)
        broadcast_state(room_id)

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
                'riichi': op.get('riichi', False)
            })

        is_my_turn = (room['current_turn'] == idx) and (room['status'] == 'playing')

        socketio.emit('state_update', {
            'room_id': room_id,
            'status': room['status'],
            'hand_number': room.get('hand_number', 1),
            'max_hands': room.get('max_hands', len(room['players'])),
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
            'others': others_info
        }, to=target_player['id'])

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))