import hashlib
import logging
import mimetypes
import os
import re
import shutil


logger = logging.getLogger(__name__)


stats = {
    'messages': 0,
    'attachments': 0,
    'attachments_size': 0,
    'saved_attachments': 0,
    'saved_attachments_size': 0,
}


def make_fs_name(name):
    """
        Return a variation of `name` that is a valid file name on Windows and Unix
        See https://docs.microsoft.com/en-us/windows/win32/fileio/naming-a-file
        (Unix only forbids `/`)
    """
    res = re.sub(r'"', "'", name)  # easy special case
    res = re.sub(r'[<>:/\\|?*\x00-\x1f]+', '-', res)
    res = re.sub(r'^(CON|PRN|AUX|NUL|(?:COM|LPT)\d)$', r'\1-', res)
    res = res.rstrip(' .')
    return res


def attachment_extension(att):
    """Return the given attachment's extension (with leading dot)"""
    if att.get('fileName'):
        _, ext = os.path.splitext(att['fileName'])
        if ext:
            return ext

    if att.get('contentType'):  # avatars may not have a contentType
        ext = mimetypes.guess_extension(att['contentType'], strict=False)
        if ext:
            return ext

        # make_fs_name for a JPEG attachment with contentType: 'image/*'
        return make_fs_name('.' + att['contentType'].lower().split('/')[1])

    return ''


def hash_file_quick(path):
    """Return the Python hash of the first 2^10 bytes of the file at the given path"""
    with open(path, 'br') as f:
        data = f.read(2 ** 10)
        return hash(data)


def hash_file_sha256(path):
    """Return the SHA256 hash of the file at the given path"""
    sha256 = hashlib.sha256()
    with open(path, 'br') as f:
        while True:
            data = f.read(2 ** 12)
            if not data:
                break
            sha256.update(data)

        return sha256.hexdigest()


class AttachmentExporter:

    hashes = {}

    def __init__(self, base_dir, config, contacts_by_number):
        self.base_dir = base_dir
        self.config = config
        self.contacts_by_number = contacts_by_number

    def export(self, att, sender_number, sent_at, msg, idx, purpose_dir='.'):
        """Export a single attachment and return its relative destination path"""

        sender = self.contacts_by_number[sender_number]['fsName']

        # Build name (might not get used)
        name = ['signal', sent_at.strftime('%Y-%m-%d-%H%M%S')]
        if len(msg['attachments']) > 1:
            name.append(str(idx))
        name = f'{"-".join(name)}{attachment_extension(att)}'

        if att.get('pending', False) or not att.get('path'):
            logger.warning('Skipping %s/%s (media file not downloaded)', sender, name)
            return
        src = os.path.join(self.config['signalDir'], 'attachments.noindex', att['path'])
        dst = os.path.join(self.base_dir, sender, purpose_dir, name)
        if not os.path.exists(src):
            logger.warning('Skipping %s/%s/%s (media file not found)', sender, purpose_dir, name)
            return

        stats['attachments'] += + 1
        stats['attachments_size'] += os.path.getsize(src)

        quick_hash = hash_file_quick(src)
        if quick_hash in self.hashes:
            if hash_file_sha256(src) in (hash_file_sha256(f) for f in self.hashes[quick_hash]):
                logger.info('Skipping %s/%s/%s (already saved an identical file)', sender, purpose_dir, name)
                # TODO return path
                return

        if os.path.exists(dst):
            # TODO file exported in previous run?
            logger.debug('Skipping %s/%s/%s (file exists)', sender, purpose_dir, name)
            self.hashes.setdefault(quick_hash, []).append(src)
            return os.path.join(sender, purpose_dir, name)

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)
        os.utime(dst, times=(sent_at.timestamp(), sent_at.timestamp()))
        size = os.path.getsize(dst)
        logger.info('Saved %s [%.1f KiB]', dst, size / 1024)

        stats['saved_attachments'] += 1
        stats['saved_attachments_size'] += size
        self.hashes.setdefault(quick_hash, []).append(src)

        return os.path.join(sender, purpose_dir, name)
