const DB_NAME = 'uhh-local-chat';
const DB_VERSION = 1;
const CHAT_STORE = 'chats';
const IMAGE_STORE = 'images';
const TERMS_VERSION = '2026-11-15';
const MODE_PROMPTS = {
  casual: 'Use a natural, friendly, conversational tone. Be clear and helpful without sounding stiff.',
  formal: 'Use a polished, respectful, professional tone. Keep the response well-structured and precise.',
  expert: 'Use careful, in-depth reasoning to produce an accurate, thoughtful response. Explain important assumptions and useful nuance, but do not reveal private chain-of-thought; give a concise rationale instead.'
};

const state = {
  db: null,
  chats: [],
  currentChatId: null,
  mode: 'casual',
  busy: false,
  settings: { name: '', instructions: '' },
  account: null,
  outageVisible: false,
  toastTimer: null
};

const $ = (selector) => document.querySelector(selector);
const welcome = $('#welcome');
const messageList = $('#messageList');
const input = $('#messageInput');
const sendButton = $('#sendButton');

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(CHAT_STORE)) database.createObjectStore(CHAT_STORE, { keyPath: 'id' });
      if (!database.objectStoreNames.contains(IMAGE_STORE)) database.createObjectStore(IMAGE_STORE, { keyPath: 'id' });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Could not open local chat storage.'));
  });
}

function dbRequest(storeName, mode, action) {
  return new Promise((resolve, reject) => {
    if (!state.db) {
      reject(new Error('Local chat storage is unavailable.'));
      return;
    }
    let transaction;
    let request;
    try {
      transaction = state.db.transaction(storeName, mode);
      request = action(transaction.objectStore(storeName));
    } catch (error) {
      reject(error);
      return;
    }
    let result;
    request.onsuccess = () => { result = request.result; };
    request.onerror = () => reject(request.error || new Error('Could not access local chat storage.'));
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = () => reject(transaction.error || new Error('Local chat storage transaction failed.'));
    transaction.onabort = () => reject(transaction.error || new Error('Local chat storage transaction failed.'));
  });
}

function saveChat(chat) {
  const record = {
    id: chat.id,
    title: chat.title,
    updatedAt: chat.updatedAt,
    mode: chat.mode,
    messages: chat.messages.map(({ id, role, content, imageId, error }) => ({ id, role, content, imageId, error }))
  };
  return dbRequest(CHAT_STORE, 'readwrite', (store) => store.put(record));
}

function getImage(id) {
  return dbRequest(IMAGE_STORE, 'readonly', (store) => store.get(id));
}

function getAllChats() {
  return dbRequest(CHAT_STORE, 'readonly', (store) => store.getAll());
}

function removeChatRecord(id) {
  return new Promise((resolve, reject) => {
    const transaction = state.db.transaction([CHAT_STORE, IMAGE_STORE], 'readwrite');
    const chatRequest = transaction.objectStore(CHAT_STORE).get(id);
    chatRequest.onsuccess = () => {
      const chat = chatRequest.result;
      transaction.objectStore(CHAT_STORE).delete(id);
      if (chat) {
        chat.messages.forEach((message) => {
          if (message.imageId) transaction.objectStore(IMAGE_STORE).delete(message.imageId);
        });
      }
    };
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error || new Error('Could not delete this chat.'));
    transaction.onabort = () => reject(transaction.error || new Error('Could not delete this chat.'));
  });
}

function readPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem('uhh-preferences') || '{}');
    state.settings = {
      name: typeof saved.name === 'string' ? saved.name.slice(0, 40) : '',
      instructions: typeof saved.instructions === 'string' ? saved.instructions.slice(0, 800) : ''
    };
  } catch (error) {
    console.error('Could not read personalization settings:', error);
    showToast('Saved personalization could not be read. You can set it again in Personalization.');
  }
}

function readLocalAccount() {
  try {
    const saved = JSON.parse(localStorage.getItem('uhh-local-account') || 'null');
    if (saved && typeof saved.email === 'string' && saved.termsVersion === TERMS_VERSION) {
      state.account = { email: saved.email.slice(0, 254) };
    }
  } catch (error) {
    console.error('Could not read the local profile:', error);
    showToast('The local profile could not be read. Please enter your email again.');
  }
}

function updateGate() {
  $('#welcomeGate').hidden = Boolean(state.account);
}

function continueWithEmail(event) {
  event.preventDefault();
  const emailInput = $('#emailInput');
  const termsCheckbox = $('#termsCheckbox');
  if (!emailInput.reportValidity() || !termsCheckbox.checked) return;

  const email = emailInput.value.trim().toLowerCase();
  try {
    localStorage.setItem('uhh-local-account', JSON.stringify({
      email,
      termsVersion: TERMS_VERSION,
      acceptedAt: new Date().toISOString()
    }));
  } catch (error) {
    console.error('Could not save local profile:', error);
    showToast('Your profile could not be saved in this browser.');
    return;
  }
  state.account = { email };
  updateProfile();
  updateGate();
}

function savePreferences() {
  state.settings = {
    name: $('#nameInput').value.trim().slice(0, 40),
    instructions: $('#instructionsInput').value.trim().slice(0, 800)
  };
  localStorage.setItem('uhh-preferences', JSON.stringify(state.settings));
  localStorage.setItem('uhh-mode', state.mode);
  updateProfile();
  closeSettings();
  showToast('Your preferences are saved in this browser.');
}

function updateProfile() {
  const name = state.settings.name || state.account?.email || 'Just you';
  $('#profileName').textContent = name;
  $('#accountStatus').textContent = state.account ? 'Local email · not verified' : 'Local profile';
  $('#welcomeName').textContent = state.settings.name ? `${state.settings.name}, what’s on your mind?` : 'what’s on your mind?';
  $('#nameInput').value = state.settings.name;
  $('#instructionsInput').value = state.settings.instructions;
}

function setMode(mode) {
  if (!Object.hasOwn(MODE_PROMPTS, mode)) return;
  state.mode = mode;
  document.querySelectorAll('.mode-button').forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  localStorage.setItem('uhh-mode', mode);
  const chat = currentChat();
  if (chat) {
    chat.mode = mode;
    chat.updatedAt = Date.now();
    saveChat(chat).catch((error) => {
      console.error('Could not save chat style:', error);
      showToast('The chat style changed, but could not be saved to this device.');
    });
  }
}

function currentChat() {
  return state.chats.find((chat) => chat.id === state.currentChatId) || null;
}

function createChat() {
  const chat = { id: crypto.randomUUID(), title: 'New chat', updatedAt: Date.now(), mode: state.mode, messages: [] };
  state.chats.unshift(chat);
  state.currentChatId = chat.id;
  saveChat(chat).catch((error) => {
    console.error('Could not save new chat:', error);
    showToast('This chat could not be saved on this device.');
  });
  renderAll();
  input.focus();
  closeSidebar();
}

async function loadChats() {
  const savedChats = await getAllChats();
  savedChats.sort((a, b) => b.updatedAt - a.updatedAt);
  state.chats = await Promise.all(savedChats.map(async (chat) => {
    const messages = await Promise.all(chat.messages.map(async (message) => {
      if (!message.imageId) return message;
      const stored = await getImage(message.imageId);
      return stored ? { ...message, imageUrl: URL.createObjectURL(stored.blob) } : { ...message, imageMissing: true };
    }));
    return { ...chat, messages };
  }));
}

function renderAll() {
  renderChatList();
  renderMessages();
}

function renderChatList() {
  const query = $('#chatSearch').value.trim().toLowerCase();
  const visible = state.chats.filter((chat) => chat.title.toLowerCase().includes(query));
  const list = $('#chatList');
  list.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-history';
    empty.textContent = query ? 'No chats match that search.' : 'Your conversations will show up here once you start one.';
    list.append(empty);
    return;
  }
  visible.forEach((chat) => {
    const item = document.createElement('div');
    item.className = `chat-item${chat.id === state.currentChatId ? ' active' : ''}`;
    item.tabIndex = 0;
    item.setAttribute('role', 'button');
    item.setAttribute('aria-label', chat.title);
    const icon = document.createElement('span');
    icon.className = 'chat-item-icon';
    icon.textContent = '◌';
    const title = document.createElement('span');
    title.className = 'chat-item-title';
    title.textContent = chat.title;
    const remove = document.createElement('button');
    remove.className = 'delete-chat';
    remove.type = 'button';
    remove.setAttribute('aria-label', `Delete ${chat.title}`);
    remove.textContent = '×';
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deleteChat(chat.id);
    });
    item.append(icon, title, remove);
    item.addEventListener('click', () => selectChat(chat.id));
    item.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectChat(chat.id);
      }
    });
    list.append(item);
  });
}

function renderMessages() {
  const chat = currentChat();
  const hasMessages = chat && chat.messages.length > 0;
  welcome.hidden = Boolean(hasMessages);
  messageList.replaceChildren();
  if (!hasMessages) return;

  chat.messages.forEach((message) => {
    const row = document.createElement('article');
    row.className = `message ${message.role === 'user' ? 'user' : ''}${message.error ? ' error' : ''}`;
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = message.role === 'user' ? (state.settings.name?.[0]?.toUpperCase() || 'U') : 'u.';
    const body = document.createElement('div');
    body.className = 'message-body';
    if (message.role !== 'user') {
      const label = document.createElement('div');
      label.className = 'message-label';
      label.textContent = message.error ? '_UHH · ISSUE' : '_UHH';
      body.append(label);
    }
    if (message.pending) {
      const dots = document.createElement('span');
      dots.className = 'typing-dots';
      dots.setAttribute('aria-label', 'Thinking');
      dots.innerHTML = '<i></i><i></i><i></i>';
      body.append(dots);
    } else if (message.content) {
      const content = document.createElement('div');
      content.className = 'message-content';
      content.textContent = message.content;
      body.append(content);
    }
    if (message.imageUrl) {
      const image = document.createElement('img');
      image.className = 'message-image';
      image.src = message.imageUrl;
      image.alt = `Generated image: ${message.content || 'image'}`;
      const download = document.createElement('a');
      download.className = 'download-image';
      download.href = message.imageUrl;
      download.download = 'uhh-generated-image.png';
      download.textContent = '↓  Save image';
      body.append(image, download);
    } else if (message.imageMissing) {
      const note = document.createElement('div');
      note.className = 'message-content';
      note.textContent = 'This image is no longer available on this device.';
      body.append(note);
    }
    row.append(avatar, body);
    messageList.append(row);
  });
}

function selectChat(id) {
  const chat = state.chats.find((item) => item.id === id);
  if (!chat) return;
  state.currentChatId = id;
  setMode(chat.mode || 'casual');
  renderAll();
  closeSidebar();
  scrollToBottom();
}

async function deleteChat(id) {
  const chat = state.chats.find((item) => item.id === id);
  if (!chat) return;
  try {
    await removeChatRecord(id);
    chat.messages.forEach((message) => {
      if (message.imageUrl) URL.revokeObjectURL(message.imageUrl);
    });
    state.chats = state.chats.filter((item) => item.id !== id);
    if (state.currentChatId === id) state.currentChatId = null;
    renderAll();
  } catch (error) {
    console.error('Could not delete chat:', error);
    showToast('Could not delete that chat. Please try again.');
  }
}

function makeSystemInstruction() {
  const name = state.settings.name ? ` The user's preferred name is ${state.settings.name}.` : '';
  const preferences = state.settings.instructions ? `\nThe user shared these response preferences: ${state.settings.instructions}` : '';
  return `You are _UHH, a friendly, capable AI assistant created by Plunger Studios. ${MODE_PROMPTS[state.mode]}${name}${preferences}
Be honest about uncertainty, avoid inventing facts, and ask a brief clarifying question when needed. Follow the AI provider's safety policies. Do not help with harmful requests; when a request is unsafe, briefly explain and offer a safer alternative. Never claim to have generated, seen, or done something you have not.`;
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const result = await response.json().catch(() => ({}));
  if (result.code === 'all_providers_unavailable') showOutage();
  if (!response.ok) throw new Error(result.error || `The request failed (${response.status}).`);
  return result;
}

function ensureCurrentChat(text) {
  let chat = currentChat();
  if (!chat) {
    chat = { id: crypto.randomUUID(), title: text.slice(0, 42).trim() || 'New chat', updatedAt: Date.now(), mode: state.mode, messages: [] };
    state.chats.unshift(chat);
    state.currentChatId = chat.id;
  } else if (chat.messages.length === 0) {
    chat.title = text.slice(0, 42).trim() || 'New chat';
  }
  return chat;
}

async function submitMessage(text) {
  if (state.busy || !text.trim()) return;
  const prompt = text.trim();
  const chat = ensureCurrentChat(prompt);
  const userMessage = { id: crypto.randomUUID(), role: 'user', content: prompt };
  const pendingMessage = { id: crypto.randomUUID(), role: 'model', content: '', pending: true };
  chat.messages.push(userMessage, pendingMessage);
  chat.mode = state.mode;
  chat.updatedAt = Date.now();
  state.busy = true;
  input.value = '';
  resizeInput();
  updateSendButton();
  renderAll();
  scrollToBottom();

  try {
    const messages = chat.messages
      .filter((message) => !message.pending)
      .slice(-20)
      .map((message) => ({
        role: message.role === 'model' ? 'assistant' : 'user',
        content: message.content
      }));
    const result = await postJson('/api/chat', {
      messages,
      mode: state.mode,
      systemInstruction: makeSystemInstruction()
    });
    pendingMessage.content = result.text;
  } catch (error) {
    pendingMessage.pending = false;
    pendingMessage.error = true;
    pendingMessage.content = error.message || 'Something went wrong. Please try again.';
  }

  pendingMessage.pending = false;
  chat.updatedAt = Date.now();
  state.busy = false;
  updateSendButton();
  try {
    await saveChat(chat);
  } catch (error) {
    console.error('Could not save conversation:', error);
    showToast('The reply appeared, but this conversation could not be saved.');
  }
  renderAll();
  scrollToBottom();
}

function resizeInput() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

function updateSendButton() {
  sendButton.disabled = state.busy || !input.value.trim();
  sendButton.setAttribute('aria-label', 'Send message');
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    const conversation = $('#conversation');
    conversation.scrollTop = conversation.scrollHeight;
  });
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('visible');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove('visible'), 3800);
}

async function checkApiStatus() {
  const status = $('#connectionStatus');
  try {
    const response = await fetch('/api/status');
    const result = await response.json();
    if (result.available) {
      status.className = 'connection-status ready';
      status.lastElementChild.textContent = 'AI available';
      $('#outageGate').hidden = true;
      state.outageVisible = false;
    } else if (result.reason === 'backend_not_configured') {
      status.className = 'connection-status offline';
      status.lastElementChild.textContent = 'Backend not connected';
    } else if (!result.configured) {
      status.className = 'connection-status offline';
      status.lastElementChild.textContent = 'AI service not configured';
    } else {
      status.className = 'connection-status offline';
      status.lastElementChild.textContent = result.reason === 'backend_unreachable'
        ? 'AI service starting'
        : 'AI temporarily unavailable';
    }
  } catch (error) {
    console.error('Could not check Gemini connection:', error);
    status.className = 'connection-status offline';
    status.lastElementChild.textContent = 'AI server unavailable';
  }
}

function showOutage() {
  state.outageVisible = true;
  $('#outageGate').hidden = false;
  checkApiStatus();
}

function openSettings() {
  updateProfile();
  $('#settingsPanel').classList.add('open');
  $('#settingsPanel').setAttribute('aria-hidden', 'false');
  $('#backdrop').classList.add('visible');
}

function closeSettings() {
  $('#settingsPanel').classList.remove('open');
  $('#settingsPanel').setAttribute('aria-hidden', 'true');
  $('#backdrop').classList.remove('visible');
}

function closeSidebar() {
  $('#sidebar').classList.remove('open');
  $('#backdrop').classList.remove('visible');
}

function openSidebar() {
  $('#sidebar').classList.add('open');
  $('#backdrop').classList.add('visible');
}

function bindEvents() {
  $('#newChatButton').addEventListener('click', createChat);
  $('#settingsButton').addEventListener('click', openSettings);
  $('#closeSettings').addEventListener('click', closeSettings);
  $('#backdrop').addEventListener('click', () => { closeSettings(); closeSidebar(); });
  $('#saveSettings').addEventListener('click', savePreferences);
  $('#signInForm').addEventListener('submit', continueWithEmail);
  $('#emailInput').addEventListener('input', () => {
    $('#continueButton').disabled = !$('#emailInput').checkValidity() || !$('#termsCheckbox').checked;
  });
  $('#termsCheckbox').addEventListener('change', () => {
    $('#continueButton').disabled = !$('#emailInput').checkValidity() || !$('#termsCheckbox').checked;
  });
  $('#chatSearch').addEventListener('input', renderChatList);
  $('#openSidebar').addEventListener('click', openSidebar);
  $('#closeSidebar').addEventListener('click', closeSidebar);
  document.querySelectorAll('.mode-button').forEach((button) => button.addEventListener('click', () => setMode(button.dataset.mode)));
  document.querySelectorAll('.suggestion').forEach((button) => {
    button.addEventListener('click', () => {
      input.value = button.dataset.prompt || '';
      resizeInput();
      updateSendButton();
      input.focus();
    });
  });
  input.addEventListener('input', () => { resizeInput(); updateSendButton(); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#composerForm').requestSubmit();
    }
  });
  $('#composerForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value;
    if (text.trim()) submitMessage(text);
  });
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      createChat();
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      $('#chatSearch').focus();
    } else if (event.key === 'Escape') {
      closeSettings();
      closeSidebar();
    }
  });
}

async function init() {
  bindEvents();
  readPreferences();
  try {
    state.db = await openDatabase();
    await loadChats();
  } catch (error) {
    console.error('Could not initialize chat storage:', error);
    showToast('Local chat storage is unavailable. Chats may not be saved in this browser.');
  }
  const savedMode = localStorage.getItem('uhh-mode');
  setMode(Object.hasOwn(MODE_PROMPTS, savedMode) ? savedMode : 'casual');
  readLocalAccount();
  updateProfile();
  updateGate();
  renderAll();
  checkApiStatus();
  window.setInterval(() => {
    if (state.outageVisible) checkApiStatus();
  }, 20_000);
}

init();
