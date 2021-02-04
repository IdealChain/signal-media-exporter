#!/usr/bin/env python3

import argparse
import coloredlogs
import logging
import hashlib
import json
import os
import shutil
import re
import sys

from datetime import datetime
from pathlib import Path
from sqlcipher3 import dbapi2 as sqlite

logger = logging.getLogger(__name__)

def get_key(config):
    with open(os.path.join(config['signalDir'], 'config.json'), 'r') as f:
        signal_config = json.load(f)

    key = signal_config['key']
    logger.info('Read sqlcipher key: 0x%s...', key[:8])
    return key

def get_messages(config, key):
    logger.info('Connecting to sql/db.sqlite, reading messages...')
    conn = sqlite.connect(os.path.join(config['signalDir'], 'sql/db.sqlite'))
    try:
        c = conn.cursor()
        c.execute(f"PRAGMA key=\"x'{key}'\"")
        for setting, value in config.get('sqlcipher', {}).items():
            c.execute(f"PRAGMA {setting}={value}")

        c.execute("select json from items where id=?", ('number_id',))
        number_id = json.loads(c.fetchone()[0])
        own_number, device_id = number_id['value'].split('.')
        logger.info('Own number: %s, device ID: %s', own_number, device_id)

        cond = ["hasVisualMediaAttachments > 0"]
        if not config.get('includeExpiringMessages', False):
            cond.append("expires_at is null")
        c.execute(f"""
            select id, json
            from messages
            where { ' and '.join(cond) }
            order by sent_at asc
            {f'limit {config["maxMessages"]}' if config["maxMessages"] > 0 else ''}
            """)

        for row in c:
            msg = json.loads(row[1])

            if 'source' not in msg and msg['type'] == 'outgoing':
                msg['source'] = own_number

            yield (row[0], msg)

    except sqlite.DatabaseError as err:
        logger.fatal(
            'DatabaseError "%s" - please check the database and the sqlcipher parameters!',
            ' | '.join(err.args))

    finally:
        conn.close()

def hash_file_quick(path):
    with open(path, 'br') as f:
        data = f.read(2 ** 10)
        return hash(data)

def hash_file_sha256(path):
    sha256 = hashlib.sha256()
    with open(path, 'br') as f:
        while True:
            data = f.read(2 ** 12)
            if not data:
                break
            sha256.update(data)

        return sha256.hexdigest()

def save_attachments(config, hashes, id, msg):
    stats = {
        'attachments': 0,
        'attachments_size': 0,
        'saved_attachments': 0,
        'saved_attachments_size': 0,
    }

    try:
        sent = datetime.fromtimestamp(msg['sent_at'] / 1000)
        recvd = datetime.fromtimestamp(msg['received_at'] / 1000)

        # translate number of sender to name
        sender = msg['source']
        if 'map' in config:
            sender = config['map'][sender]

    except KeyError as e:
        if e.args[0].startswith('+'):
            logger.warning('Skipping %s (number not mapped: "%s")', id, '.'.join(e.args))
        else:
            logger.warning('Skipping %s (field missing: "%s")', id, '.'.join(e.args))
        return

    for idx, at in enumerate(msg['attachments']):
        if at['contentType'].lower().startswith(('image/', 'video/', 'audio/')):
            ext = at['contentType'].lower().split('/')[1]
        else:
            continue

        name = ['signal', sent.strftime('%Y-%m-%d-%H%M%S')]
        if len(msg['attachments']) > 1:
            name += str(idx)
        name = '{}.{}'.format('-'.join(name), ext)

        if at.get('pending', False) or not at.get('path'):
            logger.warning('Skipping %s/%s (media file not downloaded)', sender, name)
            continue
        # if accessing a Windows signal database, need to fix paths
        if '\\' in at['path']:
            atPath = os.path.join(*at['path'].split('\\'))
        else:
            atPath = at['path']
        src = os.path.join(config['signalDir'], 'attachments.noindex', atPath)
        dst = os.path.join(config['outputDir'], sender, name)
        if not os.path.exists(src):
            logger.warning('Skipping %s/%s (media file not found)', sender, name)
            continue

        stats['attachments'] = stats['attachments'] + 1
        stats['attachments_size'] = stats['attachments_size'] + os.path.getsize(src)

        quick_hash = hash_file_quick(src)
        if quick_hash in hashes:
            if hash_file_sha256(src) in (hash_file_sha256(f) for f in hashes[quick_hash]):
                logger.info('Skipping %s/%s (already saved an identical file)', sender, name)
                continue

        if os.path.exists(dst):
            logger.debug('Skipping %s/%s (file exists)', sender, name)
            hashes.setdefault(quick_hash, []).append(src)
            continue

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        try:
            os.utime(dst, times=(sent.timestamp(), sent.timestamp()))
        except PermissionError:
            pass
        size = os.path.getsize(dst)
        logger.info('Saved %s [%.1f KiB]', dst, size / 1024)

        stats['saved_attachments'] = stats['saved_attachments'] + 1
        stats['saved_attachments_size'] = stats['saved_attachments_size'] + size
        hashes.setdefault(quick_hash, []).append(src)

    return stats

def main():
    coloredlogs.install()
    config = {
        'config': './config.json',
        'maxMessages': 0,
        'outputDir': './media',
        'signalDir': os.path.join(Path.home(), '.config/Signal'),
        'sqlcipher': {
            'cipher_compatibility': 4
        }
    }

    parser = argparse.ArgumentParser(description='Media file exporter for Signal Desktop.')
    parser.add_argument('-c', '--config', nargs='?', type=str,
                        help=f"path of config file to read (default: {config['config']})")
    parser.add_argument('-o', '--output-dir', nargs='?', type=str,
                        help=f"output directory for media files (default: {config['outputDir']})")
    parser.add_argument('-s', '--signal-dir', nargs='?', type=str,
                        help=f"Signal Desktop profile directory (default: {config['signalDir']})")
    parser.add_argument('-e', '--include-expiring-messages', action='store_const', const=True,
                        help="include expiring messages (default: no)")
    parser.add_argument('--max-messages', metavar='N', nargs='?', type=int,
                        help=f"Export media for at most N messages then stop (default: 0 = no limit)")
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
    sanitize = lambda no: re.sub('[^+\d]', '', no)
    try: config['map'] = { sanitize(number): name for number, name in config['map'].items() }
    except KeyError: pass

    # validate maxMessages
    if config['maxMessages'] < 0:
        logger.error(f'Invalid max number of messages {config["maxMessages"]} (must be >= 0).')
        sys.exit(-1)

    # read the encrypted DB and run the export
    key = get_key(config)
    msgs = list(get_messages(config, key))
    stats = {}
    hashes = {}

    for i, msg in enumerate(msgs):
        msg_stats = save_attachments(config, hashes, *msg)
        for key, value in msg_stats.items() if msg_stats else {}:
            stats[key] = stats.setdefault(key, 0) + value
        if i > 0 and not i % 50:
            logger.info('%04d/%04d messages | %.1f %% processed', i, len(msgs), i/len(msgs)*100)

    if not stats:
        logger.error('No media messages found.')
        sys.exit(-1)

    logger.info(
        'Done. %d messages, %d media attachments [%.1f MiB], %d attachments saved [%.1f MiB].',
        len(msgs),
        stats['attachments'],
        stats['attachments_size'] / 2 ** 20,
        stats['saved_attachments'],
        stats['saved_attachments_size'] / 2 ** 20,
    )
