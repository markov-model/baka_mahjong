const socket = io();
let currentRoomId = "";
let isHost = false;
let isMyTurn = false;
let actionTimer = null;
let myUsername = "";
let previousGameState = null; // state_update の差分検知（打牌音・ツモ音・手番通知音）用
let gameScreenEverShown = false; // 対局画面が初めて表示されたタイミングでBGMを切り替えるためのフラグ

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

// ====================================================
// 🔊 サウンドマネージャー（BGM / SE）
// ====================================================
// 方針：discard_self/discard_other/draw/click/turn_notify は控えめに、
// ron/tsumo_win/yakuman/chombo は思い切り大仰に鳴らす。この静と動の落差で
// 「数億点が飛び交うゲーム」の馬鹿さを演出する。
// 音声ファイルは後から追加される想定なので、存在しない/読み込みに失敗しても
// 絶対にゲーム進行を止めない（すべて try/catch と .catch(() => {}) で握りつぶす）。
const SOUND_BASE = '/static/sounds/';

const SE_FILES = {
    click:            'se/se_click.mp3',
    discard_self:     'se/se_discard_self.mp3',
    discard_other:    'se/se_discard_other.mp3',
    draw:             'se/se_draw.mp3',
    pon:              'se/se_pon.mp3',
    kan:              'se/se_kan.mp3',
    riichi:           'se/se_riichi.mp3',
    ron:              'se/se_ron.mp3',
    tsumo_win:        'se/se_tsumo_win.mp3',
    yakuman:          'se/se_yakuman.mp3',
    chombo:           'se/se_chombo.mp3',
    draw_exhaustive:  'se/se_draw_exhaustive.mp3',
    turn_notify:      'se/se_turn_notify.mp3',
    timer_tick:       'se/se_timer_tick.mp3'
};

const BGM_FILES = {
    lobby:  'bgm/bgm_lobby.mp3',
    game:   'bgm/bgm_game.mp3',
    reach:  'bgm/bgm_reach.mp3',
    result: 'bgm/bgm_result.mp3'
};

const soundState = {
    muted: false,
    bgmVolume: 0.35,
    seVolume: 0.7
};

(function loadSoundSettings() {
    try {
        const saved = JSON.parse(localStorage.getItem('mahjong_sound_settings') || '{}');
        if (typeof saved.muted === 'boolean') soundState.muted = saved.muted;
        if (typeof saved.bgmVolume === 'number') soundState.bgmVolume = saved.bgmVolume;
        if (typeof saved.seVolume === 'number') soundState.seVolume = saved.seVolume;
    } catch (e) { /* localStorageが使えない環境ではデフォルト値のまま */ }
})();

function saveSoundSettings() {
    try {
        localStorage.setItem('mahjong_sound_settings', JSON.stringify(soundState));
    } catch (e) {}
}

// SEは連打されても重ねて鳴らせるよう、キャッシュしたAudioをcloneNodeして再生する
const seAudioCache = {};
function playSe(name) {
    if (soundState.muted) return;
    const path = SE_FILES[name];
    if (!path) return; // 未知のキーでも絶対にエラーにしない
    try {
        let base = seAudioCache[name];
        if (!base) {
            base = new Audio(SOUND_BASE + path);
            seAudioCache[name] = base;
        }
        const instance = base.cloneNode(true);
        instance.volume = soundState.seVolume;
        instance.play().catch(() => { /* ファイル未配置・自動再生ブロック等は無視 */ });
    } catch (e) { /* Audio生成自体に失敗しても無視 */ }
}

let currentBgmName = null;
let currentBgmAudio = null;

function fadeAudioVolume(audio, from, to, duration, onDone) {
    const steps = 12;
    const stepTime = Math.max(20, duration / steps);
    let i = 0;
    const interval = setInterval(() => {
        i++;
        try {
            audio.volume = Math.max(0, Math.min(1, from + (to - from) * (i / steps)));
        } catch (e) {
            clearInterval(interval);
            return;
        }
        if (i >= steps) {
            clearInterval(interval);
            if (onDone) onDone();
        }
    }, stepTime);
}

// BGM切り替え。fallbackName を渡しておくと、指定したBGMの読み込み/再生に
// 失敗した場合（ファイル未配置など）に自動でそちらへ切り替える。
function playBgm(name, fallbackName) {
    if (currentBgmName === name && currentBgmAudio && !currentBgmAudio.paused) return;
    const path = BGM_FILES[name];
    if (!path) {
        if (fallbackName) playBgm(fallbackName);
        return;
    }

    const prevAudio = currentBgmAudio;
    if (prevAudio) {
        fadeAudioVolume(prevAudio, prevAudio.volume || 0, 0, 500, () => {
            try { prevAudio.pause(); } catch (e) {}
        });
    }

    try {
        const audio = new Audio(SOUND_BASE + path);
        audio.loop = true;
        audio.volume = 0;
        audio.addEventListener('error', () => {
            if (currentBgmAudio === audio && fallbackName) {
                currentBgmAudio = null;
                currentBgmName = null;
                playBgm(fallbackName);
            }
        });

        currentBgmAudio = audio;
        currentBgmName = name;

        if (!soundState.muted) {
            audio.play().catch(() => { /* 自動再生ブロック等は無視。UI操作後に再度呼ばれれば再生される */ });
        }
        fadeAudioVolume(audio, 0, soundState.bgmVolume, 800);
    } catch (e) {
        currentBgmAudio = null;
        currentBgmName = null;
        if (fallbackName) playBgm(fallbackName);
    }
}

function setMuted(muted) {
    soundState.muted = muted;
    saveSoundSettings();
    if (currentBgmAudio) {
        try {
            if (muted) {
                currentBgmAudio.pause();
            } else {
                currentBgmAudio.play().catch(() => {});
            }
        } catch (e) {}
    }
}

function setBgmVolume(vol) {
    soundState.bgmVolume = vol;
    saveSoundSettings();
    if (currentBgmAudio) {
        try { currentBgmAudio.volume = vol; } catch (e) {}
    }
}

function setSeVolume(vol) {
    soundState.seVolume = vol;
    saveSoundSettings();
}

// ====================================================
// 🔈 サウンド設定UI（🔊アイコン → BGM/SE音量・ミュート）
// ====================================================
function initSoundControlUI() {
    if (document.getElementById('sound-control')) return;
    const container = document.createElement('div');
    container.id = 'sound-control';
    container.innerHTML = `
        <button id="sound-toggle-btn" title="音量設定">${soundState.muted ? '🔇' : '🔊'}</button>
        <div id="sound-panel">
            <label><input type="checkbox" id="mute-checkbox" ${soundState.muted ? 'checked' : ''}> ミュート</label>
            <label>BGM音量<input type="range" id="bgm-volume-slider" min="0" max="1" step="0.05" value="${soundState.bgmVolume}"></label>
            <label>SE音量<input type="range" id="se-volume-slider" min="0" max="1" step="0.05" value="${soundState.seVolume}"></label>
        </div>
    `;
    document.body.appendChild(container);

    const toggleBtn = document.getElementById('sound-toggle-btn');
    const panel = document.getElementById('sound-panel');
    toggleBtn.addEventListener('click', () => {
        panel.classList.toggle('sound-panel-open');
    });

    document.getElementById('mute-checkbox').addEventListener('change', (e) => {
        setMuted(e.target.checked);
        toggleBtn.innerText = soundState.muted ? '🔇' : '🔊';
    });
    document.getElementById('bgm-volume-slider').addEventListener('input', (e) => {
        setBgmVolume(parseFloat(e.target.value));
    });
    document.getElementById('se-volume-slider').addEventListener('input', (e) => {
        setSeVolume(parseFloat(e.target.value));
    });
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
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 1px;
            align-content: flex-start;
            justify-items: center;
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
        .tile-meld    { width: 22px; height: 31px; font-size: 10px; background: #e8f4f8; color: #111; border-color: #00b894; margin: 1px; }
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

        .toast-container {
            position: fixed;
            top: 12px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 10000;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
            pointer-events: none;
            width: 90%;
            max-width: 480px;
        }

        .toast-item {
            background: rgba(10, 25, 18, 0.95);
            border: 1px solid rgba(0, 255, 204, 0.35);
            color: #fff;
            font-size: 13px;
            font-weight: bold;
            padding: 8px 16px;
            border-radius: 20px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.5);
            opacity: 0;
            transform: translateY(-8px);
            transition: opacity 0.25s ease, transform 0.25s ease;
            text-align: center;
        }

        .toast-item.toast-show {
            opacity: 1;
            transform: translateY(0);
        }

        #sound-control {
            position: fixed;
            top: 10px;
            right: 10px;
            z-index: 10001;
            font-family: inherit;
        }

        #sound-toggle-btn {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            border: 1px solid rgba(0, 255, 204, 0.4);
            background: rgba(10, 25, 18, 0.9);
            color: #fff;
            font-size: 16px;
            cursor: pointer;
        }

        #sound-panel {
            display: none;
            flex-direction: column;
            gap: 10px;
            background: rgba(10, 25, 18, 0.95);
            border: 1px solid rgba(0, 255, 204, 0.35);
            border-radius: 10px;
            padding: 12px;
            margin-top: 6px;
            width: 170px;
            font-size: 12px;
            color: #fff;
            box-shadow: 0 4px 14px rgba(0,0,0,0.5);
        }

        #sound-panel.sound-panel-open {
            display: flex;
        }

        #sound-panel label {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        #sound-panel input[type="range"] {
            width: 100%;
        }
    `;
    document.head.appendChild(style);
}

// ページ読み込み時点でスタイルと音量設定UIを用意しておく（ロビー画面から使えるように）
injectMahjongStyles();
initSoundControlUI();

// ====================================================
// 🔔 2.5 システムメッセージのトースト表示
// ====================================================
function showToast(message) {
    injectMahjongStyles();
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = 'toast-item';
    toast.innerText = message;
    container.appendChild(toast);

    requestAnimationFrame(() => toast.classList.add('toast-show'));

    setTimeout(() => {
        toast.classList.remove('toast-show');
        setTimeout(() => toast.remove(), 300);
    }, 3500);
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
    const tickAtStep = totalSteps - (1000 / intervalTime); // 残り1秒の時点
    let currentStep = 0;
    let tickPlayed = false;

    actionTimer = setInterval(() => {
        currentStep++;
        let percentage = 100 - (currentStep / totalSteps) * 100;
        bar.style.width = Math.max(0, percentage) + '%';

        if (!tickPlayed && currentStep >= tickAtStep) {
            tickPlayed = true;
            playSe('timer_tick');
        }

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
        playSe('discard_self');
        if (actionTimer) clearInterval(actionTimer);
        socket.emit('discard_tile', { room_id: currentRoomId, tile: tileVal });
    } else {
        alert("自分の手番ではありません！");
    }
};

// ====================================================
// 🔌 4.5 再接続（画面ロック/アプリ切り替え対策）
// ====================================================
// スマホの画面ロックやアプリ切り替えで接続が切れても、同じ部屋・同じ名前で
// 復帰できるように、プレイヤー名と部屋コードを保持しておく。
// ※ localStorage は同一オリジンの全タブ/全ウィンドウで共有されてしまい、
//   「別タブを新しく開いただけで直前の部屋へ勝手に再参加してしまう」事故の元になるため、
//   タブ単位で分離される sessionStorage を使う（ページの再読み込みには問題なく残る）。
function persistSession(username, roomId) {
    try {
        sessionStorage.setItem('mahjong_username', username);
        sessionStorage.setItem('mahjong_room_id', roomId);
    } catch (e) { /* sessionStorageが使えない環境では何もしない */ }
}

function clearSession() {
    try {
        sessionStorage.removeItem('mahjong_username');
        sessionStorage.removeItem('mahjong_room_id');
    } catch (e) {}
}

// Socket.IOが（再読み込みなしでも）接続/再接続した際に呼ばれる。
// 直前に参加していた部屋があれば、そこへの復帰を試みる。
function attemptRejoin() {
    let username = myUsername;
    let roomId = currentRoomId;
    if (!username || !roomId) {
        try {
            username = username || sessionStorage.getItem('mahjong_username');
            roomId = roomId || sessionStorage.getItem('mahjong_room_id');
        } catch (e) {}
    }
    if (username && roomId) {
        myUsername = username;
        socket.emit('join_room', { username: username, room_id: roomId });
    }
}

socket.on('connect', () => {
    attemptRejoin();
});

// ====================================================
// 🎮 5. エントリー操作 & Socket.IO
// ====================================================
// ブラウザの自動再生ポリシー上、ページ読み込み直後はBGMを再生できないため、
// 最初のユーザー操作（部屋作成/参加ボタンのクリック）を検知した時に一度だけロビーBGMを鳴らす
let lobbyBgmUnlocked = false;
function unlockLobbyBgm() {
    if (lobbyBgmUnlocked) return;
    lobbyBgmUnlocked = true;
    playBgm('lobby');
}

function createRoom() {
    clearError();
    unlockLobbyBgm();
    playSe('click');
    const username = getInputValue(['username', 'user-name', 'player-name', 'name']);
    myUsername = username;
    clearSession(); // 新規に部屋を立てる＝古い再接続情報は破棄する
    socket.emit('create_room', { username: username });
}

function joinRoom() {
    clearError();
    unlockLobbyBgm();
    playSe('click');
    const username = getInputValue(['username', 'user-name', 'player-name', 'name']);
    const code = getInputValue(['join-code', 'room-code', 'room-id', 'join_code', 'room_code', 'roomCode']).toUpperCase();
    if (!code) {
        showError('4桁のルームコードを入力してください');
        return;
    }
    myUsername = username;
    socket.emit('join_room', { username: username, room_id: code });
}

const joinGame = joinRoom;

function startGame() {
    if (!currentRoomId) return;
    playSe('click');
    socket.emit('start_game', { room_id: currentRoomId });
}

socket.on('room_joined', (data) => {
    currentRoomId = data.room_id;
    isHost = data.is_host;
    persistSession(myUsername, currentRoomId);
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
    // チョンボは action_chombo 送信時にも鳴らすが、自分が送り主でない場合はこの通知でしか気付けないため、
    // ここでもメッセージ本文から検知して鳴らす（自分が送信者の場合は二重に鳴ることがあるが許容する）
    if (typeof data.message === 'string' && data.message.includes('チョンボ')) {
        playSe('chombo');
    }
    showToast(data.message);
});

// state_update の差分から、鳴らすべきSEを判定して再生する
// （討伐の順番等、サーバー側からは「何が起きたか」が単独のイベントとして届かないため、
//   前回のstateと比較して検知する）
function playDiffSounds(prevState, newState) {
    if (!prevState) return;

    // 他プレイヤーの河が1枚増えた＝そのプレイヤーが打牌した
    const prevOthers = prevState.others || [];
    const newOthers = newState.others || [];
    newOthers.forEach((op, i) => {
        const prevOp = prevOthers[i];
        if (prevOp && prevOp.name === op.name) {
            const prevLen = (prevOp.kawa || []).length;
            const newLen = (op.kawa || []).length;
            if (newLen > prevLen) {
                playSe('discard_other');
            }
        }
    });

    // 自分の手牌が13枚→14枚に増えた＝自分がツモった
    const prevHandLen = (parseTilesArray(prevState.my_hand || [])).length;
    const newHandLen = (parseTilesArray(newState.my_hand || [])).length;
    if (prevHandLen === 13 && newHandLen === 14) {
        playSe('draw');
    }

    // 自分の手番が回ってきた瞬間
    if (!prevState.is_my_turn && newState.is_my_turn) {
        playSe('turn_notify');
    }
}

// リーチ中のプレイヤーが1人でもいるかどうかで、対局中BGMを 'game' <-> 'reach' に切り替える
function updateReachBgm(prevState, newState) {
    const anyRiichi = (s) => Boolean(s.my_riichi) || (s.others || []).some(o => o.riichi);
    const wasReach = prevState ? anyRiichi(prevState) : false;
    const isReach = anyRiichi(newState);
    if (isReach && !wasReach) {
        playBgm('reach', 'game');
    } else if (!isReach && wasReach) {
        playBgm('game', 'lobby');
    }
}

// ====================================================
// 🀄 6. 対局画面メイン描画
// ====================================================
socket.on('state_update', (state) => {
    injectMahjongStyles();

    const prevState = previousGameState;

    const screenWaiting = getElementByCandidates(['screen-waiting', 'waiting-screen']);
    const screenGame = getElementByCandidates(['screen-game', 'game-screen']);

    if (!gameScreenEverShown) {
        gameScreenEverShown = true;
        playBgm('game', 'lobby');
    }
    // 新しい対局が start_game で始まった（東1局に戻った）タイミングでも対局BGMに戻す
    if (state.hand_number === 1 && state.status === 'playing' && (!prevState || prevState.hand_number !== 1)) {
        playBgm('game', 'lobby');
    }
    updateReachBgm(prevState, state);
    playDiffSounds(prevState, state);
    previousGameState = state;

    if (screenWaiting) screenWaiting.style.display = 'none';
    if (screenGame) screenGame.style.display = 'block';

    isMyTurn = Boolean(state.is_my_turn || state.is_turn || state.my_turn);
    if (typeof state.my_is_host === 'boolean') {
        isHost = state.my_is_host; // ホストが対局中に切断した場合など、サーバー側の再割当をここで反映する
    }

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
                        <button onclick="playSe('click'); socket.emit('reset_game', {room_id: currentRoomId})" style="background:#00b894; color:#fff; border:none; padding:10px 22px; border-radius:6px; font-weight:bold; font-size:16px; cursor:pointer; box-shadow:0 0 12px rgba(0,184,148,0.6);">🔄 次の局へ</button>
                    ` : `
                        <button onclick="playSe('riichi'); socket.emit('action_riichi', {room_id: currentRoomId})" ${state.my_riichi ? 'disabled' : ''} style="background:${state.my_riichi ? '#555' : '#d63031'}; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:${state.my_riichi ? 'default' : 'pointer'}; opacity:${state.my_riichi ? '0.6' : '1'};">❗ ${state.my_riichi ? 'リーチ中' : 'リーチ'}</button>
                        ${(state.my_riichi && (state.my_riichi_tile_index === null || state.my_riichi_tile_index === undefined)) ? `
                            <button onclick="playSe('click'); socket.emit('action_cancel_riichi', {room_id: currentRoomId})" style="background:#636e72; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">↩️ リーチ取消</button>
                        ` : ''}
                        <button onclick="playSe('pon'); socket.emit('action_pon', {room_id: currentRoomId})" style="background:#0984e3; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">📣 ポン</button>
                        <button onclick="playSe('kan'); socket.emit('action_kan', {room_id: currentRoomId})" style="background:#0984e3; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">🔔 カン</button>
                        <button onclick="socket.emit('action_tsumo', {room_id: currentRoomId})" style="background:#e17055; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">🀄 ツモ！</button>
                        <button onclick="socket.emit('action_ron', {room_id: currentRoomId})" style="background:#d63031; color:#fff; border:none; padding:8px 14px; border-radius:6px; font-weight:bold; cursor:pointer;">🀄 ロン！</button>
                        <button onclick="playSe('chombo'); socket.emit('action_chombo', {room_id: currentRoomId})" style="background:#2d3436; color:#aaa; border:none; padding:8px 10px; border-radius:6px; font-size:11px; cursor:pointer;">🚨 チョンボ</button>
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
        <div style="position:relative; z-index:1; display:inline-flex; ${isVertical ? 'flex-direction:column;' : 'flex-direction:row;'} align-items:center; gap:1px; border:1px solid ${borderColor}; border-radius:5px; padding:3px 4px; margin:2px;">
            ${badgeText ? `<span style="position:absolute; z-index:2; top:-9px; left:2px; font-size:7px; line-height:1; background:#0b1d13; color:#ffd700; padding:1px 3px; border-radius:3px; border:1px solid ${borderColor}; white-space:nowrap;">${escapeHtml(badgeText)}</span>` : ''}
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

    // 対面(横並び)は折り返すと縦に伸びて画面(特にスマホ横向きの低い高さ)を圧迫するため、
    // 折り返さず横スクロールにする（上家/下家の縦並びは元々 max-height+overflow-y で高さを制限済み）
    const containerStyle = isVertical
        ? 'display:flex; flex-direction:column; align-items:center; gap:2px; background:rgba(0,0,0,0.2); padding:4px; border-radius:4px; max-height:280px; overflow-y:auto;'
        : 'display:flex; flex-wrap:nowrap; overflow-x:auto; max-width:100%; align-items:center; gap:2px; background:rgba(0,0,0,0.2); padding:3px; border-radius:4px;';

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
                <div style="display:flex; ${isVertical ? 'flex-direction:column;' : 'flex-wrap:nowrap;'} gap:1px;">${hiddenHandHtml}</div>
                ${meldsHtml ? `<div style="${meldDividerStyle}">${meldsHtml}</div>` : ''}
            </div>
        </div>
    `;
}

// リーチ宣言牌（riichiTileIndex番目）だけ横向きに回して分かりやすくする
function renderKawaTileList(kawaList, riichiTileIndex) {
    return kawaList.map((t, i) => {
        const isRiichiTile = (riichiTileIndex !== null && riichiTileIndex !== undefined && i === riichiTileIndex);
        const style = isRiichiTile ? 'transform:rotate(90deg); margin:0 4px;' : '';
        return `<div class="tile-card tile-kawa" style="${style}">${formatTile(t)}</div>`;
    }).join('');
}

function renderKawaTiles(op) {
    if (!op) return '';
    const kawaList = parseTilesArray(op.kawa || op.discards || []);
    return renderKawaTileList(kawaList, op.riichi_tile_index);
}

function renderKawaTilesSelf(state) {
    const rawKawa = state.my_kawa || state.kawa || state.discards || [];
    const kawaList = parseTilesArray(rawKawa);
    return renderKawaTileList(kawaList, state.my_riichi_tile_index);
}

function renderMeldsTilesSelf(state) {
    const rawMelds = state.my_melds || state.melds || state.my_kans || state.kans || [];
    const meldGroups = Array.isArray(rawMelds) ? rawMelds : [];
    if (meldGroups.length === 0) return '<span style="font-size:11px; color:#777;">なし</span>';
    return meldGroups.map(m => renderMeldGroup(m, false)).join('');
}


function renderMyHandTiles(state) {
    const myHand = parseTilesArray(state.my_hand || state.hand || []).slice();
    const isFrozen = state.status === 'game_over';
    const winningTile = isFrozen ? state.winning_tile : null;
    let winningTileMarked = false;

    // ツモった牌は手牌の並び（ソート済み）から抜き出し、右側に離して表示する
    const drawnTile = (!isFrozen && state.my_drawn_tile !== undefined && state.my_drawn_tile !== null)
        ? state.my_drawn_tile
        : null;
    let drawnIdx = -1;
    if (drawnTile !== null) {
        drawnIdx = myHand.findIndex(t => String(t) === String(drawnTile));
    }
    let drawnTileRemoved = null;
    if (drawnIdx !== -1) {
        drawnTileRemoved = myHand.splice(drawnIdx, 1)[0];
    }

    // リーチ中はツモ切り（ツモった牌をそのまま切る）以外選べないため、他の牌はクリック不可にする。
    // ただし「リーチ宣言直後、まだ宣言牌を切っていない最初の1回」だけは自由に選べる
    // （my_riichi_tile_index が null/undefined＝まだ宣言牌を切っていない状態）
    const hasDeclaredButNotDiscarded = state.my_riichi_tile_index === null || state.my_riichi_tile_index === undefined;
    const isRiichiLocked = Boolean(state.my_riichi) && !hasDeclaredButNotDiscarded && drawnTileRemoved !== null;

    const renderTile = (tileNum, { isDrawn = false, extraStyle = '' } = {}) => {
        let highlightStyle = extraStyle;
        if (winningTile !== null && winningTile !== undefined && !winningTileMarked && String(tileNum) === String(winningTile)) {
            winningTileMarked = true;
            highlightStyle += 'border:2px solid #ff4757; box-shadow:0 0 10px #ff4757; transform:translateY(-4px);';
        }
        const locked = isRiichiLocked && !isDrawn;
        if (locked) {
            highlightStyle += 'opacity:0.35; cursor:not-allowed;';
        }
        const clickHandler = locked ? '' : 'onclick="handleTileClick(this)"';
        return `
        <div class="tile-card tile-my-hand" style="${highlightStyle}" data-tile-val="${escapeHtml(String(tileNum))}" ${clickHandler}>
            ${formatTile(tileNum)}
        </div>
    `;
    };

    let html = myHand.map(t => renderTile(t)).join('');
    if (drawnTileRemoved !== null) {
        // 少し間を空けて分離し、ツモ牌だと分かるようにする
        html += `<div style="width:8px;"></div>` + renderTile(drawnTileRemoved, { isDrawn: true, extraStyle: 'border-color:#00ffcc;' });
    }
    return html;
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

    // 静と動の落差演出：通常のロン/ツモは大仰に、役満はさらに派手な専用SEで
    const allYaku = isMulti ? data.winners.flatMap(w => w.yaku || []) : (data.yaku || []);
    const hasYakuman = allYaku.some(y => typeof y === 'string' && y.includes('役満'));
    if (hasYakuman) {
        playSe('yakuman');
    } else if (data.type === 'ツモ') {
        playSe('tsumo_win');
    } else {
        playSe('ron'); // ロン・ダブロン・トリプルロン共通
    }

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
    playSe('draw_exhaustive');
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
    playBgm('result', 'game'); // bgm_result.mp3が無ければ playBgm 側のフォールバックで対局BGMのまま
    showMatchResultModal(data);
});