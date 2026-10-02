import getpass
from bot import post

token = getpass.getpass('Токен BotFather (ввод скрыт): ').strip()
try:
    data = post('https://api.telegram.org/bot' + token + '/getUpdates',
                {'timeout': 0, 'allowed_updates': ['message']})
    users = {}
    for item in data.get('result', []):
        message = item.get('message', {})
        if message.get('chat', {}).get('type') == 'private':
            user = message.get('from', {})
            users[user['id']] = user
    for uid, user in users.items():
        print(uid, user.get('first_name', ''), user.get('username', ''))
    if not users:
        print('Нет личных сообщений. Напишите своему боту и повторите.')
except Exception:
    print('Не удалось получить ID. Проверьте токен и остановите запущенный сервис бота.')
