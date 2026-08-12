const socket = io();
let currentRoomId = "";
let isHost = false;
let isMyTurn = false;
let actionTimer = null;

// ====================================================
// 🀄 1. 牌の表示マッピング & 変換関数
// ====================================================
const TILE_MAP = {
    1: "1筒", 2: "9筒",
    3: "1索", 4: "9索",
    5: "1萬", 6: "9萬",
    7: "東",  8: "南",  9: "西", 10: "北",
    11: "白", 12: "發", 13: "中"
};

function formatTile(tile) {
    if (tile === null || tile === undefined || tile === '') return '';
    if (typeof tile === 'object' && tile !== null) {
        tile = tile.tile || tile.id || tile.value || tile.name;
    }
    if (typeof tile === 'string' && !/\d/.test(tile)) return tile;
    
    const num = parseInt(String(tile).replace(/[^0-9]/g, ''), 10);
    if (!isNaN(num) && TILE_MAP[num]) {
        return TILE_MAP[num];
    }
    return String(tile);
}

// 鳴き牌（配列/オブジェクト混在）の堅牢な解析関数
function parseTilesArray(data) {
    if (!data) return [];
    if (!Array.isArray(data)) {
        if (typeof data === 'object') {
            if (data.tiles && Array.isArray(data.tiles)) return parseTilesArray(data.tiles);
            return [data.tile || data.id || data.value || data];
        }
        return [data];
    }
    let result = [];
    data.forEach(item => {
        if (Array.isArray(item)) {
            result = result.concat(parseTilesArray(item));
        } else if (typeof item === 'object' && item !== null) {
            if (item.tiles && Array.isArray(item.tiles)) {
                result = result.concat(parseTilesArray(item.tiles));
            } else {
                result.push(item.tile || item.id || item.value || item);
            }
        } else {
            result.push(item);
        }
    });
    return result;
}

// 🔊 効果音再生
function playActionSound() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(587.33, ctx.currentTime);
        osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + 0.12);
        gain.gain.setValueAtTime(0.08, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.12);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.12);
    } catch(e) {}
}

// ====================================================
// 🎨 2. 麻雀卓専用スタイルの動的注入
// ====================================================
function injectMahjongStyles() {
    if (document.getElementById('mahjong-layout-style')) return;
    const style = document.createElement('style');
    style.id = 'mahjong-layout-style';
    style.innerHTML = `
        .mahjong-table-grid {
            display: grid;
            grid-template-columns: 140px 1fr 140px;
            grid-template-rows: auto 1fr auto;
            grid-template-areas:
                ".        top       ."
                "left     center    right"
                "bottom   bottom    bottom";
            gap: 10px;
            max-width: 1050px;
            margin: 0 auto;
            background: radial-gradient(circle, #1e4620 0%, #0f2b11 100%);
            border: 12px solid #3e2723;
            border-radius: 16px;
            padding: 12px;
            box-shadow: inset 0 0 20px rgba(0,0,0,0.8), 0 10px 25px rgba(0,0,0,0.5);
            color: #fff;
            box-sizing: border-box;
        }

        .seat-top    { grid-area: top; text-align: center; }
        .seat-left   { grid-area: left; display: flex; flex-direction: column; justify-content: center; }
        .seat-right  { grid-area: right; display: flex; flex-direction: column; justify-content: center; }
        .seat-center { grid-area: center; background: rgba(0,0,0,0.35); border-radius: 12px; padding: 10px; border: 1px solid rgba(255,255,255,0.1); }
        .seat-bottom { grid-area: bottom; background: rgba(0,0,0,0.4); border-radius: 12px; padding: 10px; border: 1px solid rgba(0,255,204,0.3); }

        .center-kawa-grid {
            display: grid;
            grid-template-columns: 130px 140px 130px;
            grid-template-rows: auto 130px auto;
            grid-template-areas:
                ".        kawa-top    ."
                "kawa-left center-info kawa-right"
                ".        kawa-bottom .";
            gap: 10px;
            align-items: center;
            justify-content: center;
            margin-top: 6px;
        }

        .kawa-box {
            background: rgba(0,0,0,0.25);
            border-radius: 6px;
            padding: 4px;
            width: 126px;
            min-height: 60px;
            display: flex;
            flex-wrap: wrap;
            gap: 1px;
            align-content: flex-start;
            border: 1px dashed rgba(255,255,255,0.15);
            box-sizing: border-box;
        }

        .tile-card {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: #fffdf0;
            color: #111;
            font-weight: bold;
            border-radius: 3px;
            border: 1px solid #ccc;
            box-shadow: 0 1.5px 0 #b3a98b;
            user-select: none;
        }

        .tile-my-hand { width: 40px; height: 56px; font-size: 16px; margin: 2px; cursor: pointer; transition: transform 0.1s; }
        .tile-my-hand:hover { transform: translateY(-4px); background: #ffffff; }
        .tile-dora    { width: 24px; height: 34px; font-size: 11px; margin: 0 1px; }
        .tile-kawa    { width: 19px; height: 27px; font-size: 9px; margin: 0.5px; }
        .tile-meld    { width: 22px; height: 31px; font-size: 10px; background: #e8f4f8; border-color: #00b894; margin: 1px; }
        .tile-hidden  { width: 18px; height: 28px; background: linear-gradient(135deg, #1b4332, #081c15); border: 1px solid #40916c; color: #52b788; font-size: 9px; }

        .tile-v-hidden {
            width: 28px; height: 18px;
            background: linear-gradient(180deg, #1b4332, #081c15);
            border: 1px solid #40916c; border-radius: 3px;
            color: #52b788; font-size: 8px;
            display: flex; align-items: center; justify-content: center;
        }

        .tile-v-meld {
            width: 31px; height: 22px; font-size: 9px;
            background: #e8f4f8; border: 1px solid #00b894; color: #111; font-weight: bold;
            display: flex; align-items: center; justify-content: center; margin: 1px 0;
        }

        .last-discard-highlight {
            border: 2px solid #ff4757 !important;
            box-shadow: 0 0 8px #ff4757 !important;
        }

        .timer-bar-fill {
            height: 100%;
            background: linear-gradient(to right, #00ffcc, #ff4757);
            width: 100%;
            border-radius: 3px;
        }
    `;
    document.head.appendChild(style);
}

// ====================================================
// ⏳ 3. タイマー制御
// ====================================================
function startVisualTimer(seconds) {
    if (actionTimer) clearInterval(actionTimer);
    
    const bar = document.getElementById('action-timer-fill');
    if (!bar) return;

    bar.style.width = '100%';
    const intervalTime = 100;
    const totalSteps = (seconds * 1000) / intervalTime;
    let currentStep = 0;

    actionTimer = setInterval(() => {
        currentStep++;
        let percentage = 100 - (currentStep / totalSteps) * 100;
        bar.style.width = Math.max(0, percentage) + '%';

        if (currentStep >= totalSteps) {
            clearInterval(actionTimer);
        }
    }, intervalTime);
}

// ====================================================
// 🛠️ 4. ヘルパー関数群
// ====================================================
function getElementByCandidates(candidates) {
    for (const id of candidates) {
        const elem = document.getElementById(id);
        if (elem) return elem;
    }
    return null;
}

function getInputValue(candidates) {
    const elem = getElementByCandidates(candidates);
    return elem && elem.value ? elem.value.trim() : "";
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showError(msg) {
    const errorDiv = getElementByCandidates(['entry-error', 'error-msg', 'error-container']);
    if (errorDiv) {
        errorDiv.innerText = msg;
        errorDiv.style.display = 'block';
    } else {
        alert(msg);
    }
}

function clearError() {
    const errorDiv = getElementByCandidates(['entry-error', 'error-msg', 'error-container']);
    if (errorDiv) {
        errorDiv.innerText = '';
        errorDiv.style.display = 'none';
    }
}

window.handleTileClick = function(elem) {
    const rawVal = elem.getAttribute('data-tile-val');
    let tileVal = rawVal;
    if (!isNaN(Number(rawVal)) && rawVal.trim() !== '') {
        tileVal = Number(rawVal);
    }

    if (isMyTurn) {
        playActionSound();
        if (actionTimer) clearInterval(actionTimer);
        socket.emit('discard_tile', { room_id: currentRoomId, tile: tileVal });
    } else {
        alert("自分の手番ではありません！");
    }
};

// ====================================================
// 🎮 5. エントリー操作 & Socket.IO
// ====================================================
function createRoom() {
    clearError();
    const username = getInputValue(['username', 'user-name', 'player-name', 'name']);
    socket.emit('create_room', { username: username });
}

function joinRoom() {
    clearError();
    const username = getInputValue(['username', 'user-name', 'player-name', 'name']);
    const code = getInputValue(['join-code', 'room-code', 'room-id', 'join_code', 'room_code', 'roomCode']).toUpperCase();
    if (!code) {
        showError('4桁のルームコードを入力してください');
        return;
    }
    socket.emit('join_room', { username: username, room_id: code });
}

const joinGame = joinRoom;

function startGame() {
    if (!currentRoomId) return;
    socket.emit('start_game', { room_id: currentRoomId });
}

socket.on('room_joined', (data) => {
    currentRoomId = data.room_id;
    isHost = data.is_host;
    const screenEntry = getElementByCandidates(['screen-entry', 'entry-screen']);
    const screenWaiting = getElementByCandidates(['screen-waiting', 'waiting-screen']);
    const displayRoomCode = getElementByCandidates(['display-room-code', 'room-code-display']);
    if (screenEntry) screenEntry.style.display = 'none';
    if (screenWaiting) screenWaiting.style.display = 'block';
    if (displayRoomCode) displayRoomCode.innerText = currentRoomId;
});

socket.on('update_room', (data) => {
    const playerCountElem = getElementByCandidates(['player-count']);
    if (playerCountElem) playerCountElem.innerText = data.player_count;

    const container = getElementByCandidates(['players-container', 'player-list']);
    if (container) {
        container.innerHTML = '';
        (data.players || []).forEach(p => {
            const item = document.createElement('div');
            item.style.cssText = 'padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.1); display: flex; justify-content: space-between; align-items: center;';
            item.innerHTML = `
                <div>
                    <span style="background:#0984e3; color:white; font-size:12px; padding:2px 6px; border-radius:4px; margin-right:6px; font-weight:bold;">${p.wind || ''}</span>
                    <strong>${escapeHtml(p.name)}</strong>
                </div>
                <div>${p.is_host ? '<span style="background:#ffd700; color:#111; font-size:11px; padding:2px 6px; border-radius:10px; font-weight:bold;">ホスト</span>' : ''}</div>
            `;
            container.appendChild(item);
        });
    }

    const hostControls = getElementByCandidates(['host-controls']);
    if (hostControls) {
        hostControls.style.display = (data.is_host && data.player_count >= 2) ? 'block' : 'none';
    }
});

socket.on('error_msg', (data) => showError(data.message));
socket.on('system_msg', (data) => {
    console.log("💬 [system_msg]:", data.message);
    playActionSound();
});

// ====================================================
// 🀄 6. 対局画面メイン描画
// ====================================================
socket.on('state_update', (state) => {
    injectMahjongStyles();

    const screenWaiting = getElementByCandidates(['screen-waiting', 'waiting-screen']);
    const screenGame = getElementByCandidates(['screen-game', 'game-screen']);

    if (screenWaiting) screenWaiting.style.display = 'none';
    if (screenGame) screenGame.style.display = 'block';

    isMyTurn = Boolean(state.is_my_turn || state.is_turn || state.my_turn);

    const others = state.others || [];
    let playerTop = null, playerLeft = null, playerRight = null;

    if (others.length === 1) {
        playerTop = others[0];
    } else if (others.length === 2) {
        playerRight = others[0];
        playerLeft = others[1];
    } else if (others.length >= 3) {
        playerRight = others[0];
        playerTop = others[1];
        playerLeft = others[2];
    }

    const doraList = parseTilesArray(state.dora_indicators || []);
    const doraHtml = doraList.length > 0 
        ? doraList.map(t => `<div class="tile-card tile-dora">${formatTile(t)}</div>`).join('')
        : '<span style="color:#888;">--</span>';

    const deckCount = state.deck_count !== undefined ? state.deck_count : 0;
    const lastDiscardTile = state.last_discard ? formatTile(state.last_discard) : null;
    const isWaitingAction = Boolean(state.is_waiting_action || state.waiting_action);
    const isGameOver = state.status === 'game_over';
    const isMatchOver = state.status === 'match_over';
    const handNumber = state.hand_number || 1;
    const maxHands = state.max_hands || 1;

    // 対局終了モーダルは新しい局・新しい対局が始まったら自動で閉じる
    if (state.status === 'playing') {
        const mrm = document.getElementById('match-result-modal');
        if (mrm) mrm.style.display = 'none';
    }
    // アガリ演出モーダルも、局が進んだら自動で閉じる（閉じ忘れ対策）
    if (state.status !== 'game_over') {
        const wm = document.getElementById('win-modal');
        if (wm) wm.style.display = 'none';
        const dm = document.getElementById('draw-result-modal');
        if (dm) dm.style.display = 'none';
    }

    screenGame.innerHTML = `
        <div class="mahjong-table-grid">
            
            <!-- 対面 -->
            <div class="seat-top">
                ${renderOpponentCard(playerTop, '対面', false)}
            </div>

            <!-- 上家 -->
            <div class="seat-left">
                ${renderOpponentCard(playerLeft, '上家', true)}
            </div>

            <!-- 中央卓 -->
            <div class="seat-center">
                <div style="display:flex; justify-content:space-between; align-items:center; background:rgba(0,0,0,0.5); padding:6px 12px; border-radius:8px; border:1px solid rgba(255,215,0,0.2); margin-bottom:8px;">
                    <div style="font-size:12px; color:#ccc;">部屋: <strong style="color:#ffd700;">${state.room_id || ''}</strong></div>
                    <div style="font-size:13px; font-weight:bold; color:#ff7675;">東${handNumber}局 <span style="font-size:10px; color:#aaa; font-weight:normal;">(全${maxHands}局)</span></div>
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span style="font-size:12px; font-weight:bold; color:#ffd700;">🀄 ドラ:</span>
                        <div style="display:flex; flex-wrap:wrap; gap:2px;">${doraHtml}</div>
                    </div>
                </div>

                <div style="text-align:center; font-size:13px; font-weight:bold; color:${isGameOver || isMatchOver ? '#ffd700' : (isMyTurn ? '#00ffcc' : '#ffd700')}; margin-bottom:4px;">
                    ${isMatchOver ? '🏁 東風戦 終了' : (isGameOver ? '🀄 局終了 - 結果を確認して次の局へ' : (isWaitingAction ? '⏱️ ロン・鳴き 待機中...' : (isMyTurn ? '★ あなたの手番です' : '他家の思考中...')))}
                </div>

                <div class="center-kawa-grid">
                    <div></div>
                    <div class="kawa-box" style="grid-area: kawa-top;">${renderKawaTiles(playerTop)}</div>
                    <div></div>

                    <div class="kawa-box" style="grid-area: kawa-left;">${renderKawaTiles(playerLeft)}</div>
                    
                    <div style="grid-area: center-info; background:#111; border:2px solid #00b894; border-radius:10px; padding:6px 4px; text-align:center; box-shadow:0 0 12px rgba(0,184,148,0.4); display:flex; flex-direction:column; justify-content:center; align-items:center;">
                        <div style="font-size:9px; color:#aaa; letter-spacing:1px;">山札残り</div>
                        <div style="font-size:22px; font-weight:bold; color:#00ffcc; text-shadow:0 0 8px rgba(0,255,204,0.7); line-height:1;">${deckCount}</div>
                        
                        ${lastDiscardTile ? `
                            <div style="margin-top:4px; font-size:9px; color:#ff4757; font-weight:bold;">ロン/鳴き対象</div>
                            <div class="tile-card tile-dora last-discard-highlight" style="width:20px; height:28px; font-size:10px;">${lastDiscardTile}</div>
                            
                            <div style="width:80%; height:5px; background:#333; border-radius:3px; margin-top:6px; overflow:hidden;">
                                <div id="action-timer-fill" class="timer-bar-fill"></div>
                            </div>
                        ` : ''}
                    </div>

                    <div class="kawa-box" style="grid-area: kawa-right;">${renderKawaTiles(playerRight)}</div>

                    <div></div>
                    <div class="kawa-box" style="grid-area: kawa-bottom;">${renderKawaTilesSelf(state)}</div>
                    <div></div>
                </div>
            </div>

            <!-- 下家 -->
            <div class="seat-right">
                ${renderOpponentCard(playerRight, '下家', true)}
            </div>

            <!-- 自家 -->
            <div class="seat-bottom">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                    <div style="font-size:15px; font-weight:bold; color:#ffd700;">
                        👤 あなた ${state.my_is_dealer ? '<span style="background:#ff7675;color:#111;font-size:10px;padding:1px 6px;border-radius:8px;font-weight:bold;">親</span>' : ''} ${state.my_riichi ? '<span style="background:#d63031;color:#fff;font-size:10px;padding:1px 6px;border-radius:8px;font-weight:bold;">❗リーチ</span>' : ''} (持ち点: <span style="color:#00ffcc;">${state.my_score_str || state.my_score || 0}</span> 点)
                    </div>
                    <div id="my-kans-box" style="display:flex; align-items:center; gap:4px;">
                        <span style="font-size:11px; color:#00b894; font-weight:bold;">副露(ポン/カン):</span>
                        ${renderMeldsTilesSelf(state)}
                    </div>
                </div>

                <div id="my-hand-container" style="display:flex; flex-wrap:wrap; justify-content:center; gap:2px; margin-bottom:8px;">
                    ${renderMyHandTiles(state)}
                </div>

                <div id="action-buttons-container" style="display:flex; justify-content:center; flex-wrap:wrap; gap:6px;">
                    ${isMatchOver ? `
                        <div style="color:#aaa; font-size:13px;">🏁 対局終了 - 結果画面をご確認ください</div>
                    ` : isGameOver ? `
                        <button onclick="playActionSound(); socket.emit('reset_game', {room_id: currentRoomId})" style="background:#00b894; color:#fff; border:none; padding:10px 22px; border-radius:6px; font-weight:bold; font-size:16px; cursor:pointer; box-shadow:0 0 12px rgba(0,184,148,0.6);">🔄 次の局へ</button>
                    ` : `
                        <button onclick="playActionSound(); socket.emit('action_riichi', {room_id: currentRoomId})" ${state.my_riichi ? 'disabled' : ''} style="background:${state.my_riichi ? '#555' : '#d63031'}; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:${state.my_riichi ? 'default' : 'pointer'}; opacity:${state.my_riichi ? '0.6' : '1'};">❗ ${state.my_riichi ? 'リーチ中' : 'リーチ'}</button>
                        <button onclick="playActionSound(); socket.emit('action_pon', {room_id: currentRoomId})" style="background:#0984e3; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">📣 ポン</button>
                        <button onclick="playActionSound(); socket.emit('action_kan', {room_id: currentRoomId})" style="background:#0984e3; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">🔔 カン</button>
                        <button onclick="playActionSound(); socket.emit('action_tsumo', {room_id: currentRoomId})" style="background:#e17055; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">🀄 ツモ！</button>
                        <button onclick="playActionSound(); socket.emit('action_ron', {room_id: currentRoomId})" style="background:#d63031; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">🀄 ロン！</button>
                        <button onclick="playActionSound(); socket.emit('action_chombo', {room_id: currentRoomId})" style="background:#2d3436; color:#aaa; border:none; padding:8px 10px; border-radius:6px; font-size:11px; cursor:pointer;">🚨 チョンボ</button>
                    `}
                </div>
            </div>

        </div>
    `;

    if (lastDiscardTile && isWaitingAction) {
        startVisualTimer(3);
    }
});

// ====================================================
// 🀄 7. サブ描画関数群
// ====================================================
// 副露(ポン・カン)を1グループ単位で描画する。鳴いた牌には方向ラベルを付け、横向きに回す
function meldTileCount(type) {
    if (type === 'pon') return 3;
    if (type === 'kan' || type === 'ankan') return 4;
    return 3;
}

function renderMeldGroup(meld, isVertical) {
    if (!meld) return '';
    const tileVal = (typeof meld === 'object') ? (meld.tile ?? meld.id ?? meld.value) : meld;
    const type = (typeof meld === 'object' && meld.type) ? meld.type : 'pon';
    const fromLabel = (typeof meld === 'object' && meld.from) ? meld.from : '';
    const isAnkan = type === 'ankan';
    const count = meldTileCount(type);

    const tileClass = isVertical ? 'tile-v-meld' : 'tile-meld';
    let tilesHtml = '';
    for (let i = 0; i < count; i++) {
        // 鳴いた元の牌(1枚目)を回転させ、どこから取ったかを分かりやすくする
        const rotateStyle = (!isAnkan && i === 0)
            ? (isVertical ? 'transform:rotate(90deg); margin:2px 0;' : 'transform:rotate(90deg); margin:0 3px;')
            : '';
        tilesHtml += `<div class="${tileClass}" style="${rotateStyle}">${isAnkan && (i === 0 || i === count - 1) ? '🀫' : formatTile(tileVal)}</div>`;
    }

    const badgeText = isAnkan ? '暗槓' : (fromLabel || '');
    const borderColor = isAnkan ? '#636e72' : '#00b894';

    return `
        <div style="position:relative; display:inline-flex; ${isVertical ? 'flex-direction:column;' : 'flex-direction:row;'} align-items:center; gap:1px; border:1px solid ${borderColor}; border-radius:5px; padding:3px 4px; margin:2px;">
            ${badgeText ? `<span style="position:absolute; top:-9px; left:2px; font-size:7px; line-height:1; background:#0b1d13; color:#ffd700; padding:1px 3px; border-radius:3px; border:1px solid ${borderColor}; white-space:nowrap;">${escapeHtml(badgeText)}</span>` : ''}
            ${tilesHtml}
        </div>
    `;
}

function renderOpponentCard(op, positionLabel, isVertical = false) {
    if (!op) return `<div style="opacity:0.3; font-size:12px; text-align:center;">(${positionLabel}なし)</div>`;

    const handCount = op.hand_count || 0;
    const meldGroups = Array.isArray(op.melds) ? op.melds : (Array.isArray(op.kans) ? op.kans : []);

    const hiddenClass = isVertical ? 'tile-v-hidden' : 'tile-hidden';

    let hiddenHandHtml = '';
    for (let i = 0; i < handCount; i++) {
        hiddenHandHtml += `<div class="${hiddenClass}">🀄</div>`;
    }

    let meldsHtml = meldGroups.length > 0
        ? meldGroups.map(m => renderMeldGroup(m, isVertical)).join('')
        : '';

    const containerStyle = isVertical
        ? 'display:flex; flex-direction:column; align-items:center; gap:2px; background:rgba(0,0,0,0.2); padding:4px; border-radius:4px; max-height:280px; overflow-y:auto;'
        : 'display:flex; flex-wrap:wrap; align-items:center; gap:2px; background:rgba(0,0,0,0.2); padding:3px; border-radius:4px;';

    const meldDividerStyle = isVertical
        ? 'border-top:1px dashed #00b894; margin-top:4px; padding-top:4px; display:flex; flex-direction:column; align-items:center; flex-wrap:wrap;'
        : 'border-left:1px dashed #00b894; margin-left:4px; padding-left:4px; display:inline-flex; flex-wrap:wrap; align-items:center;';

    return `
        <div style="background:rgba(0,0,0,0.4); border-radius:8px; padding:6px; border:1px solid ${op.is_turn ? '#00ffcc' : 'rgba(255,255,255,0.1)'}; ${op.is_turn ? 'box-shadow:0 0 10px rgba(0,255,204,0.4);' : ''}">
            <div style="font-size:11px; font-weight:bold; display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
                <span>👤 ${escapeHtml(op.name)} ${op.is_dealer ? '<span style="background:#ff7675;color:#111;font-size:9px;padding:1px 5px;border-radius:8px;font-weight:bold;margin-left:2px;">親</span>' : ''} ${op.riichi ? '<span style="background:#d63031;color:#fff;font-size:9px;padding:1px 5px;border-radius:8px;font-weight:bold;margin-left:2px;">❗リーチ</span>' : ''} ${op.disconnected ? '<span style="background:#636e72;color:#fff;font-size:9px;padding:1px 5px;border-radius:8px;font-weight:bold;margin-left:2px;">⚠️切断中</span>' : ''}</span>
                <span style="color:#ffd700; font-size:10px;">${op.is_turn ? '◀' : ''}</span>
            </div>
            <div style="font-size:11px; color:#00ffcc; margin-bottom:4px;">${op.score_str || op.score || 0}点</div>
            
            <div style="${containerStyle}">
                <div style="display:flex; ${isVertical ? 'flex-direction:column;' : 'flex-wrap:wrap;'} gap:1px;">${hiddenHandHtml}</div>
                ${meldsHtml ? `<div style="${meldDividerStyle}">${meldsHtml}</div>` : ''}
            </div>
        </div>
    `;
}

function renderKawaTiles(op) {
    if (!op) return '';
    const kawaList = parseTilesArray(op.kawa || op.discards || []);
    return kawaList.map(t => `<div class="tile-card tile-kawa">${formatTile(t)}</div>`).join('');
}

function renderKawaTilesSelf(state) {
    const rawKawa = state.my_kawa || state.kawa || state.discards || [];
    const kawaList = parseTilesArray(rawKawa);
    return kawaList.map(t => `<div class="tile-card tile-kawa">${formatTile(t)}</div>`).join('');
}

function renderMeldsTilesSelf(state) {
    const rawMelds = state.my_melds || state.melds || state.my_kans || state.kans || [];
    const meldGroups = Array.isArray(rawMelds) ? rawMelds : [];
    if (meldGroups.length === 0) return '<span style="font-size:11px; color:#777;">なし</span>';
    return meldGroups.map(m => renderMeldGroup(m, false)).join('');
}


function renderMyHandTiles(state) {
    const myHand = parseTilesArray(state.my_hand || state.hand || []);
    const isFrozen = state.status === 'game_over';
    const winningTile = isFrozen ? state.winning_tile : null;
    let winningTileMarked = false;

    return myHand.map(tileNum => {
        let highlightStyle = '';
        if (winningTile !== null && winningTile !== undefined && !winningTileMarked && String(tileNum) === String(winningTile)) {
            winningTileMarked = true;
            highlightStyle = 'border:2px solid #ff4757; box-shadow:0 0 10px #ff4757; transform:translateY(-4px);';
        }
        return `
        <div class="tile-card tile-my-hand" style="${highlightStyle}" data-tile-val="${escapeHtml(String(tileNum))}" onclick="handleTileClick(this)">
            ${formatTile(tileNum)}
        </div>
    `;
    }).join('');
}
// script.js の適当な場所（末尾など）に追加

function closeWinModal() {
    const modal = document.getElementById('win-modal');
    if (modal) modal.style.display = 'none';
}

socket.on('win_result', (data) => {
    if (actionTimer) { clearInterval(actionTimer); actionTimer = null; }
    const modal = document.getElementById('win-modal');
    const details = document.getElementById('win-modal-details');
    const scoreElem = document.getElementById('win-modal-score');
    const titleElem = document.getElementById('win-modal-title');

    const isMulti = Array.isArray(data.winners);

    if (titleElem) {
        titleElem.innerText = `🀄 ${data.type} アガリ達成！`;
    }

    if (details) {
        const winningTileHtml = (data.winning_tile !== undefined && data.winning_tile !== null)
            ? `<div><strong>和了牌:</strong> ${escapeHtml(formatTile(data.winning_tile))}</div>`
            : '';

        if (isMulti) {
            const winnerBlocks = data.winners.map(w => {
                const yakuList = (w.yaku || []).join('<br>・');
                return `
                    <div style="margin-top:10px; padding-top:8px; border-top:1px dashed rgba(255,255,255,0.15);">
                        <div><strong>勝者:</strong> ${escapeHtml(w.winner)} <span style="color:#00ffcc; font-weight:bold;">(+${escapeHtml(w.score_str)}点)</span></div>
                        <div style="margin-top:4px;"><strong>成立役:</strong><br>・${yakuList}</div>
                    </div>
                `;
            }).join('');
            details.innerHTML = `
                <div><strong>放銃者:</strong> ${escapeHtml(data.loser)}</div>
                ${winningTileHtml}
                ${winnerBlocks}
            `;
        } else {
            const yakuList = (data.yaku || []).join('<br>・');
            details.innerHTML = `
                <div><strong>勝者:</strong> ${escapeHtml(data.winner)}</div>
                <div><strong>和了方:</strong> ${escapeHtml(data.type)} (放銃者: ${escapeHtml(data.loser)})</div>
                ${winningTileHtml}
                <div style="margin-top: 8px;"><strong>成立役:</strong><br>・${yakuList}</div>
            `;
        }
    }

    if (scoreElem) {
        scoreElem.innerText = isMulti ? '' : `+ ${data.score_str} 点`;
    }

    if (modal) {
        modal.style.display = 'flex';
    }
});

// ==================================================
// 流局結果モーダル（流しマンガン / 聴牌・ノーテン）
// ==================================================
function showDrawResultModal(data) {
    let modal = document.getElementById('draw-result-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'draw-result-modal';
        modal.className = 'win-modal-overlay';
        document.body.appendChild(modal);
    }

    const isNagashi = data.type === 'nagashi_mangan';
    const players = data.players || [];

    const rowsHtml = players.map(p => {
        const tag = isNagashi
            ? (p.is_nagashi
                ? '<span style="background:#ffd700;color:#111;font-size:11px;padding:2px 6px;border-radius:8px;font-weight:bold;">🌊 流しマンガン</span>'
                : '')
            : (p.is_tenpai
                ? '<span style="background:#00b894;color:#fff;font-size:11px;padding:2px 6px;border-radius:8px;font-weight:bold;">聴牌</span>'
                : '<span style="background:#636e72;color:#fff;font-size:11px;padding:2px 6px;border-radius:8px;font-weight:bold;">ノーテン</span>');
        return `
            <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 10px; margin-bottom:4px; border-radius:6px; background:rgba(255,255,255,0.04);">
                <span>${escapeHtml(p.name)} ${tag}</span>
                <span style="color:#00ffcc; font-weight:bold;">${escapeHtml(p.score_str)} 点</span>
            </div>
        `;
    }).join('');

    const riichiReveal = data.riichi_reveal || [];
    const riichiHtml = riichiReveal.length > 0 ? `
        <div style="text-align:left; margin:15px 0;">
            <div style="font-size:13px; color:#ff7675; font-weight:bold; margin-bottom:6px;">❗ リーチ手牌公開</div>
            ${riichiReveal.map(r => `
                <div style="background:rgba(0,0,0,0.3); border-radius:8px; padding:8px; margin-bottom:6px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <span style="font-weight:bold;">${escapeHtml(r.name)}</span>
                        ${r.is_tenpai
                            ? '<span style="background:#00b894;color:#fff;font-size:11px;padding:2px 6px;border-radius:8px;font-weight:bold;">聴牌（正常）</span>'
                            : '<span style="background:#d63031;color:#fff;font-size:11px;padding:2px 6px;border-radius:8px;font-weight:bold;">🚨 ノーテン・チョンボ</span>'}
                    </div>
                    <div style="display:flex; flex-wrap:wrap; gap:2px;">
                        ${(r.hand || []).map(t => `<div class="tile-card tile-kawa">${formatTile(t)}</div>`).join('')}
                    </div>
                </div>
            `).join('')}
        </div>
    ` : '';

    modal.innerHTML = `
        <div class="win-modal-content">
            <h2 style="color:var(--accent-yellow); margin-top:0;">${isNagashi ? '🌊 流局！流しマンガン成立' : '🌊 流局（山切れ）'}</h2>
            <div style="text-align:left; background:rgba(0,0,0,0.3); padding:12px; border-radius:8px; margin:15px 0;">
                ${rowsHtml}
            </div>
            ${riichiHtml}
            <button onclick="document.getElementById('draw-result-modal').style.display='none';" class="btn-primary" style="padding:10px 24px; font-size:16px;">確認して閉じる</button>
        </div>
    `;
    modal.style.display = 'flex';
}

socket.on('draw_result', (data) => {
    if (actionTimer) { clearInterval(actionTimer); actionTimer = null; }
    playActionSound();
    showDrawResultModal(data);
});

// ==================================================
// 東風戦 終了（最終結果）モーダル
// ==================================================
function showMatchResultModal(data) {
    let modal = document.getElementById('match-result-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'match-result-modal';
        modal.className = 'win-modal-overlay';
        document.body.appendChild(modal);
    }

    const ranking = data.ranking || [];
    const rankingHtml = ranking.map((p, i) => `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 10px; margin-bottom:4px; border-radius:6px; ${i === 0 ? 'background:rgba(255,215,0,0.18); border:1px solid rgba(255,215,0,0.4);' : 'background:rgba(255,255,255,0.04);'}">
            <span style="font-weight:bold;">${i === 0 ? '🏆 ' : ''}${i + 1}位　${escapeHtml(p.name)}</span>
            <span style="color:#00ffcc; font-weight:bold;">${escapeHtml(p.score_str)} 点</span>
        </div>
    `).join('');

    modal.innerHTML = `
        <div class="win-modal-content">
            <h2 style="color:var(--accent-yellow); margin-top:0;">🏁 東風戦 終了！</h2>
            <div style="text-align:left; background:rgba(0,0,0,0.3); padding:12px; border-radius:8px; margin:15px 0;">
                ${rankingHtml}
            </div>
            ${isHost ? `
                <button onclick="document.getElementById('match-result-modal').style.display='none'; socket.emit('start_game', {room_id: currentRoomId})" class="btn-primary" style="padding:10px 24px; font-size:16px;">🀄 新しい対局を始める</button>
            ` : `
                <div style="color:#aaa; font-size:13px;">ホストが新しい対局を開始するのをお待ちください</div>
            `}
        </div>
    `;
    modal.style.display = 'flex';
}

socket.on('match_result', (data) => {
    if (actionTimer) { clearInterval(actionTimer); actionTimer = null; }
    showMatchResultModal(data);
});