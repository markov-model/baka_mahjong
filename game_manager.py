import random
import string

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = []
        self.host_sid = None
        self.names = {}
        self.deck = []
        self.dora_indicators = []
        self.hands = {}
        self.discards = {}
        self.scores = {}
        self.kans = {}
        self.has_called = {}
        self.last_discard = None
        self.last_discarder = None
        self.current_turn_idx = 0
        self.is_started = False

    def add_player(self, sid, name):
        if len(self.players) >= 4 and sid not in self.players:
            return False, "満員です"
        if self.is_started:
            return False, "既に対局が開始されています"
        if sid not in self.players:
            self.players.append(sid)
            self.names[sid] = name
            if self.host_sid is None:
                self.host_sid = sid
        return True, ""

    def start_game(self):
        # 1~13筒 x 16枚 = 計208枚
        self.deck = [tile for tile in range(1, 14) for _ in range(16)]
        random.shuffle(self.deck)
        
        # ドラ表示牌 2枚
        self.dora_indicators = [self.deck.pop(), self.deck.pop()]
        
        self.discards = {sid: [] for sid in self.players}
        self.hands = {sid: [] for sid in self.players}
        self.scores = {sid: 100000000 for sid in self.players}  # 初期持点: 1億点
        self.kans = {sid: [] for sid in self.players}
        self.has_called = {sid: False for sid in self.players}
        self.last_discard = None
        self.last_discarder = None
        
        # 配牌 13枚
        for _ in range(13):
            for sid in self.players:
                self.hands[sid].append(self.deck.pop())
        
        for sid in self.players:
            self.hands[sid].sort()
            
        self.current_turn_idx = 0
        self.is_started = True

    def apply_chombo(self, sid):
        """チョンボ罰符処理（-3.2億点、他者へ均等分配）"""
        penalty = 320000000
        other_players = [p for p in self.players if p != sid]
        num_others = len(other_players)
        
        self.scores[sid] -= penalty
        
        if num_others > 0:
            share = penalty // num_others
            for other_sid in other_players:
                self.scores[other_sid] += share

    def get_current_player_sid(self):
        if not self.players:
            return None
        return self.players[self.current_turn_idx]


class RoomManager:
    def __init__(self):
        self.rooms = {}

    def create_room(self):
        while True:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
            if code not in self.rooms:
                self.rooms[code] = GameRoom(code)
                return self.rooms[code]

    def get_room(self, room_id):
        return self.rooms.get(room_id)

    def remove_player_from_all(self, sid):
        for room_id, room in list(self.rooms.items()):
            if sid in room.players:
                room.players.remove(sid)
                if sid in room.names: del room.names[sid]
                if sid in room.hands: del room.hands[sid]
                if sid in room.scores: del room.scores[sid]
                if not room.players:
                    del self.rooms[room_id]
                elif room.host_sid == sid:
                    room.host_sid = room.players[0]
                return room_id
        return None