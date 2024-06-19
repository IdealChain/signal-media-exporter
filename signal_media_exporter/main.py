#!/usr/bin/env python3

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import coloredlogs
from alive_progress import alive_bar
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

        cond = []
        cond.append("m.type in ('incoming', 'outgoing')")

        include = config.get('includeAttachments', "visual")
        if include == "visual":
            cond.append("m.hasVisualMediaAttachments > 0")
        elif include == "file":
            cond.append("m.hasFileAttachments > 0")
        elif include == "all":
            cond.append("m.hasAttachments > 0")
        else:
            raise ValueError(f"Invalid value '{include}' for 'includeAttachments' in config ")

        if not config.get('includeExpiringMessages', False):
            cond.append("m.expires_at is null")

        c.execute(f"""
            select m.id, m.json, sender.e164, coalesce(sender.name, sender.profileFullName)
            from messages m
            join conversations conv on m.conversationId == conv.id
            join conversations sender on m.sourceServiceId == sender.serviceId
            where {' and '.join(cond)}
            order by m.sent_at
            {f'limit {config["maxMessages"]}' if config["maxMessages"] > 0 else ''}
        """)

        for row in c:
            msg_id = row[0]
            msg = json.loads(row[1])
            sender_e164 = row[2]
            sender_name = row[3]
            yield msg_id, msg, sender_e164, sender_name

    except sqlite.DatabaseError as err:
        logger.fatal(
            'DatabaseError "%s" - please check the database and the sqlcipher parameters!',
            ' | '.join(err.args)
        )

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


def save_attachments(config, hashes, msg_id, msg, sender_e164, sender_name):
    stats = {
        'attachments': 0,
        'attachments_size': 0,
        'saved_attachments': 0,
        'saved_attachments_size': 0,
    }

    try:
        sent = datetime.fromtimestamp(msg['sent_at'] / 1000)
    except KeyError:
        logger.warning('Skipping %s (missing sent_at field)', msg_id)
        return

    sender = None
    sender_keys = [s for s in (sender_e164, sender_name) if s is not None]

    # with map, translate sender number or name to mapped name
    if 'map' in config and len(sender_keys) > 0:
        try:
            sender = next(config['map'][k] for k in sender_keys if k in config['map'])
        except StopIteration:
            logger.warning('Skipping %s (sender number/name not mapped: %s)', msg_id, ", ".join(sender_keys))
            return

    # or without map, use number only (sender_name might not be valid and safe dir name)
    elif sender_e164 is not None:
        sender = sender_e164
    else:
        logger.warning('Skipping %s (sender number unknown)', msg_id)
        return

    for idx, at in enumerate(msg['attachments']):
        if not at['contentType'].lower().startswith(('image/', 'video/', 'audio/')):
            continue

        ext = get_file_extension(at)

        name = ['signal', sent.strftime('%Y-%m-%d-%H%M%S')]
        if len(msg['attachments']) > 1:
            name += str(idx)
        name = '{}.{}'.format('-'.join(name), ext)

        if at.get('pending', False) or not at.get('path'):
            logger.warning('Skipping %s/%s (media file not downloaded)', sender, name)
            continue
        # if accessing a Windows signal database, need to fix paths
        if '\\' in at['path']:
            at_path = os.path.join(*at['path'].split('\\'))
        else:
            at_path = at['path']
        src = os.path.join(config['signalDir'], 'attachments.noindex', at_path)
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


def get_file_extension(at):
    """
    >>> get_file_extension({'contentType': 'image/jpeg'})
    'jpeg'
    >>> get_file_extension({'contentType': 'audio/ogg; codecs=opus'})
    'ogg'
    """
    ext = at['contentType'].lower().split('/')[1]
    if ';' in ext:
        ext = ext.split(';')[0]
    return ext


def sanitize_sender_key(key: str) -> str:
    """
    Sanitize sender phone numbers or names.
    """
    key = key.strip()
    if key.startswith('+'):
        key = sanitize_phone_number(key)
    return key


def sanitize_phone_number(no: str) -> str:
    """
    Sanitize phone numbers by removing non-digits.
    """
    return re.sub(r'[^+\d]', '', no)


def main():
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
    parser.add_argument(
        '-c', '--config',
        nargs='?',
        type=str,
        help=f"path of config file to read (default: {config['config']})"
    )
    parser.add_argument(
        '-o', '--output-dir',
        nargs='?',
        type=str,
        help=f"output directory for media files (default: {config['outputDir']})"
    )
    parser.add_argument(
        '-s', '--signal-dir',
        nargs='?',
        type=str,
        help=f"Signal Desktop profile directory (default: {config['signalDir']})"
    )
    parser.add_argument(
        '-e', '--include-expiring-messages',
        action='store_const',
        const=True,
        help="include expiring messages (default: no)"
    )
    parser.add_argument(
        '-a', '--include-attachments',
        nargs='?',
        type=str,
        help="Which attachments to include (default: visual). Choices: [visual, all]"
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_const',
        const=True,
        help="enable verbose logging (default: no)"
    )
    parser.add_argument(
        '--max-messages',
        metavar='N',
        nargs='?',
        type=int,
        help=f"Export media for at most N messages then stop (default: 0 = no limit)"
    )
    args = parser.parse_args()

    # command line args override the settings from the config file, which override the default settings
    try:
        with open(args.config if args.config else config['config'], 'r') as f:
            config = {**config, **json.load(f)}
    except FileNotFoundError:
        if args.config:
            raise

    for arg, value in vars(args).items():
        if value is None:
            continue

        arg_camelcase = ''.join(w.lower() if i == 0 else w.title() for i, w in enumerate(arg.split('_')))
        config[arg_camelcase] = value

    # configure logging verbosity
    verbose = config.get('verbose', False)
    coloredlogs.install(
        level=logging.INFO if verbose else logging.ERROR,
        fmt='%(asctime)s %(levelname)s %(message)s',
    )

    try:
        config['map'] = {sanitize_sender_key(key): name for key, name in config['map'].items()}
    except KeyError:
        pass

    # validate maxMessages
    if config['maxMessages'] < 0:
        logger.error(f'Invalid max number of messages {config["maxMessages"]} (must be >= 0).')
        sys.exit(-1)

    # read the encrypted DB and run the export
    key = get_key(config)
    msgs = list(get_messages(config, key))
    stats = {
        'attachments': 0,
        'attachments_size': 0,
        'saved_attachments': 0,
        'saved_attachments_size': 0,
    }
    hashes = {}

    with progress(verbose, stats, len(msgs)) as report:
        for msg in msgs:
            msg_stats = save_attachments(config, hashes, *msg)
            for key, value in msg_stats.items() if msg_stats else {}:
                stats[key] = stats.setdefault(key, 0) + value

            report()

    if not stats:
        logger.error('No media messages found.')
        sys.exit(-1)


@contextmanager
def progress(verbose, stats, total):
    i = 0
    stats_frequency = 50

    def msg_stats():
        return f'{i:04d}/{total:04d} messages | {i * 100 / total:.1f} % processed'

    def size_stats():
        return f'{stats["saved_attachments_size"] / 2 ** 20:.1f}/{stats["attachments_size"] / 2 ** 20:.1f} MiB'

    if verbose:
        def report():
            nonlocal i
            i += 1
            if not i % stats_frequency:
                logger.info('%s [%s]', msg_stats(), size_stats())

        yield report

        logger.info(
            'Done. %d messages, %d media attachments [%.1f MiB], %d attachments saved [%.1f MiB].',
            i,
            stats['attachments'],
            stats['attachments_size'] / 2 ** 20,
            stats['saved_attachments'],
            stats['saved_attachments_size'] / 2 ** 20,
        )

    else:
        with alive_bar(total, title='Exporting...') as bar:
            def report():
                nonlocal i
                i += 1
                bar()
                if not i % stats_frequency:
                    bar.text(size_stats())

            yield report
