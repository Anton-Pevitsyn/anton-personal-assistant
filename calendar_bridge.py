"""Owner-only Calendar bridge. Google credentials never enter model context or logs."""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet, InvalidToken
from contextlib import contextmanager

TZ = ZoneInfo('Europe/Samara')
SCOPES = ['openid', 'email', 'https://www.googleapis.com/auth/calendar.events.owned',
          'https://www.googleapis.com/auth/calendar.freebusy']


class CalendarError(ValueError):
    pass


def request_json(url, method='GET', payload=None, bearer=None, form=False):
    headers = {}
    data = None
    if bearer:
        headers['Authorization'] = 'Bearer ' + bearer
    if payload is not None:
        data = (urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode())
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=40) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


class GoogleCalendar:
    def __init__(self, db_path):
        self.db_path = db_path
        self.client_id = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
        self.client_secret = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()
        self.redirect_uri = os.environ.get('GOOGLE_REDIRECT_URI', '').strip()
        self.expected_email = os.environ.get('GOOGLE_ACCOUNT_EMAIL', '').strip().casefold()
        self.key = os.environ.get('CALENDAR_TOKEN_KEY', '').strip()
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS google_tokens
                (id INTEGER PRIMARY KEY CHECK(id=1), encrypted BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS google_oauth_state
                (digest TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS google_operations
                (operation_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, body TEXT NOT NULL,
                 email TEXT NOT NULL, allow_conflict INTEGER NOT NULL DEFAULT 0);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def check_config(self):
        if not all([self.client_id, self.redirect_uri, self.expected_email, self.key]):
            raise CalendarError('Для Google Calendar ещё не заполнены настройки Railway. См. инструкцию подключения.')
        parsed = urllib.parse.urlparse(self.redirect_uri)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.path != '/oauth/google/callback' or parsed.query or parsed.fragment:
            raise CalendarError('GOOGLE_REDIRECT_URI должен быть HTTPS-адресом с путём /oauth/google/callback.')
        try:
            return Fernet(self.key.encode())
        except (ValueError, TypeError):
            raise CalendarError('Проверь CALENDAR_TOKEN_KEY в Railway: нужен ключ из команды генерации в инструкции.') from None

    def load_tokens(self):
        cipher = self.check_config()
        with self.db() as db:
            row = db.execute('SELECT encrypted FROM google_tokens WHERE id=1').fetchone()
        if not row:
            raise CalendarError('Google Calendar не подключён. Отправь /calendar_connect.')
        try:
            token = json.loads(cipher.decrypt(row[0]))
        except (InvalidToken, ValueError, TypeError):
            raise CalendarError('Не удалось прочитать доступ Google. Верни прежний CALENDAR_TOKEN_KEY или подключи календарь заново.') from None
        if token.get('email', '').casefold() != self.expected_email:
            raise CalendarError('Подключён другой Google-аккаунт. Отправь /calendar_connect для аккаунта из настроек.')
        return token

    def save_tokens(self, token):
        cipher = self.check_config()
        encrypted = cipher.encrypt(json.dumps(token).encode())
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO google_tokens VALUES (1,?)', (encrypted,))

    def connect_url(self):
        self.check_config()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        digest = hashlib.sha256(state.encode()).hexdigest()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        with self.lock, self.db() as db:
            db.execute('DELETE FROM google_oauth_state')
            db.execute('INSERT INTO google_oauth_state VALUES (?,?,?)', (digest, verifier, int(time.time()) + 600))
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code',
            'scope': ' '.join(SCOPES), 'state': state, 'access_type': 'offline',
            'prompt': 'consent select_account', 'login_hint': self.expected_email,
            'code_challenge': challenge, 'code_challenge_method': 'S256'})

    def callback(self, query):
        # A disconnect/reconnect cannot race a code exchange and resurrect access.
        with self.lock:
            return self._callback(query)

    def _callback(self, query):
        self.check_config()
        state = query.get('state', [''])[0]
        if not state or len(state) > 256:
            raise CalendarError('Ссылка авторизации недействительна. Запроси /calendar_connect заново.')
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.lock, self.db() as db:
            row = db.execute('SELECT verifier,expires FROM google_oauth_state WHERE digest=?', (digest,)).fetchone()
            if not row or row[1] < time.time():
                raise CalendarError('Ссылка истекла или уже использована. Запроси /calendar_connect заново.')
            db.execute('DELETE FROM google_oauth_state WHERE digest=?', (digest,))
        if query.get('error') or not query.get('code', [''])[0]:
            raise CalendarError('Разрешение Google не предоставлено. Можно повторить /calendar_connect.')
        try:
            token = request_json('https://oauth2.googleapis.com/token', 'POST', {
                'client_id': self.client_id, **({'client_secret': self.client_secret} if self.client_secret else {}),
                'redirect_uri': self.redirect_uri, 'code': query['code'][0],
                'code_verifier': row[0], 'grant_type': 'authorization_code'}, form=True)
            user = request_json('https://openidconnect.googleapis.com/v1/userinfo', bearer=token['access_token'])
            if user.get('email_verified') is not True or user.get('email', '').casefold() != self.expected_email:
                raise CalendarError('Выбран другой Google-аккаунт. Войди в аккаунт, указанный в GOOGLE_ACCOUNT_EMAIL.')
            granted = set(token.get('scope', '').split())
            if not set(SCOPES[2:]).issubset(granted):
                raise CalendarError('Не все разрешения календаря предоставлены. Повтори вход и отметь оба разрешения Calendar.')
            if not token.get('refresh_token'):
                raise CalendarError('Google не выдал постоянный доступ. Повтори /calendar_connect и согласие.')
            token['expires_at'] = time.time() + int(token.get('expires_in', 3600)) - 60
            token['email'] = user['email']
            self.save_tokens(token)
            return 'Google Calendar подключён. Вернись в Telegram и отправь /calendar_status, затем «Какие встречи у меня сегодня?».'
        except CalendarError:
            raise
        except Exception:
            raise CalendarError('Не удалось завершить подключение Google. Проверь настройки и повтори /calendar_connect.') from None

    def access_token(self):
        with self.lock:
            token = self.load_tokens()
            if token.get('expires_at', 0) <= time.time():
                try:
                    fresh = request_json('https://oauth2.googleapis.com/token', 'POST', {
                        'client_id': self.client_id, **({'client_secret': self.client_secret} if self.client_secret else {}),
                        'refresh_token': token['refresh_token'], 'grant_type': 'refresh_token'}, form=True)
                    token.update(fresh)
                    token['expires_at'] = time.time() + int(fresh.get('expires_in', 3600)) - 60
                    self.save_tokens(token)
                except Exception:
                    raise CalendarError('Google не обновил доступ. Попробуй позже или переподключи /calendar_connect. В режиме Testing доступ обычно истекает через 7 дней.') from None
            return token['access_token']

    def status(self):
        try:
            token = self.load_tokens()
            self.api('GET', 'calendars/primary/events', params={'maxResults': 1})
            return 'Google Calendar подключён: ' + token['email'] + '\nОсновной календарь. Время ответов — Самара.'
        except CalendarError as exc:
            return str(exc)

    def disconnect(self):
        # Remove local credentials even when Google cannot be reached.
        try:
            token = self.load_tokens().get('refresh_token')
        except CalendarError:
            token = None
        with self.lock, self.db() as db:
            db.execute('DELETE FROM google_tokens')
            db.execute('DELETE FROM google_oauth_state')
        revoked = False
        if token:
            try:
                request_json('https://oauth2.googleapis.com/revoke', 'POST', {'token': token}, form=True)
                revoked = True
            except Exception:
                pass
        return 'Доступ Google удалён из бота. ' + ('Разрешение Google отозвано.' if revoked else
            'При необходимости отзови разрешение также в настройках Google-аккаунта.')

    def api(self, method, path, payload=None, params=None):
        url = 'https://www.googleapis.com/calendar/v3/' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        try:
            return request_json(url, method, payload, self.access_token())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise CalendarError('Google отклонил доступ к календарю. Проверь Calendar API и разрешения, затем /calendar_connect.') from None
            raise CalendarError('Google Calendar временно недоступен (HTTP ' + str(exc.code) + '). Повтори позже.') from None
        except CalendarError:
            raise
        except Exception:
            raise CalendarError('Не удалось связаться с Google Calendar. Повтори позже.') from None

    def interval(self, start, end):
        try:
            a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if a.tzinfo is None or b.tzinfo is None or a.utcoffset().total_seconds() != 14400 or b.utcoffset().total_seconds() != 14400:
                raise ValueError()
            if b <= a or b - a > timedelta(days=31):
                raise ValueError()
            return a, b
        except (ValueError, TypeError, OverflowError):
            raise CalendarError('Уточни начало и конец периода по Самаре. Максимальный период — 31 день.') from None

    def events(self, start, end):
        a, b = self.interval(start, end)
        params = {'timeMin': a.isoformat(), 'timeMax': b.isoformat(), 'singleEvents': 'true',
                  'orderBy': 'startTime', 'maxResults': 100, 'timeZone': 'Europe/Samara'}
        result = self.api('GET', 'calendars/primary/events', params=params)
        # Do not claim a complete list if a page is omitted.
        lines = []
        for item in result.get('items', []):
            if item.get('status') == 'cancelled':
                continue
            start_item, end_item = item.get('start', {}), item.get('end', {})
            if 'dateTime' in start_item:
                when = datetime.fromisoformat(start_item['dateTime'].replace('Z', '+00:00')).astimezone(TZ).strftime('%d.%m %H:%M')
                finish = datetime.fromisoformat(end_item['dateTime'].replace('Z', '+00:00')).astimezone(TZ).strftime('%d.%m %H:%M')
                label = when + '–' + finish
            else:
                # Google's all-day end date is exclusive.
                last = datetime.fromisoformat(end_item['date']) - timedelta(days=1)
                label = start_item.get('date', '') + ' — ' + last.strftime('%Y-%m-%d') + ' (весь день)'
            lines.append(label + ' — ' + item.get('summary', 'Без названия')[:500])
        return 'Google Calendar (Самара):\n' + ('\n'.join(lines) if lines else 'Событий за этот период нет.') + (
            '\nПоказаны первые 100 событий. Уточни более короткий период.' if result.get('nextPageToken') else '')

    def busy(self, start, end):
        self.interval(start, end)
        data = self.api('POST', 'freeBusy', {'timeMin': start, 'timeMax': end,
            'timeZone': 'Europe/Samara', 'items': [{'id': 'primary'}]})
        cal = data.get('calendars', {}).get('primary')
        if cal is None or cal.get('errors'):
            raise CalendarError('Google не вернул занятость. Свободное время не подтверждено.')
        return cal.get('busy', [])

    def availability(self, start, end):
        a, b = self.interval(start, end)
        busy = self.busy(start, end)
        intervals = sorted((max(a, datetime.fromisoformat(i['start'].replace('Z', '+00:00'))),
                            min(b, datetime.fromisoformat(i['end'].replace('Z', '+00:00')))) for i in busy)
        free, cursor = [], a
        for left, right in intervals:
            if left > cursor:
                free.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < b:
            free.append((cursor, b))
        return 'Свободно в основном календаре (Самара), период ' + a.strftime('%d.%m %H:%M') + '–' + b.strftime('%d.%m %H:%M') + ':\n' + ('\n'.join(
            x.astimezone(TZ).strftime('%d.%m %H:%M') + '–' + y.astimezone(TZ).strftime('%d.%m %H:%M')
            for x, y in free) if free else 'Свободных промежутков в этом периоде нет.')

    def create_event(self, title, start, end, description, location, allow_conflict, operation_key):
        a, b = self.interval(start, end)
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise CalendarError('Уточни название встречи (до 500 символов).')
        for text in (description, location):
            if text is not None and (not isinstance(text, str) or len(text) > 2000):
                raise CalendarError('Описание и место — до 2000 символов.')
        if type(allow_conflict) is not bool:
            raise CalendarError('Уточни, разрешено ли пересечение встреч.')
        key = operation_key or secrets.token_hex(16)
        with self.db() as db:
            row = db.execute('SELECT event_id,body,email FROM google_operations WHERE operation_key=?', (key,)).fetchone()
        if row:
            event_id, serialized, email = row
            if email.casefold() != self.expected_email:
                raise CalendarError('Аккаунт Google изменился. Повтори поручение отдельным сообщением.')
            body = json.loads(serialized)
        else:
            if a.timestamp() <= time.time():
                raise CalendarError('Начало встречи уже прошло. Уточни будущую дату и время.')
            body = {'id': hashlib.sha256(('anton-calendar:' + key).encode()).hexdigest(),
                    'summary': title.strip(), 'start': {'dateTime': a.isoformat(), 'timeZone': 'Europe/Samara'},
                    'end': {'dateTime': b.isoformat(), 'timeZone': 'Europe/Samara'}}
            if description:
                body['description'] = description
            if location:
                body['location'] = location
            event_id = body['id']
        event_path = 'calendars/primary/events/' + event_id
        # Reconcile a previous insert that may have succeeded before a network timeout.
        if row:
            try:
                current = request_json('https://www.googleapis.com/calendar/v3/' + event_path, bearer=self.access_token())
                return self.created_message(current)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise CalendarError('Не удалось проверить предыдущую запись. Проверь календарь, прежде чем повторять создание.') from None
            except CalendarError:
                raise
            except Exception:
                raise CalendarError('Результат предыдущей записи пока неизвестен. Проверь календарь.') from None
        if self.busy(body['start']['dateTime'], body['end']['dateTime']) and not allow_conflict:
            raise CalendarError('В это время основной календарь занят. Встреча не создана. Выбери другое время или явно разреши пересечение.')
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO google_operations VALUES (?,?,?,?,?)',
                       (key, event_id, json.dumps(body), self.expected_email, int(allow_conflict)))
        try:
            request_json('https://www.googleapis.com/calendar/v3/calendars/primary/events',
                         'POST', body, self.access_token())
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise CalendarError('Google не подтвердил создание (HTTP ' + str(exc.code) + '). Проверь календарь или повтори /calendar_retry.') from None
        except CalendarError:
            raise
        except Exception:
            raise CalendarError('Ответ Google потерян. Событие могло сохраниться. Проверь календарь или повтори /calendar_retry.') from None
        current = self.api('GET', event_path)
        return self.created_message(current)

    def retry_last(self):
        with self.db() as db:
            row = db.execute('SELECT operation_key,body,allow_conflict FROM google_operations ORDER BY rowid DESC LIMIT 1').fetchone()
        if not row:
            return 'Нет предыдущей операции создания для проверки.'
        key, serialized, allowed = row
        body = json.loads(serialized)
        return self.create_event(body['summary'], body['start']['dateTime'], body['end']['dateTime'],
                                 body.get('description'), body.get('location'), bool(allowed), key)

    def created_message(self, event):
        if event.get('status') == 'cancelled':
            raise CalendarError('Предыдущее событие уже отменено в Google Calendar. Новое не создано.')
        a = datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00')).astimezone(TZ)
        b = datetime.fromisoformat(event['end']['dateTime'].replace('Z', '+00:00')).astimezone(TZ)
        # No attendees or conferencing are sent by this integration.
        return 'Событие подтверждено в Google Calendar: ' + event.get('summary', 'Без названия') + \
            '\n' + a.strftime('%d.%m.%Y %H:%M') + '–' + b.strftime('%d.%m.%Y %H:%M') + ' (Самара).\n' + \
            'Записано только в твой календарь. Приглашения участникам не отправлялись.' + \
            ('\n' + event['htmlLink'] if event.get('htmlLink', '').startswith('https://www.google.com/calendar/') else '')

    def serve(self, port):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Query string includes OAuth code and state.

            def do_GET(self):
                self.connection.settimeout(45)
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == '/health':
                    code, text = 200, 'ok'
                elif parsed.path == '/oauth/google/callback' and len(self.path) <= 16000:
                    try:
                        text = bridge.callback(urllib.parse.parse_qs(parsed.query))
                        code = 200
                    except CalendarError as exc:
                        code, text = 400, str(exc)
                    except Exception:
                        code, text = 500, 'Не удалось подключить Google. Повтори /calendar_connect.'
                else:
                    code, text = 404, 'Not found'
                payload = text.encode()
                self.send_response(code)
                for key, value in [('Content-Type', 'text/plain; charset=utf-8'), ('Cache-Control', 'no-store'),
                                   ('Referrer-Policy', 'no-referrer'), ('X-Content-Type-Options', 'nosniff'),
                                   ('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'"),
                                   ('Content-Length', str(len(payload)))]:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
