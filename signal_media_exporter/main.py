#!/usr/bin/env python3

import argparse
import coloredlogs
import logging
import json
import os
import re
import shutil
import sys

from pathlib import Path
from pysqlcipher3 import dbapi2 as sqlite
from signal_media_exporter.attachments import stats, make_fs_name
from signal_media_exporter.conversations import export_conversation


logger = logging.getLogger(__name__)


## DB

def get_key(config):
    with open(os.path.join(config['signalDir'], 'config.json'), 'r') as f:
        signal_config = json.load(f)

    key = signal_config['key']
    logger.info('Read sqlcipher key: 0x%s...', key[:8])
    return key


def get_conversations(cursor):
    # FIXME handle contacts/convos with identical names (or FS names)
    cursor.execute('select json from conversations')
    conversations = [json.loads(row[0]) for row in cursor.fetchall()]

    # Set display name and filesystem name
    for conv in conversations:
        conv['displayName'] = conv.get('name', '') or conv.get('profileName', '') or conv['e164']
        conv['fsName'] = make_fs_name(conv['displayName'])

    return sorted(conversations, key=(lambda conv: conv['displayName']))


def group_contacts_by_number(conversations):
    return {conv['e164']: conv for conv in conversations if 'e164' in conv}


def get_messages(config, cursor, conversation_id=None):
    # logger.info('Reading messages...')
    cursor.execute("select json from items where id=?", ('number_id',))
    number_id = json.loads(cursor.fetchone()[0])
    own_number, device_id = number_id['value'].split('.')
    # logger.info('Own number: %s, device ID: %s', own_number, device_id)

    cond = []
    if conversation_id:
        cond.append(f"conversationId='{conversation_id}'")
    if not config['includeExpiringMessages']:
        cond.append("expires_at is null")
    if not config['includeTechnicalMessages']:
        cond.append("type in ('incoming', 'outgoing')")  # doesn't exclude error messages and group updates
    if config['messageId']:
        cond.append(f"id='{config['messageId']}'")
    cursor.execute(f"""
        select json
        from messages
        where { ' and '.join(cond) }
        order by sent_at asc
        {f'limit {config["maxMessages"]}' if config["maxMessages"] > 0 else ''}
        """)

    for row in cursor:
        msg = json.loads(row[0])

        if 'source' not in msg and msg['type'] == 'outgoing':
            msg['source'] = own_number

        yield msg


## Main

def get_config():
    config = {
        'config': './config.json',
        'includeExpiringMessages': False,
        'includeTechnicalMessages': False,
        'maxAttachments': 0,
        'maxMessages': 0,
        'messageId': '',
        'outputDir': './media',
        'signalDir': Path.home() / '.config/Signal',
        'sqlcipher': {
            'cipher_compatibility': 4
        }
    }

    parser = argparse.ArgumentParser(description='Media file exporter for Signal Desktop.')
    parser.add_argument('-c', '--config', type=str,
                        help=f"path of config file to read (default: {config['config']})")
    parser.add_argument('-o', '--output-dir', type=str,
                        help=f"output directory for media files (default: {config['outputDir']})")
    parser.add_argument('-s', '--signal-dir', type=str,
                        help=f"Signal Desktop profile directory (default: {config['signalDir']})")
    parser.add_argument('-e', '--include-expiring-messages', action='store_const', const=True,
                        help="include expiring messages (default: no)")
    parser.add_argument('--include-technical-messages', action='store_const', const=True,
                        help="include technical messages (default: no)")
    parser.add_argument('--max-attachments', metavar='N', type=int,
                        help=f"export at most N attachments then export messages without them (default: 0 = no limit)")
    parser.add_argument('--max-messages', metavar='N', type=int,
                        help=f"export at most N messages then stop (default: 0 = no limit)")
    parser.add_argument('--message-id', metavar='ID', type=str,
                        help=f"export a single message with the given ID (default: '' = disabled)")
    args = parser.parse_args()

    # command line args override the settings from the config file, which override the default settings
    try:
        with open(args.config if args.config else config['config'], 'r') as f:
            config = {**config, **json.load(f)}
    except FileNotFoundError:
        if args.config: raise

    for arg, value in vars(args).items():
        if value is None: continue

        arg_camelcase = ''.join(w.lower() if i == 0 else w.title() for i,w in enumerate(arg.split('_')))
        config[arg_camelcase] = value

    # sanitize phone numbers: remove non-digits
    sanitize = lambda no: re.sub(r'[^+\d]', '', no)
    try: config['map'] = { sanitize(number): name for number, name in config['map'].items() }
    except KeyError: pass

    # validate maxAttachments
    if config['maxAttachments'] < 0:
        logger.error(f'Invalid max number of attachments {config["maxAttachments"]} (must be >= 0).')
        sys.exit(1)

    # validate maxMessages
    if config['maxMessages'] < 0:
        logger.error(f'Invalid max number of messages {config["maxMessages"]} (must be >= 0).')
        sys.exit(1)

    # validate messageId
    if not re.match(r'^[\da-f-]*$', config['messageId']):
        logger.error(f'Invalid message ID {config["messageId"]}.')
        sys.exit(1)

    return config


def run_export(config):
    # Copy style.css
    os.makedirs(config['outputDir'], exist_ok=True)
    package_dir = os.path.dirname(sys.modules[__package__].__file__)
    shutil.copy(os.path.join(package_dir, "style.css"), config['outputDir'])
    style_css_size = os.path.getsize(os.path.join(package_dir, "style.css"))
    logger.info('Copied %s [%.1f KiB]', "style.css", style_css_size / 1024)

    key = get_key(config)
    logger.info('Connecting to db.sqlite...')
    db_uri = f"file:{os.path.join(config['signalDir'], 'sql', 'db.sqlite')}?mode=ro"  # read-only
    conn = sqlite.connect(db_uri, uri=True)
    try:
        cursor = conn.cursor()

        # DB setup
        cursor.execute(f"PRAGMA key=\"x'{key}'\"")
        for setting, value in config.get('sqlcipher', {}).items():
            cursor.execute(f"PRAGMA {setting}={value}")

        # Get conversation metadata
        conversations = get_conversations(cursor)
        contacts_by_number = group_contacts_by_number(conversations)

        # Export messages and attachments
        for conversation in conversations:
            msgs = list(get_messages(config, cursor, conversation['id']))
            export_conversation(conversation, msgs, config, contacts_by_number)

        logger.info(
            'Done. %d messages, %d media attachments [%.1f MiB], %d attachments saved [%.1f MiB].',
            stats['messages'],
            stats['attachments'],
            stats['attachments_size'] / 2 ** 20,
            stats['saved_attachments'],
            stats['saved_attachments_size'] / 2 ** 20,
        )

    except sqlite.DatabaseError as err:
        logger.fatal(
            'DatabaseError "%s" - please check the database and the sqlcipher parameters!',
            ' | '.join(err.args))

    finally:
        conn.close()


def main():
    coloredlogs.install()
    config = get_config()
    run_export(config)
