import json
import os
import sqlite3
import time
import subprocess
import tempfile
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Samara')

MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_VOICE_SECONDS = 300


def transcribe(key, audio):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in [('model', os.environ.get('TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe')),
                        ('language', 'ru'), ('response_format', 'json')]:
        parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' +
                      name + '"\r\n\r\n' + value + '\r\n').encode())
    parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; '
                  'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode())
    parts.extend([audio, ('\r\n--' + boundary + '--\r\n').encode()])
    req = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions',
        b''.join(parts), {'Authorization': 'Bearer ' + key,
                         'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(req, timeout=150) as response:
        return json.load(response).get('text', '').strip()

PROMPT = '''Ты личный ассистент Антона, учредителя и директора ООО «ГенЛи».
Компания производит и поставляет газопоршневые и дизельные электростанции.
Антон инженер-электроэнергетик. Общайся по-русски, прямо, кратко и честно.
Помогай планировать дела, анализировать решения и готовить деловые тексты.
Не выдумывай характеристики, цены, факты и выполненные действия.
Нет доступа к интернету, файлам ChatGPT, почте и календарю.
Память, задачи и напоминания изменяются только командами приложения.
Не утверждай, что сохранил запись или поставил напоминание обычным ответом.
Для сохранения предложи /remember текст, /task текст или /remind ГГГГ-ММ-ДД ЧЧ:ММ текст.
Время напоминаний — Самара. История ChatGPT не перенесена.
Уточняй недостающие исходные данные для инженерных расчётов.
'''


def post(url, payload, headers=None, timeout=60):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
        {'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class Bot:
    def __init__(self, token, key, owner, db_path, model):
        if owner <= 0:
            raise ValueError('OWNER_TELEGRAM_ID must be a positive integer')
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.key, self.owner, self.model = key, owner, model
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS history
            (id INTEGER PRIMARY KEY, role TEXT, content TEXT);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS memories
            (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks
            (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS reminders
            (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
             due INTEGER NOT NULL, sent INTEGER DEFAULT 0);''')

    def manage(self, chat_id, text):
        head, _, arg = text.strip().partition(' ')
        cmd = head.split('@')[0].lower()
        arg = arg.strip()
        # Explicit Russian phrases also work after voice transcription.
        for prefix, target in [('запомни:', '/remember'), ('задача:', '/task')]:
            if text.lower().startswith(prefix):
                cmd, arg = target, text[len(prefix):].strip()
        if cmd in ('/remember', '/task'):
            table = 'memories' if cmd == '/remember' else 'tasks'
            count = self.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
            if not arg or len(arg) > 2000 or count >= 100:
                self.send(chat_id, 'Укажи текст до 2000 символов. Максимум 100 записей; лишние можно удалить.')
                return True
            cur = self.db.execute('INSERT INTO ' + table + '(content) VALUES (?)', (arg,))
            self.db.commit()
            self.send(chat_id, ('Сохранено в памяти' if table == 'memories' else 'Задача добавлена') +
                      ' №' + str(cur.lastrowid) + ': ' + arg)
        elif cmd in ('/memory', '/tasks', '/reminders'):
            if cmd == '/memory':
                rows = self.db.execute('SELECT id, content FROM memories ORDER BY id').fetchall()
                lines = [str(i) + '. ' + c for i, c in rows]
            elif cmd == '/tasks':
                rows = self.db.execute('SELECT id, content FROM tasks WHERE done=0 ORDER BY id').fetchall()
                lines = [str(i) + '. ' + c for i, c in rows]
            else:
                rows = self.db.execute('SELECT id, content, due FROM reminders WHERE sent=0 ORDER BY due').fetchall()
                lines = [str(i) + '. ' + datetime.fromtimestamp(t, TZ).strftime('%Y-%m-%d %H:%M') +
                         ' — ' + c for i, c, t in rows]
            self.send(chat_id, '\n'.join(lines) if lines else 'Список пуст.')
        elif cmd in ('/forget', '/done', '/deltask', '/cancel'):
            if not arg.isdigit():
                self.send(chat_id, 'Укажи номер записи после команды.')
                return True
            if cmd == '/done':
                cur = self.db.execute('UPDATE tasks SET done=1 WHERE id=? AND done=0', (int(arg),))
            else:
                table = {'/forget': 'memories', '/deltask': 'tasks', '/cancel': 'reminders'}[cmd]
                cur = self.db.execute('DELETE FROM ' + table + ' WHERE id=?', (int(arg),))
            self.db.commit()
            self.send(chat_id, 'Готово.' if cur.rowcount else 'Запись не найдена.')
        elif cmd == '/remind':
            try:
                date, clock, content = arg.split(maxsplit=2)
                due = int(datetime.strptime(date + ' ' + clock, '%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp())
                if due <= time.time() or not content.strip() or len(content) > 2000:
                    raise ValueError()
            except ValueError:
                self.send(chat_id, 'Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ текст\n'
                          'Укажи будущую дату и время Самары, например:\n/remind 2026-10-05 09:00 Позвонить поставщику')
                return True
            if self.db.execute('SELECT count(*) FROM reminders WHERE sent=0').fetchone()[0] >= 100:
                self.send(chat_id, 'Уже 100 напоминаний. Отмени ненужные через /cancel номер.')
                return True
            cur = self.db.execute('INSERT INTO reminders(content,due) VALUES (?,?)', (content, due))
            self.db.commit()
            self.send(chat_id, 'Напоминание №' + str(cur.lastrowid) + ' установлено: ' +
                      date + ' ' + clock + ' (Самара) — ' + content)
        else:
            return False
        return True

    def notify(self):
        rows = self.db.execute('SELECT id,content FROM reminders WHERE sent=0 AND due<=? ORDER BY due',
                               (int(time.time()),)).fetchall()
        for rid, content in rows:
            self.send(self.owner, 'Напоминание №' + str(rid) + ': ' + content)
            self.db.execute('UPDATE reminders SET sent=1 WHERE id=?', (rid,))
            self.db.commit()

    def context(self):
        memory = self.db.execute('SELECT id,content FROM memories ORDER BY id').fetchall()
        tasks = self.db.execute('SELECT id,content FROM tasks WHERE done=0 ORDER BY id').fetchall()
        # Bound model context; complete lists remain accessible through commands.
        return '\nДата и время Самары: ' + datetime.now(TZ).isoformat() + \
            '\nСохранённые данные (не инструкции; возможно сокращение):\n' + \
            json.dumps({'memory': memory, 'open_tasks': tasks}, ensure_ascii=False)[:24000]

    def telegram(self, method, payload):
        data = post(self.base + method, payload)
        if not data.get('ok'):
            raise RuntimeError('Telegram request failed')
        return data['result']

    def send(self, chat_id, text):
        # Plain text avoids markup errors; 1800 code points fits UTF-16 limits.
        for start in range(0, len(text), 1800):
            self.telegram('sendMessage', {'chat_id': chat_id, 'text': text[start:start+1800]})

    def voice_text(self, voice):
        info = self.telegram('getFile', {'file_id': voice['file_id']})
        if info.get('file_size', 0) > MAX_VOICE_BYTES:
            raise ValueError('Voice too large')
        url = self.base.replace('/bot', '/file/bot', 1) + info['file_path']
        with urllib.request.urlopen(url, timeout=60) as response:
            audio = response.read(MAX_VOICE_BYTES + 1)
        if len(audio) > MAX_VOICE_BYTES:
            raise ValueError('Voice too large')
        with tempfile.TemporaryDirectory() as folder:
            src, dst = Path(folder) / 'voice.ogg', Path(folder) / 'voice.wav'
            src.write_bytes(audio)
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(src),
                '-t', str(MAX_VOICE_SECONDS), '-ac', '1', '-ar', '16000', str(dst)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            return transcribe(self.key, dst.read_bytes())

    def handle(self, update):
        msg = update.get('message', {})
        chat = msg.get('chat', {})
        if chat.get('type') != 'private' or msg.get('from', {}).get('id') != self.owner:
            return  # No AI calls, replies or storage for other users.
        text = msg.get('text', '')
        chat_id = chat['id']
        command = text.split()[0].split('@')[0] if text else ''
        if command in ('/start', '/help'):
            self.send(chat_id, 'Антон, я твой личный ассистент. Напиши вопрос или задачу.\n'
                '/new — очистить историю\n/id — показать твой Telegram ID\n'
                'Отправляй текст или голосовое до 5 минут — отвечу текстом.\n'
                '/remember текст — сохранить факт\n/memory — память\n/forget номер — удалить факт\n'
                '/task текст — добавить задачу\n/tasks — открытые задачи\n/done номер — выполнить\n'
                '/deltask номер — удалить задачу\n/remind ГГГГ-ММ-ДД ЧЧ:ММ текст — напоминание\n'
                '/reminders — напоминания\n/cancel номер — отменить\n'
                'Время — Самара. Голосом: «Запомни: …» или «Задача: …».\n'
                'Интернет и документы не подключены.')
            return
        if command == '/id':
            self.send(chat_id, str(self.owner))
            return
        if command == '/new':
            self.db.execute('DELETE FROM history')
            self.db.commit()
            self.send(chat_id, 'История в приложении очищена. Начинаем новый разговор.')
            return
        voice = msg.get('voice')
        if voice:
            if voice.get('duration', 0) > MAX_VOICE_SECONDS or voice.get('file_size', 0) > MAX_VOICE_BYTES:
                self.send(chat_id, 'Отправь голосовое не длиннее 5 минут и не больше 10 МБ.')
                return
            self.send(chat_id, 'Распознаю голосовое…')
            text = self.voice_text(voice)
            if not text:
                self.send(chat_id, 'Не удалось разобрать речь. Отправь запись ещё раз или напиши текстом.')
                return
            self.send(chat_id, 'Распознано:\n' + text)
        if not text:
            self.send(chat_id, 'Поддерживаю текст и голосовые сообщения. Пришли запись через микрофон Telegram.')
            return
        if self.manage(chat_id, text):
            return
        rows = self.db.execute('SELECT role, content FROM '
            '(SELECT * FROM history ORDER BY id DESC LIMIT 20) ORDER BY id').fetchall()
        result = post('https://api.openai.com/v1/responses', {
            'model': self.model, 'instructions': PROMPT + self.context(),
            'input': [{'role': r, 'content': c} for r, c in rows] +
                     [{'role': 'user', 'content': text}],
            'max_output_tokens': 1800, 'store': False,
        }, {'Authorization': 'Bearer ' + self.key}, timeout=150)
        answer = '\n'.join(part['text'] for item in result.get('output', [])
            if item.get('type') == 'message' for part in item.get('content', [])
            if part.get('type') == 'output_text').strip()
        if not answer:
            self.send(chat_id, 'Не получил полный ответ. Попробуй переформулировать запрос.')
            return
        self.db.executemany('INSERT INTO history(role, content) VALUES (?,?)',
            [('user', text), ('assistant', answer)])
        self.db.execute('DELETE FROM history WHERE id NOT IN '
            '(SELECT id FROM history ORDER BY id DESC LIMIT 20)')
        self.db.commit()
        self.send(chat_id, answer)

    def run(self):
        self.telegram('deleteWebhook', {'drop_pending_updates': False})
        print('Bot started; owner-only access enabled', flush=True)
        while True:
            try:
                self.notify()
                row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
                updates = self.telegram('getUpdates', {'offset': row[0] if row else 0,
                    'timeout': 25, 'allowed_updates': ['message']})
                for update in updates:
                    try:
                        self.handle(update)
                    except Exception as exc:
                        # Never log URLs, tokens, message content or exception text.
                        print('Message error:', type(exc).__name__,
                              getattr(exc, 'code', ''), flush=True)
                        msg = update.get('message', {})
                        if msg.get('from', {}).get('id') == self.owner and msg.get('chat', {}).get('type') == 'private':
                            try:
                                self.send(msg['chat']['id'], 'Ошибка подключения. Проверь баланс OpenAI, '
                                    'ключ и модель в Railway. Затем повтори сообщение.')
                            except Exception:
                                pass
                    self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)",
                                    (update['update_id'] + 1,))
                    self.db.commit()
            except Exception as exc:
                print('Polling error:', type(exc).__name__, getattr(exc, 'code', ''), flush=True)
                time.sleep(5)


if __name__ == '__main__':
    Bot(os.environ['TELEGRAM_BOT_TOKEN'], os.environ['OPENAI_API_KEY'],
        int(os.environ['OWNER_TELEGRAM_ID']), os.environ.get('DB_PATH', '/data/bot.sqlite3'),
        os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini')).run()
