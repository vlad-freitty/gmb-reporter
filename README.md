# GBP Reviews Watch

Моніторинг нових і змінених відгуків Google Business Profile по всіх локаціях
FREITTY, з алертами в Telegram.

Запускається автоматично через GitHub Actions кожні 5 хвилин.
Стан (які відгуки вже бачили) комітиться в `state/`.

## Secrets (Settings -> Secrets and variables -> Actions)

| Secret | Де взяти |
|---|---|
| GBP_CLIENT_ID | Google Cloud Console -> Credentials -> OAuth client (Desktop app) |
| GBP_CLIENT_SECRET | там же |
| GBP_REFRESH_TOKEN | локально один раз: `python gbp_reviews_watch.py --auth` |
| GBP_ACCOUNT_ID | Actions -> Run workflow -> mode `--accounts`, дивись лог |
| TG_BOT_TOKEN | @BotFather |
| TG_CHAT_ID | api.telegram.org/bot<TOKEN>/getUpdates |

## Ручні запуски

Actions -> GBP Reviews Watch -> Run workflow -> вписати mode:

- `--accounts`  показати акаунти і кількість локацій в кожному
- `--locations` перекешувати список локацій (після додавання нової)
- `--dry-run`   прогнати цикл, нічого не відправляти
- `--init`      зафіксувати поточну історію відгуків, нічого не відправляти
- (пусто)       звичайний цикл

## Порядок першого запуску

1. Залити файли, додати перші три secrets
2. Run workflow -> `--accounts` -> взяти id з 26 локаціями -> додати GBP_ACCOUNT_ID
3. Додати TG secrets
4. Run workflow -> `--init`
5. Далі працює саме

Thank you
