import discord
from discord import FFmpegPCMAudio
from discord.ext import commands
from discord.ext.commands import Bot
from discord.voice_client import VoiceClient
from discord.utils import get

from os import walk

bot = commands.Bot(command_prefix="?")

mp3FilesList = []

def play_mp3(name, voice_channel):
    voice_channel.play(discord.FFmpegPCMAudio('./mp3/' + name + '.mp3'))

@bot.event
async def on_ready():
    global mp3FilesList

    _, _, mp3Files = next(walk('./mp3'))
    mp3FilesList = [mp3File[:-4] for mp3File in mp3Files]

    print('Bot is running!')

@bot.event
async def on_message(message):
    content = message.content
    channel = message.author.voice.channel

    if content.startswith('?'):
        mp3FileName = content[1:]

        if not bot.voice_clients:
            await channel.connect()

        if mp3FileName in mp3FilesList:
            for vc in bot.voice_clients:
                if vc.guild == message.guild:
                    play_mp3(mp3FileName, vc)
    elif content.startswith('/'):
        if content.startswith('/join'):
            await channel.connect()
        elif content.startswith('/leave'):
            for vc in bot.voice_clients:
                if vc.guild == message.guild:
                    await vc.disconnect()

bot.run('NDY0Nzg2NDkwNTUyMDkwNjI0.Wz9wJg.NCklFLCDfbOlsUSgnNjiBKXhAbk')
