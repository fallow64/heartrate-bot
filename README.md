# heartrate-bot

A Discord bot that keeps one message updated with my live heart rate, read from an Apple Watch through [Pulsoid](https://pulsoid.net).

## Invite

[Add the bot to a server](https://discord.com/oauth2/authorize?client_id=1569210924693200937&permissions=117760&scope=bot)

It needs View Channel, Send Messages, Embed Links, Attach Files and Read Message History.

The bot keeps its message at the bottom of the channel: when anything is posted below it, it deletes its own message and sends a new one. It never touches other messages, and it refuses to start unless the channel name contains `fallow`.
