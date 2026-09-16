# Telegram Bot — Railway PostgreSQL Ready

This package keeps the existing bot features and changes only the database layer from SQLite to PostgreSQL.

## Railway setup
1. Create a Railway project.
2. Add a **PostgreSQL** service.
3. Deploy this bot as a separate service from this folder/repository.
4. Connect the PostgreSQL service to the bot service so `DATABASE_URL` is available.
5. Add Variables on the bot service:
   - `BOT_TOKEN` = your Telegram BotFather token
   - `ADMIN_ID` = your numeric Telegram admin ID
6. Deploy. The bot creates its required tables automatically on first start.

## Important
- The live database is PostgreSQL, not a local `.db` file.
- Restarts and redeployments keep PostgreSQL data.
- Do NOT put your BOT_TOKEN in `main.py` or commit it to GitHub.
- If the old bot had important SQLite data, this package does not automatically migrate that old data because the uploaded source contained code only. Export/migrate the old SQLite database before switching production databases.

## Included bot features
Force Join, Start page, configurable media/text/buttons, broadcast, channel management,
statistics, live user chat, admin reply mapping, temporary success message, admin-only access,
and the existing Telegram button styling helpers are preserved from the supplied source.
