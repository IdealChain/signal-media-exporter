signal-media-exporter
=====================

Python script to export media files from [Signal Desktop](https://github.com/signalapp/Signal-Desktop).

The Signal Desktop client stores attachment files in the `$HOME/.config/Signal/attachments.noindex` folder. This script opens the encrypted SQLite database to gather the associated metadata (sender number, message timestamp and expiration date, media content type) and copies the media files to the configured export directory, grouped by the sender number/name. It can be run without needing to exit the Signal Desktop client and will only process newly received media files when run in regular intervals.

Features
--------

* export media files to a specified output directory
* either export files from all senders (by number) or from selected senders only (by assigning names)
* file name and modification time will be set to the timestamp of the original message
* only newly received media files will be processed on repeated runs - but if you decide to add or rename a selected sender at a later time, all missing media files will be exported on the next run
* if the same media file appears in multiple conversations, only the earliest instance will be exported (deduplication)
* media files from expiring messages are not exported by default

Requirements
------------

* Python 3.6/3.7 (and pip)
* [pysqlcipher3](https://github.com/rigglemania/pysqlcipher3) (via pip)
* [coloredlogs](https://github.com/xolox/python-coloredlogs) (via pip)

If you have pipenv installed, you can just run:

```
$ cd signal-media-exporter
$ pipenv run export -h
```

Otherwise, to use only pip, you can create a virtalenv by hand:

```
$ cd signal-media-exporter
$ python3 -m venv venv
$ source venv/bin/activate
(venv) $ pip install -r requirements.txt
(venv) $ ./export.py -h
```

Usage / Configuration
---------------------

You can either just use the similar command line parameters (see `export.py -h`) or create a `config.json` file by modifying the supplied `config.json.example`:

```
{
    "outputDir": "./media",
    "includeExpiringMessages": false,
    "ownNumber": "+430000000000",
    "map": {
        "+430000000000": "Me",
        "+430000000001": "My buddy"
    }
}
```

* `outputDir`: Output directory for media files, as a relative or absolute path. Inside this directory, subdirectories for the sender numbers or names will be created.
* `includeExpiringMessages`: Whether to include media files from messages that are set to expire in the future.
* `ownNumber`: Your own phone number, for mapping outgoing messages that have no sender number set. Phone numbers must be complete including the country code.
* `map`: If you include this dict, only the media files sent by the listed numbers will be exported, and the supplied name will be used for the `outputDir` subdirectories (including your own number). If omitted, all media files will be exported using the senders' numbers as subdirectories.
