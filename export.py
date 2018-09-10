#!/usr/bin/env python3

import argparse
import coloredlogs
import logging
import hashlib
import json
import os
import shutil
import re

from datetime import datetime
from pathlib import Path
from pysqlcipher3 import dbapi2 as sqlite

logger = logging.getLogger(__name__)

def get_key(config):
    with open(os.path.join(config['signalDir'], 'config.json'), 'r') as f:
        signal_config = json.load(f)

    key = signal_config['key']
    logger.info('Read sqlcipher key: %s', key)
    return key

def get_messages(config, key):
    conn = sqlite.connect(os.path.join(config['signalDir'], 'sql/db.sqlite'))
    logger.info('Connected to sql/db.sqlite, reading messages...')
    try:
        c = conn.cursor()
        c.execute(f"PRAGMA key=\"x'{key}'\"")

        cond = ["hasVisualMediaAttachments > 0"]
        if not config.get('includeExpiringMessages', False):
            cond.append("expires_at is null")
        c.execute(f"""
            select id, json
            from messages
            where { ' and '.join(cond) }
            order by sent_at asc
            """)
        res = c.fetchall()

        logger.info('Got %d messages with media attachments',  len(res))
        return [(m[0], json.loads(m[1])) for m in res]

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

        if 'source' in msg:
            sender = msg['source']
        elif 'source' not in msg and msg['type'] == 'outgoing':
            sender = config['ownNumber']

        # translate number of sender to name
        if 'map' in config:
            sender = config['map'][sender]

    except KeyError as e:
        if e.args[0] == 'ownNumber':
            logger.warning('Skipping %s (own number not set)', id)
        elif e.args[0].startswith('+'):
            logger.warning('Skipping %s (number not mapped: "%s")', id, '.'.join(e.args))
        else:
            logger.warning('Skipping %s (field missing: "%s")', id, '.'.join(e.args))
        return

    for at in msg['attachments']:
        if at['contentType'].lower().startswith(('image/', 'video/', 'audio/')):
            ext = at['contentType'].lower().split('/')[1]
        else:
            continue

        name = 'signal-{}.{}'.format(sent.strftime('%Y-%m-%d-%H%M%S'), ext)
        src = os.path.join(config['signalDir'], 'attachments.noindex', at['path'])
        dst = os.path.join(config['outputDir'], sender, name)
        if not os.path.exists(src):
            logger.warning('Skipping %s (file does not exist)', src)
            continue

        stats['attachments'] = stats['attachments'] + 1
        stats['attachments_size'] = stats['attachments_size'] + os.path.getsize(src)

        quick_hash = hash_file_quick(src)
        if quick_hash in hashes:
            if hash_file_sha256(src) in (hash_file_sha256(f) for f in hashes[quick_hash]):
                logger.info('Skipping %s (already saved an identical file)', dst)
                continue

        if os.path.exists(dst):
            logger.debug('Skipping %s (file exists)', dst)
            hashes.setdefault(quick_hash, []).append(src)
            continue

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)
        os.utime(dst, times=(sent.timestamp(), sent.timestamp()))
        size = os.path.getsize(dst)
        logger.info('Saved %s [%.1f KiB]', dst, size / 1024)

        stats['saved_attachments'] = stats['saved_attachments'] + 1
        stats['saved_attachments_size'] = stats['saved_attachments_size'] + size
        hashes.setdefault(quick_hash, []).append(src)

    return stats

if __name__ == '__main__':
    coloredlogs.install()
    config = {
        'config': './config.json',
        'outputDir': './media',
        'signalDir': os.path.join(Path.home(), '.config/Signal'),
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
    parser.add_argument('-n', '--own-number', nargs='?', type=str,
                        help="own phone number (sender for outgoing messages)")
    args = parser.parse_args()

    # command line args override the settings from the config file, which override the default settings
    try:
        with open(args.config if args.config else config['config'], 'r') as f:
            config = {**config, **json.load(f)}
    except FileNotFoundError:
        if args.config: raise

    for arg, value in vars(args).items():
        if not value: continue

        arg_camelcase = ''.join(w.lower() if i == 0 else w.title() for i,w in enumerate(arg.split('_')))
        config[arg_camelcase] = value

    # sanitize phone numbers: remove non-digits
    sanitize = lambda no: re.sub('[^+\d]', '', no)
    try: config['ownNumber'] = sanitize(config['ownNumber'])
    except KeyError: pass
    try: config['map'] = { sanitize(number): name for number, name in config['map'].items() }
    except KeyError: pass

    # read the encrypted DB and run the export
    key = get_key(config)
    msgs = get_messages(config, key)
    stats = {}
    hashes = {}

    for i, msg in enumerate(msgs):
        msg_stats = save_attachments(config, hashes, *msg)
        for key, value in msg_stats.items() if msg_stats else {}:
            stats[key] = stats.setdefault(key, 0) + value
        if i > 0 and not i % 50:
            logger.info('%04d/%04d messages | %.1f %% processed', i, len(msgs), i/len(msgs)*100)

    logger.info(
        'Done. %d messages, %d media attachments [%.1f MiB], %d attachments saved [%.1f MiB].',
        len(msgs),
        stats['attachments'],
        stats['attachments_size'] / 2 ** 20,
        stats['saved_attachments'],
        stats['saved_attachments_size'] / 2 ** 20,
    )
