const $ = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem('codepilot-token') || '';
sessionStorage.setItem('codepilot-token', token);
history.replaceState(null, '', '/');
let config, selected = 0, dirty = false, busy = false, shutdown = false;
const keyDrafts = {}, clearedKeys = new Set();
let lastError = '', noticeTimer;
async function api(path, body) {
  const response = await fetch('/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type':'application/json', 'X-Session-Token':token},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error);
  return value;
}
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
  $('notice').hidden = !message;
  if (!error) noticeTimer = setTimeout(() => $('notice').hidden = true, 6000);
}
function changed() {
  dirty = true;
  $('save-state').textContent = '有未保存的修改';
}
function current() { return config.profiles[selected]; }
function acceptConfig(value) {
  if (value.profiles.some(p => typeof p.clipboard_hotkey !== 'string')) {
    throw new Error('后台版本尚未更新，请点击“退出程序”，重新启动后再保存配置。');
  }
  return value;
}
function collect() {
  const p = current();
  for (const [field, id] of Object.entries({hotkey:'hotkey',clipboard_hotkey:'clipboard-hotkey',language:'language',base_url:'base-url',model:'model'})) p[field] = $(id).value.trim();
  keyDrafts[p.id] = $('api-key').value;
  if ($('delete-key').checked) clearedKeys.add(p.id); else clearedKeys.delete(p.id);
  config.pause_hotkey = $('pause-hotkey').value.trim();
  config.stop_hotkey = $('stop-hotkey').value.trim();
}
function renderProfiles() {
  $('profiles').replaceChildren();
  config.profiles.forEach((p, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'profile-button' + (index === selected ? ' active' : '');
    button.setAttribute('aria-current', index === selected ? 'true' : 'false');
    const name = document.createElement('strong'); name.textContent = p.language;
    const hint = document.createElement('span'); hint.textContent = '截图 ' + p.hotkey + (p.clipboard_hotkey ? ' · 剪贴板 ' + p.clipboard_hotkey : '');
    button.append(name, hint);
    button.onclick = () => { collect(); selected = index; render(); };
    $('profiles').append(button);
  });
}
function render() {
  const p = current();
  $('profile-title').textContent = p.language;
  for (const [field, id] of Object.entries({hotkey:'hotkey',clipboard_hotkey:'clipboard-hotkey',language:'language',base_url:'base-url',model:'model'})) $(id).value = p[field];
  $('pause-hotkey').value = config.pause_hotkey;
  $('stop-hotkey').value = config.stop_hotkey;
  $('api-key').value = keyDrafts[p.id] || '';
  $('api-key').type = 'password'; $('show-key').textContent = '显示';
  $('api-key').placeholder = p.has_key ? '已保存；留空保留原密钥' : '填写 API Key';
  $('key-hint').textContent = p.has_key ? '密钥已保存在 Windows 凭据管理器中' : '保存在 Windows 凭据管理器中';
  $('delete-key').checked = clearedKeys.has(p.id);
  $('remove-profile').disabled = config.profiles.length === 1;
  $('models').replaceChildren();
  renderProfiles();
}
document.querySelectorAll('.hotkey').forEach(input => {
  // Let the browser receive shortcuts that are already registered by the app.
  input.addEventListener('focus', () => api('recording',{active:true}).catch(error => notice(error.message,true)));
  input.addEventListener('blur', () => api('recording',{active:false}).catch(error => notice(error.message,true)));
  input.addEventListener('keydown', event => {
    if (event.key === 'Tab') return;
    event.preventDefault();
    if (input.id === 'clipboard-hotkey' && event.key === 'Backspace' && !event.ctrlKey && !event.altKey && !event.shiftKey && !event.metaKey) {
      input.value = ''; changed(); return;
    }
    if (['Control','Alt','Shift','Meta'].includes(event.key)) return;
    const mods = [];
    if (event.ctrlKey) mods.push('Ctrl'); if (event.altKey) mods.push('Alt');
    if (event.shiftKey) mods.push('Shift'); if (event.metaKey) mods.push('Win');
    const aliases = {' ':'Space', 'PageUp':'PageUp', 'PageDown':'PageDown'};
    const physical = /^(Key[A-Z]|Digit[0-9])$/.test(event.code) ? event.code.replace(/^(Key|Digit)/,'') : null;
    const key = physical || aliases[event.key] || (event.key.length === 1 ? event.key.toUpperCase() : event.key);
    input.value = [...mods,key].join('+'); changed();
  });
});
setInterval(() => {
  if (document.hasFocus() && document.activeElement?.classList.contains('hotkey')) {
    api('recording',{active:true}).catch(() => {});
  }
},1500);
$('settings').addEventListener('input', changed);
$('settings').addEventListener('change', changed);
document.querySelectorAll('.global-settings input').forEach(input => input.addEventListener('input', changed));
$('show-key').onclick = () => { const hidden = $('api-key').type === 'password'; $('api-key').type = hidden ? 'text' : 'password'; $('show-key').textContent = hidden ? '隐藏' : '显示'; };
$('clear-clipboard-hotkey').onclick = () => { $('clipboard-hotkey').value = ''; changed(); };
$('add-profile').onclick = () => {
  collect();
  const used = new Set([...config.profiles.flatMap(p => [p.hotkey.toUpperCase(),p.clipboard_hotkey.toUpperCase()]),config.pause_hotkey.toUpperCase(),config.stop_hotkey.toUpperCase()]);
  const candidates = Array.from({length:24},(_,i) => 'F'+(i+1)).filter(key => key !== 'F12');
  const hotkey = candidates.find(key => !used.has(key)) || '';
  used.add(hotkey);
  const clipboard_hotkey = candidates.find(key => !used.has(key)) || '';
  const p = {id:crypto.randomUUID(),hotkey,clipboard_hotkey,language:'C++17',base_url:'https://api.deepseek.com',model:'deepseek-flash',has_key:false};
  config.profiles.push(p); selected = config.profiles.length - 1;
  changed(); render(); $('language').focus(); $('language').select();
};
$('remove-profile').onclick = () => { if (config.profiles.length <= 1) return; collect(); const p = current(); delete keyDrafts[p.id]; clearedKeys.delete(p.id); config.profiles.splice(selected,1); selected = Math.max(0, selected-1); changed(); render(); notice('方案已移除；保存全部配置后生效'); };
$('settings').onsubmit = async event => {
  event.preventDefault(); if (busy) return; collect(); busy = true; $('save').disabled = true; $('save').textContent = '保存中…';
  const keys = {};
  for (const p of config.profiles) {
    if (clearedKeys.has(p.id)) keys[p.id] = '';
    else if (keyDrafts[p.id]) keys[p.id] = keyDrafts[p.id];
  }
  try {
    config = acceptConfig(await api('config', {config,keys}));
    for (const key of Object.keys(keyDrafts)) delete keyDrafts[key]; clearedKeys.clear();
    dirty = false; $('save-state').textContent = '已保存，下一轮生效'; render(); notice('全部配置已保存，快捷键已更新');
  } catch (error) { notice(error.message,true); }
  finally { busy = false; $('save').disabled = false; $('save').textContent = '保存全部配置'; }
};
$('refresh-models').onclick = async () => {
  collect(); const p = structuredClone(current()); const button = $('refresh-models'); button.disabled = true; button.textContent = '读取中…';
  try {
    if (clearedKeys.has(p.id)) throw new Error('此方案已勾选清除密钥，请先填写新密钥');
    const result = await api('models', {profile:p,key:keyDrafts[p.id] || ''});
    if (current().id === p.id) $('models').replaceChildren(...result.models.map(id => { const option=document.createElement('option'); option.value=id; return option; }));
    notice('已读取 ' + result.models.length + ' 个模型，点击模型输入框选择');
  } catch (error) { notice(error.message,true); }
  finally { button.disabled = false; button.textContent = '刷新列表'; }
};
for (const [id,path] of [['pause','pause'],['stop','stop']]) $(id).onclick = async () => { try { await api(path,{}); await poll(); } catch(error) { notice(error.message,true); } };
$('exit').onclick = async () => {
  try { await api('shutdown',{}); shutdown=true; $('status').textContent='程序已退出'; $('status-detail').textContent='再次双击启动文件即可继续使用。'; $('connection').textContent='已退出'; $('pause').disabled=$('stop').disabled=$('exit').disabled=true; }
  catch(error) { notice(error.message,true); }
};
const phases = {idle:'等待获取题目',capture:'正在截图',reading_clipboard:'正在读取剪贴板',ocr:'正在识别文字',generating:'正在生成代码',ready:'可以逐字输入',done:'本轮输入完成',error:'本轮未完成'};
const activePhases = ['capture','reading_clipboard','ocr','generating','ready'];
async function poll() {
  if (shutdown) return;
  try {
    const s = await api('status');
    if (shutdown) return;
    $('status').textContent = (s.paused ? '输入已暂停 · ' : '') + phases[s.phase];
    const hints = {idle:'按截图快捷键开始；也可手动选中题目并右键复制，再按剪贴板快捷键。',capture:'当前画面已进入截图流程。',reading_clipboard:'正在读取已复制的题目文字。',ocr:'正在本机识别题目，首次识别需要加载模型。',generating:'等待完整结果，生成期间可以正常打字。',ready:"切换到编辑器，每按一次 A–Z 或 [];',./ 输出一个字符。",done:'触发键已恢复正常输入，可以截图或重新复制题目开始下一轮。',error:s.error};
    $('status-detail').textContent = s.paused ? '正常字母输入已恢复；再次按暂停快捷键可继续。' : hints[s.phase];
    $('status-dot').className = 'status-dot' + (s.phase === 'error' ? ' error' : activePhases.includes(s.phase) && !s.paused ? ' active' : '');
    $('progress').textContent = s.index + ' / ' + s.total + ' 字符';
    $('pause').textContent = s.paused ? '恢复输入' : '暂停输入';
    $('pause').disabled = !activePhases.includes(s.phase);
    $('stop').disabled = !activePhases.includes(s.phase);
    $('round-label').textContent = s.profile ? s.profile + ' · ' + (s.paused ? '已暂停' : phases[s.phase]) : '等待获取题目';
    $('ocr-count').textContent = s.question.length + ' 字符'; $('code-count').textContent = s.total + ' 字符';
    $('ocr-text').textContent = s.question || '截图识别或读取剪贴板后，题目文本会显示在这里。';
    $('code-text').textContent = s.code || "生成完成后，代码写入剪贴板；在编辑器按 A–Z 或 [];',./ 逐字输出。";
    $('history').replaceChildren(...s.history.map(item => { const li=document.createElement('li'), time=document.createElement('time'), text=document.createElement('span'); time.textContent=item.time; text.textContent=item.message; li.append(time,text); return li; }));
    if (s.error && s.error !== lastError) notice(s.error,true); lastError=s.error;
    $('connection').textContent = '本机已连接';
  } catch(error) { if (shutdown) return; $('connection').textContent = '连接已断开'; $('status').textContent='后台未连接'; $('status-detail').textContent='请重新双击启动文件。'; $('pause').disabled=$('stop').disabled=true; }
}
async function init() {
  try {
    config = acceptConfig(await api('config'));
    render(); await poll(); setInterval(poll,800);
  } catch(error) { notice(error.message,true); $('status').textContent='无法加载配置'; $('status-detail').textContent='请重新双击启动文件打开此页面。'; }
}
init();
