import json
import logging
import os
import re
import yattag

from datetime import datetime
from signal_media_exporter.attachments import AttachmentExporter, stats


logger = logging.getLogger(__name__)


## CSS

css_colors = {
    # signal name: (in msg bg + out quote border-left + avatar, quote bg)
    'red': ('cc163d', 'eda6ae'),
    'deep_orange': ('c73800', 'eba78e'),
    'brown': ('746c53', 'c4b997'),
    'pink': ('a23474', 'dcb2ca'),
    'purple': ('862caf', 'cdaddc'),
    'indigo': ('5951c8', 'c2c1e7'),
    'blue': ('336ba3', 'adc8e1'),
    'teal': ('067589', 'a5cad5'),
    'green': ('3b7845', '8fcc9a'),
    'light_green': ('1c8260', '9bcfbd'),
    'blue_grey': ('895d66', 'cfb5bb')
}


def contact_class(number):
    return 'p' + number[1:]


def write_contacts_css(conversations, directory):
    contact_classes_by_color = {}
    for conv in conversations:
        if conv.get('color') and conv.get('e164'):
            contact_classes_by_color.setdefault(conv['color'], []).append(contact_class(conv['e164']))

    logger.info("Writing contact-colors.css")
    with open(os.path.join(directory, 'contact-colors.css'), 'w') as f:
        for signal_color in css_colors:
            if signal_color in contact_classes_by_color:
                f.write(',\n'.join([f'.message.incoming.{cl} .container' for cl in contact_classes_by_color[signal_color]]))
                f.write(f' {{\n  background-color: #{css_colors[signal_color][0]};\n}}\n')

                f.write(',\n'.join([f'.incoming.{cl} .quote' for cl in contact_classes_by_color[signal_color]]))
                f.write(f' {{\n  background-color: #{css_colors[signal_color][1]};\n}}\n')

                f.write(',\n'.join([f'.outgoing .quote.{cl}' for cl in contact_classes_by_color[signal_color]]))
                f.write(f' {{\n  border-left-color: #{css_colors[signal_color][0]};\n')
                f.write(f'  background-color: #{css_colors[signal_color][1]};\n}}\n')


## HTML

# Fuzzy URL regex. Recognized by a known schema, delimited by whitespace, only allowing as last character one that
# isn't likely to be punctuation
# TODO recognize URLs with no schema (e.g., foobar.com) and maybe other schemas (e.g., mailto)
url_re = re.compile(r'(?i)(?<!\w)((?:file|ftp|https?)://\S*[\w#$%&*+/=@\\^_`|~-])')


def add_header(doc, conversation):
    doc, tag, text = doc.tagtext()
    with tag('header'):
        with tag('div', klass='title-container'):
            with tag('div', klass='title-flex'):
                # TODO add avatar
                with tag('div', klass='title'):
                    text(conversation['displayName'])
                    if conversation.get('e164'):
                        text(' · ' + conversation['e164'])


def add_author(doc, number, contacts_by_number):
    doc.line('div', contacts_by_number[number]['displayName'], klass='author')


def add_message_line(doc, line):
    doc, tag, text = doc.tagtext()
    # isolate URLs to turn them into links
    parts = url_re.split(line)
    for part in parts:
        if url_re.match(part):
            with tag('a', href=part, rel='external noopener noreferrer'):
                text(part)
        else:
            text(part)


def add_message_text(doc, txt, klass):
    doc, tag, text = doc.tagtext()
    # replace newlines with <br/>
    with tag('div', klass=klass):
        lines = txt.split('\n')
        add_message_line(doc, lines[0])
        for line in lines[1:]:
            doc.stag('br')
            add_message_line(doc, line)


def add_quote(doc, quote, contacts_by_number):
    doc, tag, text = doc.tagtext()
    with tag('div', klass='quote-container'):
        with tag('div', klass=f'quote {contact_class(quote["author"])}'):
            with tag('div', klass='primary'):
                add_author(doc, quote['author'], contacts_by_number)
                if quote.get('text') is not None:
                    add_message_text(doc, quote['text'], 'quote-text')

            # TODO display quote attachments
            # for att in quote.get('attachments', []):
            #     if att.get('thumbnail'):
            #         path = attachment_exporter.export(att['thumbnail'], quote['author'], sent_at, msg, idx,
            #                                           purpose_dir='thumbnails')
            #         with tag('div', klass='icon-container'):
            #             doc.stag('img', height=54, width=54, src=path)


def add_attachments(doc, msg, exporter):
    doc, tag, text = doc.tagtext()

    sent_at = datetime.fromtimestamp(msg['sent_at'] / 1000)
    sender_number = msg['source']

    for idx, att in enumerate(msg['attachments']):
        att_path = exporter.export(att, sender_number, sent_at, msg, idx)
        thumbnail_path = None
        if att.get('thumbnail'):
            thumbnail_path = exporter.export(att['thumbnail'], sender_number, sent_at, msg, idx,
                                             purpose_dir='thumbnails')

        if att_path is None:
            # attachment wasn't downloaded :'(
            # TODO add missing-file placeholder
            continue

        if att['contentType'].startswith('audio/'):
            # one must not self-close html tags that may have children
            doc.line('audio', '', 'controls', preload='metadata', src=att_path)

        elif att['contentType'].startswith('image/'):
            with tag('a', href=att_path, rel='noopener noreferrer'):
                doc.stag('img', src=thumbnail_path if thumbnail_path else att_path)

        elif att['contentType'].startswith('video/'):
            with tag('video', 'controls', preload='none', src=att_path):
                if len(msg['attachments']) > 1:
                    doc.attr(height=150, width=150, poster=thumbnail_path)
                elif att.get('screenshot'):  # a video may not have a screenshot
                    screenshot_path = exporter.export(att['screenshot'], sender_number, sent_at, msg, idx,
                                                      purpose_dir='screenshots')
                    doc.attr(poster=screenshot_path)

        else:
            with tag('a', href=att_path, klass='generic-attachment'):
                with tag('div', klass='icon-container'):
                    with tag('div', klass='icon'):
                        _, ext = os.path.splitext(att_path)
                        doc.line('div', ext.lstrip('.'), klass='extension')
                with tag('div', klass='text'):
                    with tag('div', klass='file-name'):
                        text(att['fileName'])
                    with tag('div', klass='file-size'):
                        size = os.path.getsize(os.path.join(exporter.base_dir, att_path))
                        text(f'{size / 1024:.2f} KB')


def add_contacts(doc, contacts):
    # TODO display as HTML and/or export as vCard
    # TODO check if there can be a contact picture to export
    doc, tag, text = doc.tagtext()
    with tag('pre', klass='contacts'):
        with tag('code'):
            text(json.dumps(contacts, indent=2))


def add_errors(doc, errors):
    doc, tag, text = doc.tagtext()
    for error in errors:
        with tag('div', klass='message-text error'):
            text(error['message'])


def add_message_metadata(doc, msg):
    doc, tag, text = doc.tagtext()
    with tag('div', klass='metadata'):
        if msg.get('sent_at') is not None:
            with tag('time'):
                text(datetime.fromtimestamp(msg['sent_at'] // 1000).isoformat(' '))  # local date/time from epoch ms


def add_reactions(doc, reactions, contacts_by_number):
    doc, tag, text = doc.tagtext()

    # group by emoji
    aggregated = {}
    for reaction in reactions:
        aggregated.setdefault(reaction['emoji'], []).append(reaction['fromId'])

    # add to html
    with tag('div', klass='reactions'):
        for emoji, reactors in aggregated.items():
            reactors_names = [contacts_by_number[r]['displayName'] for r in reactors]
            with tag('span', title=', '.join(reactors_names), klass='reaction'):
                text(emoji)
                if len(reactors) > 1:
                    doc.attr(klass='reaction with-count')
                    with tag('span', klass='count'):
                        text(len(reactors))


def add_message(doc, msg, config, contacts_by_number, attachment_exporter):
    # TODO export stickers
    doc, tag, text = doc.tagtext()
    klass = f'message {msg.get("type", "")}'
    if msg.get('source'):
        klass += ' ' + contact_class(msg['source'])
    with tag('div', klass=klass):
        with tag('div', klass='container'):
            if msg.get('type') == 'incoming':
                add_author(doc, msg['source'], contacts_by_number)
            if msg.get('quote') is not None:
                add_quote(doc, msg['quote'], contacts_by_number)
            if msg['attachments']:
                if config['maxAttachments'] == 0 or stats['attachments'] < config['maxAttachments']:
                    add_attachments(doc, msg, attachment_exporter)
            if msg['contact']:
                add_contacts(doc, msg['contact'])
            if msg.get('body') is not None:
                add_message_text(doc, msg['body'], 'message-text')
            if msg.get('errors') and config['includeTechnicalMessages']:
                add_errors(doc, msg['errors'])
            add_message_metadata(doc, msg)
            if msg.get('reactions'):
                add_reactions(doc, msg['reactions'], contacts_by_number)


def add_contact_name(doc, number, contacts_by_number):
    doc.line('span', contacts_by_number[number]['displayName'], klass='contact')


def add_notifications(doc, msg, contacts_by_number):
    doc, tag, text = doc.tagtext()

    # Just in case
    non_notif_fields = ['attachments', 'body', 'contact', 'errors', 'quote', 'reactions', 'sticker']
    missed_fields = [field for field in non_notif_fields if msg.get(field)]
    if missed_fields:
        logger.error(f'Ignoring {", ".join(missed_fields)} in notification message {msg["id"]}')

    with tag('div', klass='inline-notification-wrapper'):
        if msg.get('group_update'):
            gu = msg['group_update']
            with tag('div', klass='notification'):
                if gu.get('joined'):
                    with tag('div', klass='change'):
                        add_contact_name(doc, gu['joined'][0], contacts_by_number)
                        for number in gu['joined'][1:]:
                            text(', ')
                            add_contact_name(doc, number, contacts_by_number)
                        text(' joined the group')
                if gu.get('left'):
                    with tag('div', klass='change'):
                        add_contact_name(doc, gu['left'], contacts_by_number)
                        text(' left the group')
                if gu.get('name'):
                    with tag('div', klass='change'):
                        text(f"Group name is now '{gu['name']}'")
                if not (gu.get('joined') or gu.get('left') or gu.get('name')):
                    with tag('div', klass='change'):
                        # TODO f"{displayName} updated the group"
                        text('The group was updated')

        if msg.get('type') == 'keychange':
            with tag('div', klass='notification'):
                text('The safety number with ')
                add_contact_name(doc, msg['key_changed'], contacts_by_number)
                text(' has changed')

        elif msg.get('type') == 'verified-change':
            with tag('div', klass='notification'):
                text('Your marked the safety number with ')
                add_contact_name(doc, msg['verifiedChanged'], contacts_by_number)
                text(f' as {"" if msg["verified"] else "not "}verified')
                if not msg['local']:
                    text(' from another device')

        elif msg.get('expirationTimerUpdate'):
            with tag('div', klass='notification'):
                timer = msg['expirationTimerUpdate'].get('expireTimer') or 0

                if timer > 0 and msg['expirationTimerUpdate'].get('fromSync'):
                    text(f'Updated disappearing message timer to {timer} s')
                    return

                add_contact_name(doc, msg['expirationTimerUpdate']['source'], contacts_by_number)
                if timer > 0:
                    text(f' set the disappearing message timer to {timer} s')
                else:
                    text(' disabled disappearing messages')

        elif msg.get('type') not in ['incoming', 'outgoing']:
            with tag('div', klass='notification'):
                text(msg.get('type', 'Untyped message'))


def add_main(doc, msgs, config, contacts_by_number, attachment_exporter, conversation_name):
    doc, tag, text = doc.tagtext()
    with tag('main'):
        for i, msg in enumerate(msgs):
            with tag('div', klass='message-container'):
                if msg.get('type') in ['incoming', 'outgoing'] and not msg.get('group_update') and not msg.get('expirationTimerUpdate'):
                    add_message(doc, msg, config, contacts_by_number, attachment_exporter)
                elif config['includeTechnicalMessages']:
                    add_notifications(doc, msg, contacts_by_number)
            if i > 0 and not i % 100:
                logger.info('%04d/%04d messages | %.1f %% of %s processed',
                            i, len(msgs), i / len(msgs) * 100, conversation_name)


def export_conversation(conversation, msgs, config, contacts_by_number, attachment_exporter=None):
    if len(msgs) <= 0:
        logger.info('Skipping %s (no messages)', conversation['displayName'])
        return

    logger.info("Exporting %s", conversation['displayName'])

    stats['messages'] += len(msgs)

    if config['conversationDirs']:
        base_dir = os.path.join(config['outputDir'], conversation['fsName'])
        os.makedirs(base_dir, exist_ok=True)
        attachment_exporter = AttachmentExporter(base_dir, config, contacts_by_number)
        html_file = os.path.join(base_dir, 'index.html')
        resources_dir = '..'
    else:
        html_file = os.path.join(config['outputDir'], conversation['fsName'] + '.html')
        resources_dir = '.'

    # Make HTML
    doc, tag, text = yattag.Doc().tagtext()
    doc.asis('<!DOCTYPE html>')
    with tag('html', ('data-conversation-id', conversation['id']), klass=conversation.get('type', '')):
        with tag('head'):
            doc.stag('meta', charset='utf-8')
            doc.line('title', conversation['displayName'])
            doc.stag('base', target='_blank')
            doc.stag('link', rel='stylesheet', href=resources_dir + '/contact-colors.css')
            doc.stag('link', rel='stylesheet', href=resources_dir + '/signal-desktop.css')
            doc.stag('link', rel='stylesheet', href=resources_dir + '/style.css')
        with tag('body'):
            add_header(doc, conversation)
            add_main(doc, msgs, config, contacts_by_number, attachment_exporter, conversation['displayName'])

    logger.info('Writing out %s.html ...', conversation['fsName'])
    with open(html_file, 'w') as file:
        # note: yattag.indent breaks messages that only contain a link (2020-05-14)
        # https://github.com/leforestier/yattag/issues/62
        # file.write(yattag.indent(doc.getvalue()))
        file.write(doc.getvalue())
