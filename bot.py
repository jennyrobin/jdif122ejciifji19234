import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from enum import Enum

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


logging.basicConfig(level=logging.INFO)


class Phase(str, Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    DAY = "day"


ROLE_LABELS = {
    "мафия": "мафия",
    "доктор": "доктор",
    "комиссар": "комиссар",
    "мирный": "мирный житель",
    "мирный_житель": "мирный житель",
}
ROLE_ORDER = ("мафия", "доктор", "комиссар", "мирный житель")


@dataclass
class Player:
    user_id: int
    name: str
    role: str = ""
    alive: bool = True


@dataclass
class Game:
    chat_id: int
    players: dict[int, Player] = field(default_factory=dict)
    phase: Phase = Phase.LOBBY
    round: int = 0
    night_actions: dict[str, int] = field(default_factory=dict)
    votes: dict[int, int] = field(default_factory=dict)
    roles_config: dict[str, int] | None = None


games: dict[int, Game] = {}


def display_name(user) -> str:
    return user.full_name or user.username or str(user.id)


def alive_players(game: Game) -> list[Player]:
    return [player for player in game.players.values() if player.alive]


def default_roles(player_count: int) -> dict[str, int]:
    mafia = max(1, player_count // 4)
    doctor = 1 if player_count >= 4 else 0
    commissioner = 1 if player_count >= 4 else 0
    return {
        "мафия": mafia,
        "доктор": doctor,
        "комиссар": commissioner,
        "мирный житель": max(0, player_count - mafia - doctor - commissioner),
    }


def winner(game: Game) -> str | None:
    mafia = sum(p.alive and p.role == "мафия" for p in game.players.values())
    townsfolk = sum(p.alive and p.role != "мафия" for p in game.players.values())
    if mafia == 0:
        return "🏘 Мирные жители победили!"
    if mafia >= townsfolk:
        return "🔪 Мафия победила!"
    return None


def target_keyboard(game: Game, action: str, actor_id: int, *, include_self: bool = False) -> InlineKeyboardMarkup:
    targets = [p for p in alive_players(game) if include_self or p.user_id != actor_id]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p.name, callback_data=f"{action}:{game.chat_id}:{p.user_id}")]
        for p in targets
    ])


def night_roles_done(game: Game) -> bool:
    required = {p.role for p in alive_players(game)} & {"мафия", "доктор", "комиссар"}
    return required.issubset(game.night_actions.keys())


async def finish_game(bot: Bot, chat_id: int, message: str) -> None:
    await bot.send_message(chat_id, message)
    games.pop(chat_id, None)


async def begin_day(bot: Bot, game: Game) -> None:
    target_id = game.night_actions.get("мафия")
    saved_id = game.night_actions.get("доктор")
    events: list[str] = []

    if target_id is not None and target_id != saved_id:
        victim = game.players[target_id]
        victim.alive = False
        events.append(f"🌅 Ночью убит(а): {victim.name}.")
    else:
        events.append("🌅 Ночь прошла спокойно: никто не погиб.")

    result = winner(game)
    if result:
        await finish_game(bot, game.chat_id, "\n".join(events + [result]))
        return

    game.phase = Phase.DAY
    game.votes.clear()
    events.append("☀️ День. Обсудите подозрения и проголосуйте кнопкой ниже. Каждый живой игрок может проголосовать один раз.")
    await bot.send_message(
        game.chat_id,
        "\n".join(events),
        reply_markup=target_keyboard(game, "vote", actor_id=-1, include_self=True),
    )


async def resolve_vote(bot: Bot, game: Game) -> None:
    counts: dict[int, int] = {}
    for target_id in game.votes.values():
        counts[target_id] = counts.get(target_id, 0) + 1
    highest = max(counts.values(), default=0)
    leaders = [target_id for target_id, count in counts.items() if count == highest]
    if len(leaders) != 1:
        result_text = "⚖️ Голосование закончилось ничьей. Никого не исключаем."
    else:
        eliminated = game.players[leaders[0]]
        eliminated.alive = False
        result_text = f"🗳️ Из игры исключён(а): {eliminated.name}. Его/её роль — {eliminated.role}."

    result = winner(game)
    if result:
        await finish_game(bot, game.chat_id, f"{result_text}\n{result}")
        return

    game.phase = Phase.NIGHT
    game.round += 1
    game.night_actions.clear()
    await bot.send_message(game.chat_id, f"{result_text}\n🌙 Раунд {game.round}: наступила ночь. Роли с ночным действием — проверьте личные сообщения бота.")
    await send_night_prompts(bot, game)


async def send_night_prompts(bot: Bot, game: Game) -> None:
    prompts = {
        "мафия": "🌙 Выберите игрока, которого устранить этой ночью:",
        "доктор": "🌙 Выберите игрока, которого защитить этой ночью:",
        "комиссар": "🌙 Выберите игрока для проверки:",
    }
    for player in alive_players(game):
        if player.role not in prompts:
            continue
        try:
            await bot.send_message(
                player.user_id,
                prompts[player.role],
                reply_markup=target_keyboard(game, "act", player.user_id, include_self=player.role == "доктор"),
            )
        except TelegramAPIError:
            logging.warning("Could not send a night prompt to user %s", player.user_id)


async def start_night(bot: Bot, game: Game) -> None:
    game.phase = Phase.NIGHT
    game.round = 1
    game.night_actions.clear()
    await bot.send_message(game.chat_id, "🌙 Наступила ночь. Мафия, доктор и комиссар — выберите действие в личном чате с ботом.")
    await send_night_prompts(bot, game)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except TelegramAPIError:
        return False


async def on_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот для игры в «Мафию». Добавьте меня в групповой чат.\n\n"
        "Команды в группе:\n/newgame — создать лобби\n/join — присоединиться\n"
        "/roles — показать состав ролей\n/setrole <роль> <число> — настроить состав (только администратор)\n"
        "/startgame — начать игру (от 4 игроков)\n/endgame — завершить игру\n\n"
        "Роли и ночные действия приходят в личные сообщения. Чтобы получать их, сначала нажмите /start в личном чате со мной."
    )


async def new_game(message: Message) -> None:
    if not message.chat or message.chat.type == "private":
        await message.answer("Создайте игру в групповом чате.")
        return
    current = games.get(message.chat.id)
    if current and current.phase != Phase.LOBBY:
        await message.answer("Игра уже началась. Завершите её командой /endgame перед созданием новой.")
        return
    games[message.chat.id] = Game(chat_id=message.chat.id)
    await message.answer("🎭 Лобби создано! Напишите /join, чтобы присоединиться. Для начала игры нужно минимум 4 игрока.")


async def join_game(message: Message) -> None:
    if not message.chat or message.chat.type == "private" or not message.from_user:
        await message.answer("Присоединиться можно из группового чата.")
        return
    game = games.get(message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await message.answer("Сейчас нет открытого лобби. Создайте его командой /newgame.")
        return
    if message.from_user.id in game.players:
        await message.answer(f"Вы уже в игре, {display_name(message.from_user)}!")
        return
    game.players[message.from_user.id] = Player(message.from_user.id, display_name(message.from_user))
    if game.roles_config is not None:
        game.roles_config["мирный житель"] += 1
    await message.answer(f"✅ {display_name(message.from_user)} присоединился(ась). Игроков: {len(game.players)}.")


async def show_roles(message: Message) -> None:
    if not message.chat or message.chat.type == "private":
        await message.answer("Посмотреть настройки ролей можно в групповом чате.")
        return
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Сначала создайте лобби командой /newgame.")
        return
    config = game.roles_config or default_roles(len(game.players))
    lines = [f"• {role}: {config.get(role, 0)}" for role in ROLE_ORDER]
    mode = "настройки администратора" if game.roles_config is not None else "по умолчанию"
    await message.answer(
        f"🎭 Роли ({mode}; игроков в лобби: {len(game.players)}):\n" + "\n".join(lines) +
        "\n\nНастроить может администратор: /setrole <мафия|доктор|комиссар|мирный> <число>."
    )


async def set_role(message: Message, bot: Bot) -> None:
    if not message.chat or message.chat.type == "private" or not message.from_user:
        await message.answer("Настраивать роли можно только администратору в групповом чате.")
        return
    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("⛔ Настраивать роли может только администратор группы.")
        return
    game = games.get(message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await message.answer("Настраивать состав ролей можно только в открытом лобби.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: /setrole <мафия|доктор|комиссар|мирный> <число>\nПример: /setrole мафия 2")
        return
    role = ROLE_LABELS.get(parts[1].lower())
    try:
        count = int(parts[2])
    except ValueError:
        count = -1
    if role is None or count < 0:
        await message.answer("Укажите роль мафия, доктор, комиссар или мирный и целое число не меньше 0.")
        return
    if role == "мафия" and count < 1:
        await message.answer("В игре должна быть хотя бы одна мафия.")
        return
    if not game.players:
        await message.answer("Сначала пусть игроки присоединятся командой /join, затем настройте роли.")
        return

    config = dict(game.roles_config or default_roles(len(game.players)))
    if role == "мирный житель":
        config[role] = count
        if sum(config.values()) != len(game.players):
            await message.answer(
                f"Число мирных жителей должно быть {len(game.players) - sum(v for r, v in config.items() if r != role)} "
                f"при текущем составе ({len(game.players)} игрока). Остальные роли: "
                f"мафия {config['мафия']}, доктор {config['доктор']}, комиссар {config['комиссар']}."
            )
            return
    else:
        config[role] = count
        remaining = len(game.players) - sum(config[r] for r in ROLE_ORDER if r != "мирный житель")
        if remaining < 0:
            await message.answer("Сумма специальных ролей больше числа игроков. Измените другие роли или добавьте игроков.")
            return
        config["мирный житель"] = remaining

    game.roles_config = config
    await message.answer("✅ Состав ролей обновлён:\n" + "\n".join(f"• {r}: {config[r]}" for r in ROLE_ORDER))


async def start_game(message: Message, bot: Bot) -> None:
    if not message.chat or message.chat.type == "private":
        await message.answer("Начать игру можно в групповом чате.")
        return
    game = games.get(message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await message.answer("Нет открытого лобби. Создайте его командой /newgame.")
        return
    if len(game.players) < 4:
        await message.answer("Для игры нужно минимум 4 игрока.")
        return
    roles_config = game.roles_config or default_roles(len(game.players))
    if sum(roles_config.values()) != len(game.players) or roles_config.get("мафия", 0) < 1:
        await message.answer("Состав ролей не совпадает с числом игроков. Проверьте /roles и настройте его командой /setrole.")
        return

    roles = [role for role, count in roles_config.items() for _ in range(count)]
    random.shuffle(roles)
    for player, role in zip(game.players.values(), roles):
        player.role = role

    try:
        for player in game.players.values():
            await bot.send_message(player.user_id, f"🎭 Ваша роль: {player.role}. Не показывайте это сообщение другим игрокам!")
    except TelegramAPIError:
        for player in game.players.values():
            player.role = ""
        await message.answer("Не удалось отправить роль одному из игроков. Пусть каждый откроет личный чат с ботом и нажмёт /start, затем повторите /startgame.")
        return

    await message.answer(f"🎭 Игра начинается! Игроков: {len(game.players)}. Роли отправлены в личные сообщения.")
    await start_night(bot, game)


async def end_game(message: Message) -> None:
    if not message.chat or message.chat.type == "private":
        await message.answer("Завершить игру можно в групповом чате.")
        return
    if games.pop(message.chat.id, None):
        await message.answer("Игра завершена. Чтобы сыграть снова, используйте /newgame.")
    else:
        await message.answer("В этом чате нет активной игры.")


async def status(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        await message.answer("В этом чате нет активной игры. Создайте её командой /newgame.")
        return
    players = "\n".join(f"• {p.name}{'' if p.alive else ' — выбыл(а)'}" for p in game.players.values()) or "Пока никого нет."
    await message.answer(f"Фаза: {game.phase.value}\nИгроков: {len(alive_players(game))}/{len(game.players)}\n{players}")


async def vote_callback(callback: CallbackQuery, bot: Bot, chat_id: int, target_id: int) -> None:
    game = games.get(chat_id)
    voter_id = callback.from_user.id
    if not game or game.phase != Phase.DAY or voter_id not in game.players or not game.players[voter_id].alive:
        await callback.answer("Сейчас голосовать нельзя.", show_alert=True)
        return
    if target_id not in game.players or not game.players[target_id].alive:
        await callback.answer("Этот игрок уже выбыл.", show_alert=True)
        return
    if voter_id in game.votes:
        await callback.answer("Вы уже проголосовали.", show_alert=True)
        return
    game.votes[voter_id] = target_id
    await callback.answer("Ваш голос учтён.")
    if len(game.votes) == sum(p.alive for p in game.players.values()):
        await resolve_vote(bot, game)


async def action_callback(callback: CallbackQuery, bot: Bot, chat_id: int, target_id: int) -> None:
    game = games.get(chat_id)
    actor_id = callback.from_user.id
    actor = game.players.get(actor_id) if game else None
    target = game.players.get(target_id) if game else None
    if not game or game.phase != Phase.NIGHT or not actor or not actor.alive or not target or not target.alive:
        await callback.answer("Это действие сейчас недоступно.", show_alert=True)
        return
    if actor.role not in {"мафия", "доктор", "комиссар"}:
        await callback.answer("У вашей роли нет ночного действия.", show_alert=True)
        return
    if actor.role != "доктор" and actor_id == target_id:
        await callback.answer("Нельзя выбрать себя.", show_alert=True)
        return

    game.night_actions[actor.role] = target_id
    await callback.answer("Действие принято.")
    if actor.role == "комиссар":
        await callback.message.answer(f"🔎 Проверка: {target.name} — {'мафия' if target.role == 'мафия' else 'не мафия'}.")
    if night_roles_done(game):
        await begin_day(bot, game)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите токен бота в переменной окружения TELEGRAM_BOT_TOKEN")

    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.message.register(on_start, CommandStart())
    dispatcher.message.register(new_game, Command("newgame"))
    dispatcher.message.register(join_game, Command("join"))
    dispatcher.message.register(show_roles, Command("roles"))
    dispatcher.message.register(set_role, Command("setrole"))
    dispatcher.message.register(start_game, Command("startgame"))
    dispatcher.message.register(end_game, Command("endgame"))
    dispatcher.message.register(status, Command("status"))

    @dispatcher.callback_query(F.data.startswith("vote:"))
    async def on_vote(callback: CallbackQuery) -> None:
        try:
            _, chat, target = callback.data.split(":")
            await vote_callback(callback, bot, int(chat), int(target))
        except (ValueError, AttributeError):
            await callback.answer("Некорректный голос.", show_alert=True)

    @dispatcher.callback_query(F.data.startswith("act:"))
    async def on_action(callback: CallbackQuery) -> None:
        try:
            _, chat, target = callback.data.split(":")
            await action_callback(callback, bot, int(chat), int(target))
        except (ValueError, AttributeError):
            await callback.answer("Некорректное действие.", show_alert=True)

    asyncio.run(dispatcher.start_polling(bot))


if __name__ == "__main__":
    main()
