import glob
import logging
import os
import shutil
import sys
from html.parser import HTMLParser


logger = logging.getLogger(__name__)


## Read and update previously exported data

def get_previous_conversation_id(html_path):
    class Found(Exception):
        pass

    class MyHTMLParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == 'html':
                for (name, value) in attrs:
                    if name == 'data-conversation-id':
                        raise Found(value)

    with open(html_path, 'r') as f:
        try:
            MyHTMLParser().feed(f.read())
        except Found as e:
            return e.args[0]
        else:
            return None


def get_previous_conversations_by_id(config):
    if config['conversationDirs']:
        html_files = glob.glob(os.path.join(config['outputDir'], '*', 'index.html'))
        conversation_id_files = glob.glob(os.path.join(config['outputDir'], '*', '*', 'conversationId.txt'))
    else:
        html_files = glob.glob(os.path.join(config['outputDir'], '*.html'))
        conversation_id_files = glob.glob(os.path.join(config['outputDir'], '*', 'conversationId.txt'))

    # Associate to each found conversationId:
    # - the one filesystem name (crash if we find several)
    # - all paths to move if the conversation was renamed
    res = {}

    # Previously exported conversations
    for file in html_files:
        conversation_id = get_previous_conversation_id(file)
        if conversation_id:
            if config['conversationDirs']:
                path = os.path.dirname(file)
                name = os.path.basename(path)
            else:
                path = file
                name, ext = os.path.splitext(os.path.basename(path))

            if conversation_id in res:
                logger.error("Found two previously exported conversations with the same ID: %s and %s",
                             res[conversation_id]['fsName'], name)
                sys.exit(1)

            res[conversation_id] = {'fsName': name, 'conversationPath': path}

    # Senders of previously exported attachments
    for file in conversation_id_files:
        with open(file, 'r') as f:
            conversation_id = f.read()
        path = os.path.dirname(file)
        name = os.path.basename(path)

        if conversation_id in res and name != res[conversation_id]['fsName']:
            logger.error("Found two previously exported conversations or senders with the same ID: %s and %s",
                         res[conversation_id]['fsName'], name)
            sys.exit(1)

        res.setdefault(conversation_id, {'fsName': name}).setdefault('senderPaths', []).append(path)

    return res


def replace_rightmost(old, new, string):
    idx = string.rfind(old)
    if idx > -1:
        return string[:idx] + new + string[idx+len(old):]
    else:
        raise Exception('Substring not found')


def rename_previous_conversations(new_conversations, config):
    # TODO check for previous conversations with same name as a new one but unknown ID
    old_conversations = get_previous_conversations_by_id(config)

    # first move sender dirs, if any (may be contained in convo dirs renamed below)
    for new_conv in new_conversations:
        if new_conv['id'] in old_conversations:
            old_conv = old_conversations[new_conv['id']]
            if new_conv['fsName'] != old_conv['fsName'] and 'senderPaths' in old_conv:
                logger.info('Renaming sender "%s" to "%s"', old_conv['fsName'], new_conv['fsName'])
                for old_path in old_conv['senderPaths']:
                    new_path = replace_rightmost(old_conv['fsName'], new_conv['fsName'], old_path)
                    if os.path.lexists(new_path):
                        logger.error('Cannot rename "%s" to "%s": destination already exists', old_path, new_path)
                        sys.exit(1)
                    shutil.move(old_path, new_path)

    # then move convo dirs or HTML files, if any (may contain sender dirs renamed above)
    for new_conv in new_conversations:
        if new_conv['id'] in old_conversations:
            old_conv = old_conversations[new_conv['id']]
            if new_conv['fsName'] != old_conv['fsName'] and 'conversationPath' in old_conv:
                logger.info('Renaming conversation "%s" to "%s"', old_conv['fsName'], new_conv['fsName'])
                old_path = old_conv['conversationPath']
                new_path = replace_rightmost(old_conv['fsName'], new_conv['fsName'], old_path)
                if os.path.lexists(new_path):
                    logger.error('Cannot rename "%s" to "%s": destination already exists', old_path, new_path)
                    sys.exit(1)
                shutil.move(old_path, new_path)
